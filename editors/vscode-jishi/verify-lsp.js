'use strict';
/**
 * 语言服务自检（脱离 VS Code 跑）：`node verify-lsp.js [jishi 可执行文件]`
 *
 * 做的事就是把扩展会走的那条路走一遍——initialize → didOpen → 逐项请求——
 * 然后断言每一项**都真的返回了东西**。价值在于把「扩展里某个能力没接上」
 * 这种问题挡在装编辑器之前：装进 VS Code 之后才发现「点了没反应」，
 * 排查成本高得多。
 *
 * 用**两份**文档，因为它们考的是两件事（第一版把两者混在一份里，
 * 结果「跳转定义/重命名/签名」全报失败——其实是那份文档本身就解析不过，
 * 服务端拿不到 AST，这是**正确行为**）：
 *
 * - `干净.jsh`：语法完整 → 考补全 / 悬停 / 跳转 / 大纲 / 重命名 / 签名 / 语义高亮
 * - `有错.jsh`：含英文内建 → 考诊断与快速修复
 *
 * 覆盖：诊断 / 补全 / 悬停 / 跳转定义 / 文档符号 / 工作区符号 / 格式化 /
 *       重命名 / 快速修复 / 签名提示 / 语义高亮。
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { LspTransport } = require('./lsp-transport');

const JISHI = process.argv[2] || process.env.JISHI_BIN || 'jishi';
//: 启动参数默认 `["lsp"]`；`JISHI_ARGS` 可覆盖，好让自动化测试用
//: `python -m jishi.cli lsp` 这种形式跑（不必依赖已安装的入口脚本）。
const ARGS = process.env.JISHI_ARGS
  ? process.env.JISHI_ARGS.split(' ').filter(Boolean)
  : ['lsp'];

const 干净 = [
  '导入 容器',
  '函数 求平均(分数们)：',
  '    令 总数 = 0',
  '    返回 总数',
  '',
  '令 成绩 = [85, 92]',
  '打印(求平均(成绩))',
  '',
].join('\n');

const 有错 = '打印(len([1, 2]))\n';

const URI干净 = 'file:///verify-%E5%B9%B2%E5%87%80.jsh';
const URI有错 = 'file:///verify-%E6%9C%89%E9%94%99.jsh';
const URI别的 = 'file:///verify-%E5%88%AB%E7%9A%84.jsh';

let 失败 = 0;
let 通过 = 0;

function 断言(条件, 名称, 细节) {
  if (条件) {
    通过 += 1;
    console.log(`  ✅ ${名称}`);
  } else {
    失败 += 1;
    console.log(`  ❌ ${名称}${细节 ? `  （${细节}）` : ''}`);
  }
}

async function main() {
  const 工作区 = fs.mkdtempSync(path.join(os.tmpdir(), 'jishi-lsp-verify-'));
  fs.writeFileSync(path.join(工作区, '别的.jsh'),
    '函数 打招呼()：\n    返回 1\n', 'utf8');

  const t = new LspTransport(JISHI, ARGS, { cwd: 工作区 });
  t.start();

  const init = await t.request('initialize', {
    processId: process.pid,
    rootUri: 'file:///' + 工作区.replace(/\\/g, '/'),
    capabilities: {},
  });
  const caps = init.capabilities || {};
  console.log('\n── 能力声明 ──────────────────────────────');
  for (const k of ['completionProvider', 'hoverProvider', 'definitionProvider',
                   'documentSymbolProvider', 'documentFormattingProvider',
                   'renameProvider', 'codeActionProvider',
                   'signatureHelpProvider', 'workspaceSymbolProvider',
                   'semanticTokensProvider']) {
    断言(!!caps[k], `声明了 ${k}`);
  }
  t.notify('initialized', {});

  const 诊断 = new Map();
  t.onNotification((method, params) => {
    if (method === 'textDocument/publishDiagnostics') 诊断.set(params.uri, params);
  });

  const 开 = (uri, text) => t.notify('textDocument/didOpen', {
    textDocument: { uri, languageId: 'jishi', version: 1, text },
  });
  开(URI干净, 干净);
  开(URI有错, 有错);

  const 位置 = (line, character) => ({ line, character });
  const 请 = (uri, method, params) => t.request(method, {
    textDocument: { uri }, ...params,
  });

  await new Promise((r) => setTimeout(r, 400));

  console.log('\n── 诊断与快速修复（有错的那份）──────────');
  const d = 诊断.get(URI有错);
  断言(!!d, '收到了 publishDiagnostics');
  const 有len = d && d.diagnostics.some((x) => x.message.includes('len'));
  断言(有len, '英文内建 `len` 被报了出来',
       d ? d.diagnostics.map((x) => x.message.slice(0, 26)).join(' | ') : '无');
  const 动作 = await 请(URI有错, 'textDocument/codeAction', {
    range: { start: 位置(0, 3), end: 位置(0, 6) }, context: { only: [] } });
  断言(Array.isArray(动作) && 动作.some(
    (a) => a.kind === 'quickfix' &&
           a.edit.changes[URI有错][0].newText === '长度'),
    '给出了「改成 长度」的快速修复',
    (动作 || []).map((a) => a.title).join(' / '));

  console.log('\n── 逐项能力（干净的那份）────────────────');
  const 补全 = await 请(URI干净, 'textDocument/completion', { position: 位置(7, 0) });
  断言(补全 && 补全.items && 补全.items.length > 0, '补全有结果',
       `items=${补全 && 补全.items.length}`);

  const 悬停 = await 请(URI干净, 'textDocument/hover', { position: 位置(2, 6) });
  断言(悬停 && 悬停.contents, '悬停有内容');

  const 定义 = await 请(URI干净, 'textDocument/definition', { position: 位置(6, 4) });
  断言(定义 && 定义.uri && 定义.range.start.line === 1, '跳转定义落到函数定义行',
       JSON.stringify(定义 && 定义.range));

  const 大纲 = await 请(URI干净, 'textDocument/documentSymbol', {});
  断言(Array.isArray(大纲) && 大纲.length > 0, '文档符号有结果',
       `n=${大纲 && 大纲.length}`);

  const 格式化 = await 请(URI干净, 'textDocument/formatting', {
    options: { tabSize: 4, insertSpaces: true } });
  断言(Array.isArray(格式化), '格式化有应答（已格式化→空数组也算对）');

  const 改名前 = await 请(URI干净, 'textDocument/prepareRename', {
    position: 位置(1, 3) });
  断言(改名前 && 改名前.placeholder === '求平均', '可重命名处给了占位符',
       JSON.stringify(改名前));
  const 改名 = await 请(URI干净, 'textDocument/rename', {
    position: 位置(1, 3), newName: '算平均' });
  const 编辑数 = 改名 && 改名.changes && 改名.changes[URI干净]
    ? 改名.changes[URI干净].length : 0;
  断言(编辑数 === 2, '重命名改了定义处 + 调用处两处', `编辑数=${编辑数}`);

  const 不可改名 = await 请(URI干净, 'textDocument/prepareRename', {
    position: 位置(1, 8) });
  断言(不可改名 === null, '形参不给改名（没有作用域分析，宁可不做）',
       JSON.stringify(不可改名));

  const 签名 = await 请(URI干净, 'textDocument/signatureHelp', {
    position: 位置(6, 7) });
  断言(签名 && 签名.signatures && 签名.signatures.length > 0 &&
       签名.signatures[0].label === '求平均(分数们)',
       '签名提示给出了形参表', JSON.stringify(签名 && 签名.signatures));

  const 语义 = await 请(URI干净, 'textDocument/semanticTokens/full', {});
  断言(语义 && 语义.data && 语义.data.length > 0 &&
       语义.data.length % 5 === 0,
       '语义高亮数据合法（5 的倍数）', `len=${语义 && 语义.data.length}`);

  const 工作区符号 = await t.request('workspace/symbol', { query: '' });
  断言(Array.isArray(工作区符号) && 工作区符号.some((s) => s.name === '打招呼'),
       '工作区符号扫到了磁盘上的别的文件',
       `n=${工作区符号 && 工作区符号.length}`);

  const 跨文件 = await 请(URI干净, 'textDocument/definition', {
    position: 位置(7, 0) });
  断言(跨文件 === null || 跨文件 === undefined,
       '空行上不给跳转（不该乱跳）', JSON.stringify(跨文件));

  // 干净收尾：先让子进程退出，再删临时目录（Windows 上目录被占用会 EBUSY）
  t.dispose();
  await new Promise((r) => setTimeout(r, 200));
  try {
    fs.rmSync(工作区, { recursive: true, force: true });
  } catch (e) {
    /* 临时目录删不掉不影响结论 */
  }

  console.log(`\n通过 ${通过} 项，失败 ${失败} 项`);
  process.exit(失败 === 0 ? 0 : 1);
}

main().catch((e) => {
  console.error(`自检崩了：${e.message}`);
  process.exit(2);
});
