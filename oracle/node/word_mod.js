// 标准库 `Word`（M194）—— Node 宿主。与 Python 侧 `jishi/stdlib/Word.py` 同行为。
//
// `.docx` = zip 包 + 几份 XML。zip 读写复用宿主已有的 `readZipEntries` /
// `crc32`（`excel_mod.js` 就是这么用的），这里自己补一个 **stored 打包**
// 和一段**够用就停**的 XML 扫描。
//
// ⚠️ 三处容易错，都写在下面：
//   1. 段落只算 `body` 的**直接子** `w:p` —— 表格里的 `w:p` 属于表格，
//      混进来用户会看到重复内容；
//   2. `w:gridSpan` 的格子要**重复填**（横向合并）；
//   3. 写出去的包必须带 `w:sectPr`，否则 Word 报「文件已损坏」。
'use strict';

module.exports = function 造Word模块(tools) {
  const { zipBytes, readZipEntries, makeTable, pText, R, crc32 } = tools;

  const W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main';
  const R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';

  const 对齐们 = { 左: 'left', 居中: 'center', 右: 'right', 两端: 'both' };
  const 样式中文 = {
    Heading1: '标题1', Heading2: '标题2', Heading3: '标题3',
    Heading4: '标题4', Heading5: '标题5', Heading6: '标题6',
    Title: '标题', Normal: '正文', Subtitle: '副标题',
  };
  const 样式反向 = {};
  for (const [k, v] of Object.entries(样式中文)) 样式反向[v] = k;

  // -- 小工具 ---------------------------------------------------------------

  function 本地名(s) {
    const i = String(s).lastIndexOf(':');
    return i < 0 ? String(s) : String(s).slice(i + 1);
  }

  function 还原实体(s) {
    if (s.indexOf('&') < 0) return s;
    return s.replace(/&(#x[0-9a-fA-F]+|#[0-9]+|[a-zA-Z]+);/g, (m, g) => {
      if (g === 'amp') return '&';
      if (g === 'lt') return '<';
      if (g === 'gt') return '>';
      if (g === 'quot') return '"';
      if (g === 'apos') return "'";
      if (g[0] === '#') {
        const code = g[1] === 'x' || g[1] === 'X'
          ? parseInt(g.slice(2), 16) : parseInt(g.slice(1), 10);
        if (Number.isFinite(code)) {
          try { return String.fromCodePoint(code); } catch (_) { return m; }
        }
      }
      return m;
    });
  }

  function 转义(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&apos;');
  }

  /** 一段 XML 里的可见文字（`t` 的内容 + `tab` + `br`）。 */
  function 文字(xml) {
    let out = '';
    const re = /<(?:[\w.-]+:)?(t|tab|br)\b[^>]*?(?:\/>|>([\s\S]*?)<\/(?:[\w.-]+:)?\1>)/g;
    let m;
    while ((m = re.exec(xml)) !== null) {
      if (m[1] === 'tab') out += '\t';
      else if (m[1] === 'br') out += '\n';
      else out += 还原实体(m[2] == null ? '' : m[2]);
    }
    return out;
  }

  function 段落样式(xml) {
    const m = /<(?:[\w.-]+:)?pStyle\b[^>]*\b(?:[\w.-]+:)?val\s*=\s*"([^"]*)"/.exec(xml);
    if (!m) return '';
    const id = m[1];
    return Object.prototype.hasOwnProperty.call(样式中文, id) ? 样式中文[id] : id;
  }

  function 取标签体(xml, 名, 起) {
    // 找 `<名 ...>`（不是 `<名Pr>`、`<名Xxx>`），返回 [内文, 结束位置]
    const 开 = new RegExp(`<(?:[\\w.-]+:)?${名}\\b[^>]*?(/?)>`, 'g');
    开.lastIndex = 起 || 0;
    const m = 开.exec(xml);
    if (!m) return null;
    if (m[1] === '/') return ['', 开.lastIndex];
    const 始 = 开.lastIndex;
    const 闭 = new RegExp(`</(?:[\\w.-]+:)?${名}>`, 'g');
    闭.lastIndex = 始;
    const c = 闭.exec(xml);
    if (!c) return [xml.slice(始), xml.length];
    return [xml.slice(始, c.index), 闭.lastIndex];
  }

  /** 把 `<w:tbl>…</w:tbl>` 全挖掉（段落不该把表格里的算进来）。 */
  function 挖掉表格(xml) {
    let out = '';
    let i = 0;
    for (;;) {
      const m = /<(?:[\w.-]+:)?tbl\b[^>]*>/.exec(xml.slice(i));
      if (!m) { out += xml.slice(i); break; }
      const 始 = i + m.index;
      out += xml.slice(i, 始);
      const 体 = 取标签体(xml, 'tbl', 始);
      i = 体 ? 体[1] : xml.length;
    }
    return out;
  }

  function 格子们(tblXml) {
    const 数据 = [];
    let i = 0;
    for (;;) {
      const tr = 取标签体(tblXml, 'tr', i);
      if (!tr) break;
      i = tr[1];
      const 行 = [];
      let j = 0;
      for (;;) {
        const tc = 取标签体(tr[0], 'tc', j);
        if (!tc) break;
        j = tc[1];
        const t = 文字(tc[0]);
        const g = /<(?:[\w.-]+:)?gridSpan\b[^>]*\b(?:[\w.-]+:)?val\s*=\s*"(\d+)"/.exec(tc[0]);
        const 跨 = g ? Math.max(1, parseInt(g[1], 10) || 1) : 1;
        for (let k = 0; k < 跨; k++) 行.push(t);
      }
      数据.push(行);
    }
    return 数据;
  }

  function 表头们(数据) {
    if (数据.length === 0) return [];
    const 见 = {};
    return 数据[0].map((原) => {
      const 基 = 原 === '' ? '列' : String(原);
      if (Object.prototype.hasOwnProperty.call(见, 基)) {
        见[基] += 1;
        return `${基}${见[基] + 1}`;
      }
      见[基] = 0;
      return 基;
    });
  }

  function 找部件(条们, 名) {
    const 低 = 名.toLowerCase();
    for (const e of 条们) if (String(e.name).toLowerCase() === 低) return e.data;
    return null;
  }

  function 读条目(路径) {
    const p = pText(路径);
    let 字节;
    try {
      字节 = zipBytes(p);
    } catch (e) {
      if (e && (e.code === 'ENOENT' || /ENOENT/.test(String(e.message || '')))) {
        throw R.fileErr(`找不到文件「${p}」`);
      }
      throw R.fileErr(`打不开文件「${p}」`);
    }
    try {
      return readZipEntries(字节).map((e) => ({
        name: String(e.name), data: Buffer.from(e.data || e.bytes || []),
      }));
    } catch (e) {
      throw R.typeErr(`「${p}」不是 .docx（它连 zip 包都不是）`,
        undefined, undefined,
        '.docx / .xlsx / .pptx 都是 zip 包；老的 .doc 不是，要用 Word 另存');
    }
  }

  function 取正文(条们, 路径) {
    const b = 找部件(条们, 'word/document.xml');
    if (b === null) {
      throw R.typeErr(`这个 zip 里没有 word/document.xml —— 它不是 Word 文档`,
        undefined, undefined,
        '.docx 的正文一定在 word/document.xml；xlsx / pptx 里没有这个文件');
    }
    return b.toString('utf8');
  }

  // -- 读 -------------------------------------------------------------------

  function 读文档(路径) {
    const 条们 = 读条目(路径);
    const 正文 = 取正文(条们, 路径);

    const 段落 = [];
    const 无表 = 挖掉表格(正文);
    let i = 0;
    for (;;) {
      const p = 取标签体(无表, 'p', i);
      if (!p) break;
      i = p[1];
      段落.push(new Map([
        ['文本', 文字(p[0])],
        ['样式', 段落样式(p[0])],
      ]));
    }

    const 表格们 = [];
    let j = 0;
    for (;;) {
      const t = 取标签体(正文, 'tbl', j);
      if (!t) break;
      j = t[1];
      const 数据 = 格子们(t[0]);
      const headers = 表头们(数据);
      表格们.push(makeTable(headers, 数据.slice(1)));
    }

    let 图片 = 0;
    const re图 = /<(?:[\w.-]+:)?(drawing|pict)\b/g;
    while (re图.exec(正文) !== null) 图片 += 1;

    let 页眉 = '';
    let 页脚 = '';
    for (const e of 条们) {
      const 低 = e.name.toLowerCase();
      if (低.indexOf('word/header') === 0 && 页眉 === '') 页眉 = 文字(e.data.toString('utf8'));
      else if (低.indexOf('word/footer') === 0 && 页脚 === '') 页脚 = 文字(e.data.toString('utf8'));
    }

    return new Map([
      ['段落', 段落],
      ['表格', 表格们],
      ['图片数', 图片],
      ['页眉', 页眉],
      ['页脚', 页脚],
    ]);
  }

  // -- 写 -------------------------------------------------------------------

  function 要文(v) {
    if (!(v instanceof Map) || !v.has('块们')) {
      throw R.typeErr(`这里要一个文档对象（Word.新建() 的返回值），`
        + `给过来的却是「${R.display(v)}」`, undefined, undefined,
        '先 令 文 = Word.新建()，再往它里面加内容');
    }
    return v;
  }

  function 段落块(文本, 样式, 选项) {
    return new Map([
      ['种类', '段落'], ['文本', 文本], ['样式', 样式], ['选项', 选项 || new Map()],
    ]);
  }

  function 段落XML(块) {
    const 样式 = 块.get('样式') || '';
    const 文本 = 块.get('文本') || '';
    const 选项 = 块.get('选项') || new Map();
    let 段属性 = '';
    if (样式 && 样式 !== '正文') {
      段属性 += `<w:pStyle w:val="${样式反向[样式] || 样式}"/>`;
    }
    if (选项.get('对齐') && 对齐们[选项.get('对齐')]) {
      段属性 += `<w:jc w:val="${对齐们[选项.get('对齐')]}"/>`;
    }
    let r属性 = '';
    if (选项.get('粗体')) r属性 += '<w:b/>';
    if (选项.get('斜体')) r属性 += '<w:i/>';
    const 号 = 选项.get('字号');
    if (号) r属性 += `<w:sz w:val="${Math.round(Number(号) * 2)}"/>`;
    let x = '<w:p>';
    if (段属性) x += `<w:pPr>${段属性}</w:pPr>`;
    x += '<w:r>';
    if (r属性) x += `<w:rPr>${r属性}</w:rPr>`;
    x += `<w:t xml:space="preserve">${转义(文本)}</w:t></w:r></w:p>`;
    return x;
  }

  function 表格XML(块) {
    const 表头 = 块.get('表头') || [];
    const 行们 = 块.get('行们') || [];
    const 列数 = 表头.length;
    if (列数 === 0) return '';
    let out = '<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid>';
    for (let i = 0; i < 列数; i++) out += '<w:gridCol w:w="2400"/>';
    out += '</w:tblGrid>';
    const 所有行 = [表头].concat(行们);
    for (const 行 of 所有行) {
      out += '<w:tr>';
      for (let i = 0; i < 列数; i++) {
        const 格 = i < 行.length ? 行[i] : '';
        out += '<w:tc><w:p><w:r><w:t xml:space="preserve">'
          + `${转义(格)}</w:t></w:r></w:p></w:tc>`;
      }
      out += '</w:tr>';
    }
    out += '</w:tbl>';
    return out;
  }

  function 内容类型XML() {
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      + '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
      + '<Default Extension="xml" ContentType="application/xml"/>'
      + '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
      + '</Types>';
  }

  function 关系XML() {
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
      + `<Relationship Id="rId1" Type="${R_NS}/officeDocument" Target="word/document.xml"/>`
      + '</Relationships>';
  }

  function 正文XML(文) {
    let out = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      + `<w:document xmlns:w="${W}"><w:body>`;
    for (const 块 of 文.get('块们')) {
      const 种 = 块.get('种类');
      if (种 === '段落') out += 段落XML(块);
      else if (种 === '表格') out += 表格XML(块);
      else if (种 === '分页') out += '<w:p><w:r><w:br w:type="page"/></w:r></w:p>';
    }
    out += '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
      + '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"'
      + ' w:header="851" w:footer="992" w:gutter="0"/></w:sectPr>';
    out += '</w:body></w:document>';
    return out;
  }

  /** **stored**（不压缩）打包：判据要「写两次字节相同」，deflate 不保证。 */
  function 打包(条目们) {
    const locals = [];
    const centrals = [];
    let offset = 0;
    for (const e of 条目们) {
      const name = Buffer.from(e.name, 'utf8');
      const data = Buffer.isBuffer(e.data) ? e.data : Buffer.from(e.data);
      const crc = crc32(data);
      const lh = Buffer.alloc(30);
      lh.writeUInt32LE(0x04034b50, 0);
      lh.writeUInt16LE(20, 4);
      lh.writeUInt16LE(0, 6);
      lh.writeUInt16LE(0, 8);              // stored
      lh.writeUInt16LE(0, 10);             // 时间 0
      lh.writeUInt16LE(0x21, 12);          // 日期 1980-01-01
      lh.writeUInt32LE(crc, 14);
      lh.writeUInt32LE(data.length, 18);
      lh.writeUInt32LE(data.length, 22);
      lh.writeUInt16LE(name.length, 26);
      lh.writeUInt16LE(0, 28);
      locals.push(lh, name, data);

      const cd = Buffer.alloc(46);
      cd.writeUInt32LE(0x02014b50, 0);
      cd.writeUInt16LE(20, 4);
      cd.writeUInt16LE(20, 6);
      cd.writeUInt16LE(0, 8);
      cd.writeUInt16LE(0, 10);
      cd.writeUInt16LE(0, 12);
      cd.writeUInt16LE(0x21, 14);
      cd.writeUInt32LE(crc, 16);
      cd.writeUInt32LE(data.length, 20);
      cd.writeUInt32LE(data.length, 24);
      cd.writeUInt16LE(name.length, 28);
      cd.writeUInt16LE(0, 30);
      cd.writeUInt16LE(0, 32);
      cd.writeUInt16LE(0, 34);
      cd.writeUInt16LE(0, 36);
      cd.writeUInt32LE(0, 38);
      cd.writeUInt32LE(offset, 42);
      centrals.push(cd, name);
      offset += 30 + name.length + data.length;
    }
    const cdBuf = Buffer.concat(centrals);
    const eocd = Buffer.alloc(22);
    eocd.writeUInt32LE(0x06054b50, 0);
    eocd.writeUInt16LE(0, 4);
    eocd.writeUInt16LE(0, 6);
    eocd.writeUInt16LE(条目们.length, 8);
    eocd.writeUInt16LE(条目们.length, 10);
    eocd.writeUInt32LE(cdBuf.length, 12);
    eocd.writeUInt32LE(offset, 16);
    eocd.writeUInt16LE(0, 20);
    return Buffer.concat(locals.concat([cdBuf, eocd]));
  }

  function 要docx路径(v) {
    const p = pText(v);
    if (!p) throw R.valueErr('保存路径不能是空的');
    if (!p.toLowerCase().endsWith('.docx')) {
      throw R.valueErr(`保存的文件名要以 .docx 结尾，现在是「${p}」`, undefined, undefined,
        '老的 .doc 是另一种格式，写不出来；存成 .docx 就好');
    }
    return p;
  }

  const fs = require('node:fs');

  const WORD_FUNCS = {
    '读文档': new R.Builtin('读文档', (路径) => 读文档(路径)),

    '新建': new R.Builtin('新建', () => new Map([['块们', []]])),

    '加标题': new R.Builtin('加标题', (文, 文本, 级别) => {
      const t = 要文(文);
      const 级 = (级别 === undefined || 级别 === null) ? 1 : 级别;
      if (typeof 级 !== 'number' || !Number.isInteger(级) || 级 < 1 || 级 > 3) {
        throw R.valueErr(`标题级别只能是 1 / 2 / 3，给的是「${R.display(级)}」`);
      }
      t.get('块们').push(段落块(pText(文本), `标题${级}`, new Map()));
      return null;
    }),

    '加段落': new R.Builtin('加段落', (文, 文本, 选项) => {
      const t = 要文(文);
      const 净 = new Map();
      if (选项 !== undefined && 选项 !== null) {
        if (!(选项 instanceof Map)) {
          throw R.typeErr(`「选项」要给一个字典，给过来的却是「${R.display(选项)}」`);
        }
        for (const [k, v] of 选项) {
          const 名 = pText(k);
          if (名 === '粗体' || 名 === '斜体') {
            净.set(名, v === true);
          } else if (名 === '字号') {
            if (typeof v !== 'number') {
              throw R.typeErr(`「字号」要给一个数字（磅），给的是「${R.display(v)}」`);
            }
            if (!(v >= 1 && v <= 200)) {
              throw R.valueErr(`「字号」要在 1 到 200 磅之间，现在是 ${v}`);
            }
            净.set(名, v);
          } else if (名 === '对齐') {
            const s = pText(v);
            if (!对齐们[s]) {
              throw R.valueErr(`「对齐」只能是 ${Object.keys(对齐们).join(' / ')}，给的是「${s}」`);
            }
            净.set(名, s);
          } else {
            throw R.valueErr(`「选项」里没有「${名}」这一项`, undefined, undefined,
              '能写的只有：粗体 / 斜体 / 字号 / 对齐');
          }
        }
      }
      t.get('块们').push(段落块(pText(文本), '正文', 净));
      return null;
    }),

    '加表格': new R.Builtin('加表格', (文, 表) => {
      const t = 要文(文);
      if (!表 || typeof 表 !== 'object' || 表.__jishiType !== '表格') {
        throw R.typeErr(`「加表格」要一个表格（表格.读表格 的返回值），`
          + `给过来的却是「${R.display(表)}」`, undefined, undefined,
          '要自己拼表：先 表格.从记录(...) 再传进来');
      }
      t.get('块们').push(new Map([
        ['种类', '表格'],
        ['表头', 表.headers.map((h) => pText(h))],
        ['行们', 表.rows.map((r) => r.map((c) => pText(c)))],
      ]));
      return null;
    }),

    '加分页': new R.Builtin('加分页', (文) => {
      要文(文).get('块们').push(new Map([['种类', '分页']]));
      return null;
    }),

    '保存': new R.Builtin('保存', (文, 路径) => {
      要文(文);
      const p = 要docx路径(路径);
      const 字节 = 打包([
        { name: '[Content_Types].xml', data: Buffer.from(内容类型XML(), 'utf8') },
        { name: '_rels/.rels', data: Buffer.from(关系XML(), 'utf8') },
        { name: 'word/document.xml', data: Buffer.from(正文XML(文), 'utf8') },
      ]);
      try {
        fs.writeFileSync(p, 字节);
      } catch (e) {
        throw R.fileErr(`没法写入文件「${p}」`);
      }
      return null;
    }),

    '填充模板': new R.Builtin('填充模板', (模板, 路径, 数据) => {
      const p = 要docx路径(路径);
      if (!(数据 instanceof Map)) {
        throw R.typeErr(`「数据」要给一个字典（键 → 值），给过来的却是「${R.display(数据)}」`);
      }
      const 条们 = 读条目(模板);
      const 正文 = 取正文(条们, 模板);
      const 新正文 = 正文.replace(/\{\{([^{}]*)\}\}/g, (m, 键) =>
        (数据.has(键) ? 转义(R.display(数据.get(键))) : m));
      const 字节 = 打包(条们.map((e) =>
        (e.name.toLowerCase() === 'word/document.xml'
          ? { name: e.name, data: Buffer.from(新正文, 'utf8') }
          : { name: e.name, data: e.data })));
      try {
        fs.writeFileSync(p, 字节);
      } catch (e) {
        throw R.fileErr(`没法写入文件「${p}」`);
      }
      return null;
    }),
  };

  return WORD_FUNCS;
};
