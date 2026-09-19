// 用 VSCode 的 TextMate 引擎实测 .jsh 语法高亮
// 运行：node verify-tokenize.js
const fs = require("fs");
const path = require("path");
const vscodeTextmate = require("vscode-textmate");
const vsctm = vscodeTextmate;
const oniguruma = require("vscode-oniguruma");

const SAMPLE = `# 面向对象演示：验证语法高亮
类 账户:
    函数 初始化(自身, 余额):  # 构造方法
        自身.余额 = 余额
    函数 取(自身, 数额):
        如果 数额 > 自身.余额 或 真:
            抛出 值错误("余额不足")
        自身.余额 = 自身.余额 - 数额
    函数 查(自身):
        返回 自身.余额

令 我的账户 = 新建 账户(100)
我的账户.存(50)   # 存钱
打印("账户余额：", 我的账户.查())
尝试:
    我的账户.取(999)
捕获 值错误 为 e:
    打印("取款失败：", e.消息)
最终:
    打印("结束")
`;

const SCOPES_OF_INTEREST = {
  "keyword.control.jishi": "关键字",
  "constant.language.jishi": "布尔字面量",
  "constant.numeric.jishi": "数字",
  "string.quoted.double.jishi": "字符串",
  "comment.line.number-sign.jishi": "注释",
  "entity.name.function.jishi": "函数名",
  "entity.name.class.jishi": "类名",
  "support.function.builtin.jishi": "内建函数",
  "keyword.operator.logical.jishi": "逻辑运算符",
};

async function main() {
  const wasmBin = fs.readFileSync(
    path.join(__dirname, "node_modules/vscode-oniguruma/release/onig.wasm")).buffer;
  const onigLib = await oniguruma.loadWASM(wasmBin);
  const registry = new vsctm.Registry({
    onigLib: Promise.resolve({ createOnigScanner: (s) => new oniguruma.OnigScanner(s),
                               createOnigString: (s) => new oniguruma.OnigString(s) }),
    loadGrammar: async (scopeName) => {
      if (scopeName === "source.jishi") {
        const text = fs.readFileSync(
          path.join(__dirname, "syntaxes/jishi.tmLanguage.json"), "utf-8");
        return vsctm.parseRawGrammar(text, "jishi.tmLanguage.json");
      }
      return null;
    },
  });

  const grammar = await registry.loadGrammar("source.jishi");
  if (!grammar) { console.error("✗ 语法加载失败"); process.exit(1); }

  let ruleStack = vsctm.INITIAL;
  const hits = new Set();
  const lines = SAMPLE.split("\n");
  console.log("=== tokenize 结果（仅显示命中的高亮分类） ===\n");
  lines.forEach((line, li) => {
    const result = grammar.tokenizeLine(line, ruleStack);
    ruleStack = result.ruleStack;
    const toks = result.tokens.filter(
      t => Object.keys(SCOPES_OF_INTEREST).some(s => t.scopes.includes(s)));
    if (toks.length === 0) return;
    const parts = toks.map(t => {
      const scope = t.scopes.find(s => SCOPES_OF_INTEREST[s]) || "";
      const name = SCOPES_OF_INTEREST[scope] || scope;
      hits.add(scope);
      return `[${name}]${JSON.stringify(line.slice(t.startIndex, t.endIndex))}`;
    });
    console.log(`L${li+1}: ${parts.join(" ")}`);
  });

  console.log("\n=== 命中的高亮分类 ===");
  const expected = ["keyword.control.jishi", "constant.language.jishi",
    "constant.numeric.jishi", "string.quoted.double.jishi",
    "comment.line.number-sign.jishi", "entity.name.function.jishi",
    "entity.name.class.jishi", "support.function.builtin.jishi"];
  for (const e of expected) {
    console.log(hits.has(e) ? `  ✓ ${SCOPES_OF_INTEREST[e]}` : `  ✗ 未命中 ${e}`);
  }
  const missing = expected.filter(e => !hits.has(e));
  if (missing.length) { console.error("\n✗ 有分类未命中"); process.exit(1); }
  console.log("\n✓ 语法高亮验证通过");
}

main().catch(e => { console.error(e); process.exit(1); });
