'use strict';
/**
 * 基石（jishi）VS Code 扩展：在自带的语法高亮之上，接上语言服务。
 *
 * 语言服务由 `jishi lsp` 提供（本项目自带，stdio 上的 LSP），本文件只做
 * 「编辑器 API ↔ LSP 消息」的翻译。
 *
 * 覆盖的能力（M21 起 + M42 新增）：
 *   诊断 / 补全 / 悬停 / 跳转定义 / 文档符号 / 工作区符号 / 格式化 /
 *   重命名 / 快速修复 / 签名提示 / 语义高亮
 *
 * 为什么手写而不引 `vscode-languageclient`：见 `lsp-transport.js` 顶部说明。
 * 手写还有一个实际好处——**服务端支持的每一项能力都能在这里逐条对上**，
 * 不会出现「装了客户端但某项功能悄悄没接」的情况。
 *
 * 调试方式：用 VS Code 打开本目录，按 F5（Extension Development Host），
 * 在里面打开任意 .jsh 文件即可。
 */

const vscode = require('vscode');
const { LspTransport } = require('./lsp-transport');

/** @type {LspTransport | null} */
let transport = null;
let serverCapabilities = {};
let diagnostics = null;
let statusItem = null;
let serverArgs = [];
let output = null;

//: 语言文档选择器。显式写 `scheme: '*'`——我们要的就是「任何来源的 .jsh 都管」
//: （新建还没保存的文件是 `untitled:`，也该有补全与诊断）。写上 scheme 之后，
//: VS Code 那句「document selector without scheme」的提示也就不再出现了。
const DOC_SELECTOR = { language: 'jishi', scheme: '*' };

// --- LSP → VS Code 的枚举映射（两边数值**不通用**，别拿数字硬套）---

const COMPLETION_KIND = {
  2: vscode.CompletionItemKind.Method,
  3: vscode.CompletionItemKind.Function,
  5: vscode.CompletionItemKind.Field,
  6: vscode.CompletionItemKind.Variable,
  7: vscode.CompletionItemKind.Class,
  9: vscode.CompletionItemKind.Module,
  10: vscode.CompletionItemKind.Property,
  13: vscode.CompletionItemKind.Enum,
  14: vscode.CompletionItemKind.Keyword,
  15: vscode.CompletionItemKind.Snippet,
};

const SYMBOL_KIND = {
  2: vscode.SymbolKind.Module,
  5: vscode.SymbolKind.Class,
  12: vscode.SymbolKind.Function,
  13: vscode.SymbolKind.Variable,
};

const SEVERITY = {
  1: vscode.DiagnosticSeverity.Error,
  2: vscode.DiagnosticSeverity.Warning,
  3: vscode.DiagnosticSeverity.Information,
  4: vscode.DiagnosticSeverity.Hint,
};

function toRange(r) {
  return new vscode.Range(
    r.start.line, r.start.character, r.end.line, r.end.character);
}

function toLocation(loc) {
  return new vscode.Location(vscode.Uri.parse(loc.uri), toRange(loc.range));
}

function toWorkspaceEdit(changes) {
  const edit = new vscode.WorkspaceEdit();
  for (const [uri, edits] of Object.entries(changes || {})) {
    const target = vscode.Uri.parse(uri);
    for (const e of edits) edit.replace(target, toRange(e.range), e.newText);
  }
  return edit;
}

// ---------------------------------------------------------------------------
// 服务器生命周期
// ---------------------------------------------------------------------------

function log(line) {
  if (output) output.appendLine(line);
}

async function startServer(context) {
  const cfg = vscode.workspace.getConfiguration('jishi');
  const command = cfg.get('server.command', 'jishi');
  serverArgs = cfg.get('server.args', ['lsp']);

  transport = new LspTransport(command, serverArgs, {
    cwd: vscode.workspace.workspaceFolders &&
         vscode.workspace.workspaceFolders.length
      ? vscode.workspace.workspaceFolders[0].uri.fsPath
      : undefined,
  });

  transport.onNotification((method, params) => {
    if (method === 'textDocument/publishDiagnostics') {
      publishDiagnostics(params);
    }
  });
  if (cfg.get('server.trace', false)) {
    transport.onMessage = (dir, msg) => {
      log(`${dir} ${msg.method || `#${msg.id}`} ` +
          `${JSON.stringify(msg.params || msg.result || '').slice(0, 300)}`);
    };
  }
  transport.onRequest((method) => {
    // 服务器问什么我们都给个空答复，别让它等（等 = 编辑器里没反应）
    if (method === 'workspace/configuration') return [{}];
    return undefined;
  });
  transport.onExit((err) => {
    log(`语言服务退出：${err.message}`);
    setStatus('已停止', 'warning');
    vscode.window.showWarningMessage(
      `基石语言服务已退出：${err.message}`,
      '重启', '看日志').then((pick) => {
      if (pick === '重启') restartServer(context);
      else if (pick === '看日志' && output) output.show();
    });
  });

  transport.start();

  const rootUri = vscode.workspace.workspaceFolders &&
                  vscode.workspace.workspaceFolders.length
    ? vscode.workspace.workspaceFolders[0].uri.toString()
    : null;

  const init = await transport.request('initialize', {
    processId: process.pid,
    rootUri,
    capabilities: {
      textDocument: {
        synchronization: { dynamicRegistration: false },
        completion: { completionItem: { snippetSupport: false } },
        hover: { contentFormat: ['markdown', 'plaintext'] },
        publishDiagnostics: { relatedInformation: false },
      },
      workspace: { workspaceFolders: false },
    },
    workspaceFolders: null,
  });
  serverCapabilities = (init && init.capabilities) || {};
  transport.notify('initialized', {});
  log(`语言服务已就绪（${command} ${serverArgs.join(' ')}）`);
  setStatus('就绪', 'ok');
  registerFeatures(context);

  // 已经打开的文档补发 didOpen（重启服务器时不能漏）
  for (const doc of vscode.workspace.textDocuments) {
    if (doc.languageId === 'jishi') sendOpen(doc);
  }
}

function restartServer(context) {
  if (transport) transport.dispose();
  transport = null;
  diagnostics && diagnostics.clear();
  return startServer(context).catch((e) => {
    setStatus('启动失败', 'error');
    // 给可点的出路，别只甩一句错误：起不来最常见的原因是 `jishi` 不在 PATH 上
    // （装是装了，但没激活虚拟环境 / 没加 PATH）。
    vscode.window.showErrorMessage(
      `基石语言服务启动失败：${e.message}`,
      '设置 jishi 路径', '看日志').then(async (pick) => {
      if (pick === '设置 jishi 路径') {
        await vscode.commands.executeCommand(
          'workbench.action.openSettings', 'jishi.server.command');
      } else if (pick === '看日志' && output) {
        output.show();
      }
    });
  });
}

function setStatus(text, kind) {
  if (!statusItem) return;
  statusItem.text = `$(symbol-method) 基石 LSP：${text}`;
  statusItem.tooltip = kind === 'ok' ? '点击重启语言服务' : text;
  statusItem.command = 'jishi.restartServer';
}

// ---------------------------------------------------------------------------
// 文档同步
// ---------------------------------------------------------------------------

function sendOpen(doc) {
  if (!transport) return;
  transport.notify('textDocument/didOpen', {
    textDocument: { uri: doc.uri.toString(), languageId: 'jishi',
                    version: doc.version, text: doc.getText() },
  });
}

function registerSync(context) {
  context.subscriptions.push(
    vscode.workspace.onDidOpenTextDocument((doc) => {
      if (doc.languageId === 'jishi') sendOpen(doc);
    }),
    vscode.workspace.onDidChangeTextDocument((e) => {
      if (!transport || e.document.languageId !== 'jishi') return;
      // 增量同步：只发改动的那一段（服务端声明了 change = 2）
      transport.notify('textDocument/didChange', {
        textDocument: { uri: e.document.uri.toString(),
                        version: e.document.version },
        contentChanges: e.contentChanges.map((c) => ({
          range: {
            start: { line: c.range.start.line,
                     character: c.range.start.character },
            end: { line: c.range.end.line, character: c.range.end.character },
          },
          text: c.text,
        })),
      });
    }),
    vscode.workspace.onDidCloseTextDocument((doc) => {
      if (!transport || doc.languageId !== 'jishi') return;
      transport.notify('textDocument/didClose', {
        textDocument: { uri: doc.uri.toString() },
      });
      diagnostics && diagnostics.delete(doc.uri);
    }),
  );
}

function publishDiagnostics(params) {
  if (!diagnostics) return;
  const uri = vscode.Uri.parse(params.uri);
  const list = (params.diagnostics || []).map((d) => {
    const diag = new vscode.Diagnostic(
      toRange(d.range), d.message,
      SEVERITY[d.severity] || vscode.DiagnosticSeverity.Error);
    diag.source = d.source || 'jishi';
    if (d.code !== undefined) diag.code = d.code;
    return diag;
  });
  diagnostics.set(uri, list);
}

// ---------------------------------------------------------------------------
// 各个能力
// ---------------------------------------------------------------------------

function docUri(document) { return document.uri.toString(); }

function registerFeatures(context) {
  const caps = serverCapabilities;

  // --- 补全 ---
  context.subscriptions.push(vscode.languages.registerCompletionItemProvider(
    DOC_SELECTOR, {
      async provideCompletionItems(document, position) {
        const res = await transport.request('textDocument/completion', {
          textDocument: { uri: docUri(document) },
          position: { line: position.line, character: position.character },
        });
        return (res.items || []).map((it) => {
          const item = new vscode.CompletionItem(
            it.label, COMPLETION_KIND[it.kind] || vscode.CompletionItemKind.Text);
          item.detail = it.detail || '';
          if (it.documentation) {
            item.documentation = new vscode.MarkdownString(
              it.documentation.value || String(it.documentation));
          }
          return item;
        });
      },
    }, '.'));

  // --- 悬停 ---
  context.subscriptions.push(vscode.languages.registerHoverProvider(DOC_SELECTOR, {
    async provideHover(document, position) {
      const res = await transport.request('textDocument/hover', {
        textDocument: { uri: docUri(document) },
        position: { line: position.line, character: position.character },
      });
      if (!res) return undefined;
      const md = new vscode.MarkdownString(
        (res.contents && res.contents.value) || String(res.contents || ''));
      return new vscode.Hover(md, res.range ? toRange(res.range) : undefined);
    },
  }));

  // --- 跳转定义（含跨文件）---
  context.subscriptions.push(
    vscode.languages.registerDefinitionProvider(DOC_SELECTOR, {
      async provideDefinition(document, position) {
        const res = await transport.request('textDocument/definition', {
          textDocument: { uri: docUri(document) },
          position: { line: position.line, character: position.character },
        });
        return res ? toLocation(res) : undefined;
      },
    }));

  // --- 文档符号（大纲）---
  context.subscriptions.push(
    vscode.languages.registerDocumentSymbolProvider(DOC_SELECTOR, {
      async provideDocumentSymbols(document) {
        const res = await transport.request('textDocument/documentSymbol', {
          textDocument: { uri: docUri(document) },
        });
        return (res || []).map((s) => {
          const sym = new vscode.DocumentSymbol(
            s.name, s.detail || '',
            SYMBOL_KIND[s.kind] || vscode.SymbolKind.Variable,
            toRange(s.range),
            toRange(s.selectionRange || s.range));
          return sym;
        });
      },
    }));

  // --- 工作区符号 ---
  if (caps.workspaceSymbolProvider) {
    context.subscriptions.push(
      vscode.languages.registerWorkspaceSymbolProvider({
        async provideWorkspaceSymbols(query) {
          const res = await transport.request('workspace/symbol', { query });
          return (res || []).map((s) => new vscode.SymbolInformation(
            s.name,
            SYMBOL_KIND[s.kind] || vscode.SymbolKind.Variable,
            s.containerName || '',
            toLocation(s.location)));
        },
      }));
  }

  // --- 格式化 ---
  if (caps.documentFormattingProvider) {
    context.subscriptions.push(
      vscode.languages.registerDocumentFormattingEditProvider(DOC_SELECTOR, {
        async provideDocumentFormattingEdits(document) {
          const res = await transport.request('textDocument/formatting', {
            textDocument: { uri: docUri(document) },
            options: { tabSize: 4, insertSpaces: true },
          });
          return (res || []).map(
            (e) => new vscode.TextEdit(toRange(e.range), e.newText));
        },
      }));
  }

  // --- 重命名（含「能不能改」的前置判断）---
  if (caps.renameProvider) {
    context.subscriptions.push(
      vscode.languages.registerRenameProvider(DOC_SELECTOR, {
        async prepareRename(document, position) {
          const res = await transport.request('textDocument/prepareRename', {
            textDocument: { uri: docUri(document) },
            position: { line: position.line, character: position.character },
          });
          // 服务端返回 null（不支持）时必须回 undefined，VS Code 才会
          // 禁用输入框并说「无法重命名」——回 null 会被当成一个空编辑。
          if (!res) return undefined;
          return { range: toRange(res.range), placeholder: res.placeholder };
        },
        async provideRenameEdits(document, position, newName) {
          const res = await transport.request('textDocument/rename', {
            textDocument: { uri: docUri(document) },
            position: { line: position.line, character: position.character },
            newName,
          });
          return res ? toWorkspaceEdit(res.changes) : undefined;
        },
      }));
  }

  // --- 快速修复 ---
  if (caps.codeActionProvider) {
    context.subscriptions.push(
      vscode.languages.registerCodeActionsProvider(DOC_SELECTOR, {
        async provideCodeActions(document, range, ctx) {
          const only = (ctx.only || []).map((k) => k.value || String(k));
          const res = await transport.request('textDocument/codeAction', {
            textDocument: { uri: docUri(document) },
            range: { start: { line: range.start.line,
                              character: range.start.character },
                     end: { line: range.end.line,
                            character: range.end.character } },
            context: { diagnostics: [], only },
          });
          return (res || []).map((a) => {
            const action = new vscode.CodeAction(
              a.title, new vscode.CodeActionKind(a.kind || 'quickfix'));
            if (a.isPreferred) action.isPreferred = true;
            if (a.edit) action.edit = toWorkspaceEdit(a.edit.changes);
            return action;
          });
        },
      }, { providedCodeActionKinds: [vscode.CodeActionKind.QuickFix,
                                    vscode.CodeActionKind.Source] }));
  }

  // --- 签名提示 ---
  if (caps.signatureHelpProvider) {
    const triggers = caps.signatureHelpProvider.triggerCharacters || ['('];
    context.subscriptions.push(
      vscode.languages.registerSignatureHelpProvider(DOC_SELECTOR, {
        async provideSignatureHelp(document, position) {
          const res = await transport.request('textDocument/signatureHelp', {
            textDocument: { uri: docUri(document) },
            position: { line: position.line, character: position.character },
          });
          if (!res || !res.signatures || !res.signatures.length) {
            return undefined;
          }
          const help = new vscode.SignatureHelp();
          help.signatures = res.signatures.map((s) => {
            const sig = new vscode.SignatureInformation(
              s.label,
              s.documentation && s.documentation.value
                ? new vscode.MarkdownString(s.documentation.value)
                : undefined);
            sig.parameters = (s.parameters || []).map((p) =>
              new vscode.ParameterInformation(p.label));
            return sig;
          });
          help.activeSignature = res.activeSignature || 0;
          help.activeParameter = res.activeParameter || 0;
          return help;
        },
      }, ...triggers));
  }

  // --- 语义高亮 ---
  const sem = caps.semanticTokensProvider;
  if (sem && sem.legend) {
    const legend = new vscode.SemanticTokensLegend(
      sem.legend.tokenTypes || [], sem.legend.tokenModifiers || []);
    context.subscriptions.push(
      vscode.languages.registerDocumentSemanticTokensProvider(DOC_SELECTOR, {
        async provideDocumentSemanticTokens(document) {
          const res = await transport.request(
            'textDocument/semanticTokens/full',
            { textDocument: { uri: docUri(document) } });
          const data = new Uint32Array((res && res.data) || []);
          return new vscode.SemanticTokens(data);
        },
      }, legend));
  }
}

// ---------------------------------------------------------------------------
// 入口
// ---------------------------------------------------------------------------
// 调试器（M43）：把 `jishi dap` 交给 VS Code
//
// 这里**不用**自己写传输层：`DebugAdapterExecutable` 就是「起一个子进程 +
// 做 DAP 的 stdio 转发」这件事的官方做法，VS Code 自己会把断点、单步、
// 变量面板那一套接上。语言服务那边之所以自己写 lsp-transport.js，是因为
// 要在**非扩展**环境（verify-lsp.js / 自检）里也能跑同一份代码。
// ---------------------------------------------------------------------------

/** 取「可执行文件 + 工作目录」——语言服务与调试适配器共用同一套设置。 */
function serverLaunch() {
  const cfg = vscode.workspace.getConfiguration('jishi');
  return {
    command: cfg.get('server.command', 'jishi'),
    cwd: vscode.workspace.workspaceFolders
      ? vscode.workspace.workspaceFolders[0].uri.fsPath : undefined,
  };
}

/** 启动配置里没给 program 时，用当前打开的那个 .jsh。 */
function activeJishiFile() {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.document.languageId !== 'jishi') return null;
  return editor.document.uri.fsPath;
}

function registerDebugger(context) {
  context.subscriptions.push(
    vscode.debug.registerDebugAdapterDescriptorFactory('jishi', {
      createDebugAdapterDescriptor() {
        const { command, cwd } = serverLaunch();
        return new vscode.DebugAdapterExecutable(command, ['dap'], {
          cwd,
          // Windows 上 Python 的 stdout 默认不是 UTF-8，程序里的中文会炸
          //（与 CI 里设 PYTHONUTF8 是同一个理由）
          env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
        });
      },
    }),
  );

  context.subscriptions.push(
    vscode.debug.registerDebugConfigurationProvider('jishi', {
      // 第一次按 F5 且没有 launch.json 时，VS Code 会拿这个当模板
      provideDebugConfigurations() {
        return [{
          type: 'jishi',
          request: 'launch',
          name: '调试基石脚本',
          program: '${file}',
          stopOnEntry: false,
        }];
      },
      resolveDebugConfiguration(_folder, config) {
        if (!config.type && !config.request && !config.name) {
          const file = activeJishiFile();
          if (!file) {
            vscode.window.showInformationMessage('先打开一个 .jsh 文件再启动调试');
            return undefined;          // 返回 undefined = 中止这次启动
          }
          config.type = 'jishi';
          config.request = 'launch';
          config.name = '调试基石脚本';
        }
        if (!config.program) {
          const file = activeJishiFile();
          if (!file) {
            vscode.window.showInformationMessage('先打开一个 .jsh 文件再启动调试');
            return undefined;
          }
          config.program = file;
        }
        return config;
      },
    }),
  );
}

// ---------------------------------------------------------------------------

async function activate(context) {
  output = vscode.window.createOutputChannel('基石语言服务');
  diagnostics = vscode.languages.createDiagnosticCollection('jishi');
  statusItem = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right, 50);
  setStatus('启动中…', 'pending');
  statusItem.show();

  context.subscriptions.push(
    output, diagnostics, statusItem,
    vscode.commands.registerCommand('jishi.restartServer', () =>
      restartServer(context)),
  );

  registerSync(context);
  registerDebugger(context);
  await restartServer(context);
}

function deactivate() {
  if (transport) transport.dispose();
  transport = null;
}

module.exports = { activate, deactivate };
