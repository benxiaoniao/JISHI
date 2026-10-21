// Excel 读（M123）—— `.xlsx` = zip 包 + 一堆 XML。
// 由 `vm.js` 注入内部工具（zip 解析 / 表格构造 / 文本与类型转换），
// 这样它跟 `压缩` / `表格` 用的是**同一份**底层实现，不会自成一套。
//
// ⚠️ XML 没引第三方库：xlsx 里的 XML 是**机器生成**的（标签规整、属性永远带引号、
//    没有 DTD / CDATA），一个「按标签名扫片段」的扫描器够用，行为也完全可预测。
//    唯一要当心的是实体（`&amp;` 之类），否则「A&B」会读成字面的 `A&amp;B`。
module.exports = function (工具) {
  const { zipBytes, readZipEntries, makeTable, pText, R } = 工具;

  /** 还原五个 XML 实体。找不到分号就把 `&` 当普通字符（别把整段吃掉）。 */
  function _xl还原(s) {
    if (!s.includes('&')) return s;
    const 表 = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };
    let 出 = '';
    for (let i = 0; i < s.length; i++) {
      if (s[i] === '&') {
        const 分号 = s.indexOf(';', i + 1);
        if (分号 > 0 && 分号 - i <= 11) {
          const 名 = s.slice(i + 1, 分号);
          if (表[名] !== undefined) { 出 += 表[名]; i = 分号; continue; }
        }
      }
      出 += s[i];
    }
    return 出;
  }

  /** 找 `名` 元素的内容片段（不含标签）。只找第一个。 */
  function _xl首个内容(xml, 名) {
    const 起 = xml.indexOf('<' + 名);
    if (起 < 0) return null;
    const 头尾 = xml.indexOf('>', 起);
    if (头尾 < 0) return null;
    if (xml.slice(起, 头尾 + 1).endsWith('/>')) return '';
    const 尾 = xml.indexOf('</' + 名 + '>', 头尾);
    if (尾 < 0) return null;
    return xml.slice(头尾 + 1, 尾);
  }

  /** 所有 `名` 元素的完整片段（含标签对）。 */
  function _xl元素们(xml, 名) {
    const 开 = '<' + 名;
    const 闭 = '</' + 名 + '>';
    const 出 = [];
    let 起 = 0;
    for (;;) {
      const 头 = xml.indexOf(开, 起);
      if (头 < 0) break;
      const 后 = xml[头 + 开.length];
      // 边界：`<c` 不该匹配 `<cols`
      if (后 && /[A-Za-z0-9_:]/.test(后)) { 起 = 头 + 开.length; continue; }
      const 头尾 = xml.indexOf('>', 头);
      if (头尾 < 0) break;
      if (xml.slice(头, 头尾 + 1).endsWith('/>')) {
        出.push(xml.slice(头, 头尾 + 1));
        起 = 头尾 + 1;
        continue;
      }
      const 尾 = xml.indexOf(闭, 头尾);
      if (尾 < 0) break;
      出.push(xml.slice(头, 尾 + 闭.length));
      起 = 尾 + 闭.length;
    }
    return 出;
  }

  /** 从开始标签里取属性值。 */
  function _xl属性(标签, 键) {
    const 样 = 键 + '="';
    const 起 = 标签.indexOf(样);
    if (起 < 0) return null;
    const 值起 = 起 + 样.length;
    const 尾 = 标签.indexOf('"', 值起);
    if (尾 < 0) return null;
    return _xl还原(标签.slice(值起, 尾));
  }

  /** `A1` → 0、`AA1` → 26。 */
  function _xl列号(引用) {
    let 列 = 0;
    for (const ch of 引用) {
      if (ch >= 'A' && ch <= 'Z') 列 = 列 * 26 + (ch.charCodeAt(0) - 64);
      else if (ch >= 'a' && ch <= 'z') 列 = 列 * 26 + (ch.charCodeAt(0) - 96);
      else break;
    }
    return 列 - 1;
  }

  /** 一个 `<c>` → 基石值。**没值就是 null（空）**，不是空串。 */
  function _xl单元格(片段, 共享) {
    const 开始 = 片段.slice(0, 片段.indexOf('>') + 1) || 片段;
    const 类型 = _xl属性(开始, 't') || 'n';

    if (类型 === 'inlineStr') {
      const 内 = _xl首个内容(片段, 'is') || '';
      return _xl元素们(内, 't').map((t) => _xl还原(_xl首个内容(t, 't') || '')).join('');
    }

    const v = _xl首个内容(片段, 'v');
    if (v === null) return null;
    const 值 = _xl还原(v);

    if (类型 === 's') {
      const 序 = Number.parseInt(值.trim(), 10);
      if (Number.isNaN(序)) return 值;
      return 序 >= 0 && 序 < 共享.length ? 共享[序] : '';
    }
    if (类型 === 'b') {
      const t = 值.trim();
      return !(t === '0' || t === '' || t.toLowerCase() === 'false');
    }
    if (类型 === 'str' || 类型 === 'e') return 值;
    // 数字：整数给整数（`42` 不该变成 `42` 的浮点形态）
    if (/[.eE]/.test(值)) {
      const f = Number(值.trim());
      return Number.isNaN(f) ? 值 : f;
    }
    const i = Number.parseInt(值.trim(), 10);
    if (!Number.isNaN(i) && String(i) === 值.trim()) return i;
    const f = Number(值.trim());
    return Number.isNaN(f) ? 值 : f;
  }

  function _xl共享串(xml) {
    return _xl元素们(xml, 'si').map((si) =>
      _xl元素们(si, 't').map((t) => _xl还原(_xl首个内容(t, 't') || '')).join(''));
  }

  /** `[[名字, 包内路径], …]`，**顺序就是标签页顺序**（别按字母排）。 */
  function _xl工作表们(条们) {
    const 取 = (名) => {
      const 中 = 条们.find((x) => x.name === 名);
      return 中 ? 中.data.toString('utf8') : null;
    };
    const 原 = 取('xl/workbook.xml');
    if (原 === null) throw R.valueErr('这不是一个 .xlsx 工作簿（里面没有 xl/workbook.xml）');

    const 关系 = new Map();
    const rels = 取('xl/_rels/workbook.xml.rels');
    if (rels !== null) {
      for (const 片段 of _xl元素们(rels, 'Relationship')) {
        const 开始 = 片段.slice(0, 片段.indexOf('>') + 1);
        const id = _xl属性(开始, 'Id');
        const 目标 = _xl属性(开始, 'Target');
        if (!id || !目标) continue;
        const 路径 = 目标.startsWith('/') ? 目标.slice(1) : ('xl/' + 目标);
        关系.set(id, 路径.replace(/\/\.\//g, '/'));
      }
    }

    const 出 = [];
    for (const 片段 of _xl元素们(原, 'sheet')) {
      const 开始 = 片段.slice(0, 片段.indexOf('>') + 1);
      const 名字 = _xl属性(开始, 'name');
      if (名字 === null) continue;
      let 路径 = '';
      for (const 键 of ['r:id', 'id']) {
        const v = _xl属性(开始, 键);
        if (v && 关系.has(v)) { 路径 = 关系.get(v); break; }
      }
      if (!路径) 路径 = `xl/worksheets/sheet${出.length + 1}.xml`;
      出.push([名字, 路径]);
    }
    return 出;
  }

  const EXCEL_FUNCS = {
    '读工作簿': new R.Builtin('读工作簿', (路径) => {
      const p = pText(路径);
      let 字节;
      try {
        字节 = zipBytes(p);
      } catch (e) {
        if (e && (e.code === 'ENOENT' || /ENOENT/.test(String(e.message || '')))) {
          throw R.runErr(`找不到文件「${p}」`);
        }
        throw R.runErr(`打不开文件「${p}」`);
      }
      let 条们;
      try {
        条们 = readZipEntries(字节);
      } catch (e) {
        throw R.valueErr(
          `「${p}」不是一个 .xlsx（Excel 的 .xlsx 是 zip 包；老的 .xls 不支持）`);
      }
      const 表们 = _xl工作表们(条们);
      return new Map([
        ['路径', p],
        ['工作表们', 表们.map((x) => x[0])],
      ]);
    }),

    '读工作表': new R.Builtin('读工作表', (簿, 名) => {
      if (!(簿 instanceof Map) || !簿.has('路径') || !簿.has('工作表们')) {
        throw R.typeErr('「簿」要是 `读工作簿(…)` 的返回值');
      }
      const 名上 = pText(名);
      const p = 簿.get('路径');
      let 条们;
      try {
        条们 = readZipEntries(zipBytes(p));
      } catch (e) {
        throw R.runErr(`找不到文件「${p}」`);
      }
      const 表们 = _xl工作表们(条们);
      const 中 = 表们.find((x) => x[0] === 名上);
      if (!中) {
        const 可用 = 表们.map((x) => `「${x[0]}」`).join('、') || '（一个都没有）';
        throw R.valueErr(`没有叫「${名上}」的工作表。这个工作簿里有：${可用}`);
      }
      const 成员 = 条们.find((x) => x.name === 中[1]);
      if (!成员) throw R.valueErr(`工作簿里找不到「${名上}」的内容（${中[1]}）`);

      const xml = 成员.data.toString('utf8');
      const 共享成员 = 条们.find((x) => x.name === 'xl/sharedStrings.xml');
      const 共享 = 共享成员 ? _xl共享串(共享成员.data.toString('utf8')) : [];

      const 行们 = _xl元素们(xml, 'row');
      if (行们.length === 0) return makeTable([], []);

      // 列数 = 所有行里最宽的那行（表头短了也不丢数据）
      let 列数 = 0;
      for (const 行 of 行们) {
        for (const c of _xl元素们(行, 'c')) {
          const r = _xl属性(c.slice(0, c.indexOf('>') + 1), 'r');
          if (r !== null) 列数 = Math.max(列数, _xl列号(r) + 1);
        }
      }
      if (列数 === 0) 列数 = 1;

      const 数据 = 行们.map((行) => {
        const 值 = new Array(列数).fill(null);
        let 顺位 = 0;
        for (const c of _xl元素们(行, 'c')) {
          const r = _xl属性(c.slice(0, c.indexOf('>') + 1), 'r');
          let 列;
          if (r !== null) 列 = _xl列号(r);
          else {
            // 没有 `r` 的极简生成器 ⇒ 顺着往后放
            let i = 顺位;
            while (i < 列数 && 值[i] !== null) i++;
            列 = i;
          }
          顺位 = 列 + 1;
          if (列 >= 0 && 列 < 列数) 值[列] = _xl单元格(c, 共享);
        }
        return 值;
      });

      // 表头保持**值的类型**（与 Rust 侧一致）：空表头给空串，其余原样。
      const 表头 = 数据[0].map((v) => (v === null ? '' : v));
      return makeTable(表头, 数据.slice(1));
    }),
  };

  return EXCEL_FUNCS;
};
