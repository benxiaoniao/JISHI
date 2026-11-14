// 标准库 `PDF`（M195）—— Node 宿主。与 Python 侧 `jishi/stdlib/PDF.py` 同行为、
// 同字节输出。
//
// PDF 不是 zip 包，而是**一串对象 + 一张交叉引用表（xref）**：文件末尾的
// `startxref` 指向 xref，xref 里记着每个对象在文件里的**字节偏移**。所以写出去
// 的要害不是拼 XML，而是**把每个字节数准** —— 差一个字节，严格的阅读器就说
// 文件坏了。
//
// ## 坐标
//
// PDF 规范原点在左下角、y 向上；屏幕与 `图形` 模块都是左上角、y 向下。
// 这里**对外统一用屏幕坐标**，内部 `y` 翻成 `页高 - y`。
//
// ## 为什么只能写英文
//
// 只用 PDF 自带的 14 种标准字体（不嵌进文件），代价是字符串只能放 Latin-1
// 里的字。中文要嵌字体子集（+1 MB + OFL 声明），这一版不做 ⇒ `写文本` 遇到
// ASCII 以外的字符**直接报错**并让用户改用 `Word`。
//
// ## 三处容易错
//
//   1. 写颜色时**不能拿「0–255 输入」的转换去读已经存好的 0–1 值** —— 会再除
//      一次 255，得到 0.003（Rust 侧第一次就是这么错的，两宿主的字节对不上才发现）；
//   2. `xref` 每条必须正好 **20 字节**（10 位偏移 + 空格 + 5 位世代 + 空格 + n/f + 空格 + LF）；
//   3. 对象流（`/ObjStm`）里的对象要**覆盖**顶层的 —— 打包的对象在顶层本来
//      就不该有实体，不拆会「一页都找不到」。
'use strict';

module.exports = function 造PDF模块(tools) {
  const { R, pText } = tools;
  const A4宽 = 595;
  const A4高 = 842;

  // 索引 = 粗体 * 2 + 斜体，正好对应 PDF 的四种后缀
  const 字体表 = [
    ['Helvetica', 'Helvetica-Oblique', 'Helvetica-Bold', 'Helvetica-BoldOblique'],
    ['Times-Roman', 'Times-Italic', 'Times-Bold', 'Times-BoldItalic'],
    ['Courier', 'Courier-Oblique', 'Courier-Bold', 'Courier-BoldOblique'],
  ];
  const 字体族们 = ['Helvetica', 'Times', 'Courier'];
  const 字体别名 = {
    无衬线: 'Helvetica', 黑体: 'Helvetica',
    衬线: 'Times', 宋体: 'Times',
    等宽: 'Courier', 打字机: 'Courier',
  };

  // -- 取参数 ---------------------------------------------------------------

  function 数取(v, 谁, 名) {
    if (typeof v === 'number') return v;
    if (v instanceof R.FloatBox) return v.value;
    throw R.typeErr(`「${谁}」的 ${名} 要给一个数字，给的是「${pText(v)}」`);
  }

  function 是整数(v) {
    return typeof v === 'number' && Number.isInteger(v);
  }

  function 取字典(d, 键) {
    if (d instanceof Map) return d.has(键) ? d.get(键) : undefined;
    if (d && typeof d === 'object') return d[键];
    return undefined;
  }

  function 真值(v) {
    return v === true;
  }

  function 要文档(v) {
    if (v instanceof Map && v.has('页们')) return v;
    throw R.typeErr(
      `这里要一个 PDF 文档（\`PDF.新建()\` 的返回值），给过来的却是「${pText(v)}」`,
      undefined, undefined,
      '先 令 文 = PDF.新建()，再往它里面写');
  }

  function 当前内容(v) {
    const 文 = 要文档(v);
    const 页们 = 文.get('页们');
    if (!Array.isArray(页们) || 页们.length === 0) {
      throw R.valueErr('文档里一页都没有');
    }
    const 末 = 页们[页们.length - 1];
    return 末.get('内容');
  }

  function 要颜色(v, 谁) {
    if (v === undefined || v === null) return [0, 0, 0];
    if (!Array.isArray(v) || v.length !== 3) {
      throw R.typeErr(
        `「${谁}」的「颜色」要给三个数 [红, 绿, 蓝]（每个 0–255），现在是「${pText(v)}」`);
    }
    const 出 = [];
    for (const x of v) {
      const f = typeof x === 'number' ? x : (x instanceof R.FloatBox ? x.value : NaN);
      if (Number.isNaN(f)) {
        throw R.typeErr(`「${谁}」的颜色分量要是数字，给的是「${pText(x)}」`);
      }
      if (f < 0 || f > 255) {
        throw R.valueErr(`「${谁}」的颜色分量要在 0–255 之间，现在是 ${pText(x)}`);
      }
      出.push(f / 255);
    }
    return 出;
  }

  function 字体名(选项, 谁) {
    const 原名 = 选项 && 选项.has && 选项.has('字体') ? pText(选项.get('字体')) : 'Helvetica';
    const 名 = 字体别名[原名] || 原名;
    const 粗 = 选项 && 选项.has && 选项.has('粗体') ? 真值(选项.get('粗体')) : false;
    const 斜 = 选项 && 选项.has && 选项.has('斜体') ? 真值(选项.get('斜体')) : false;
    for (let i = 0; i < 字体族们.length; i++) {
      if (字体族们[i].toLowerCase() === 名.toLowerCase()) {
        return 字体表[i][(粗 ? 2 : 0) + (斜 ? 1 : 0)];
      }
    }
    throw R.valueErr(
      `「${谁}」不认识的字体「${名}」—— 只能是：Helvetica / Times / Courier`
      + '（也可以写 无衬线 / 衬线 / 等宽）',
      undefined, undefined,
      'PDF 自带的标准字体只有这几种；要别的字体得嵌进文件，这一版不做');
  }

  // 标准字体只装得下 ASCII 可打印字符（PDF 字符串是单字节的）
  function 查ASCII(文本, 谁) {
    const 字表 = Array.from(文本);
    for (let i = 0; i < 字表.length; i++) {
      const c = 字表[i];
      if (c === '\n') continue;
      if (c === '\t') {
        throw R.valueErr(`「${谁}」的文本里有制表符（第 ${i + 1} 个字符）—— `
          + 'PDF 不会自动对齐，请换成空格');
      }
      const o = c.codePointAt(0);
      if (o < 0x20) {
        throw R.valueErr(`「${谁}」的文本里有控制字符 U+${o.toString(16).toUpperCase().padStart(4, '0')}`
          + `（第 ${i + 1} 个字符）`);
      }
      if (o > 0x7e) {
        throw R.valueErr(
          `「${谁}」只写得下英文和常见符号：第 ${i + 1} 个字符是「${c}」`
          + `（U+${o.toString(16).toUpperCase().padStart(4, '0')}），PDF 的 14 种标准字体里没有它`,
          undefined, undefined,
          '要中文报告请用 Word（`Word.加段落`），再由 Word / LibreOffice 导出 PDF');
      }
    }
  }

  // -- 生成 -----------------------------------------------------------------

  // 数字 → PDF 写法：整数不带小数点，小数最多四位（去尾零）。三宿主必须一致。
  function 数(v) {
    const r = Math.round(v * 10000) / 10000;
    if (Number.isInteger(r) && Math.abs(r) < 1e15) return String(r);
    return r.toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
  }

  function 串(s) {
    let o = '(';
    for (const c of s) {
      if (c === '(' || c === ')' || c === '\\') o += '\\';
      o += c;
    }
    return o + ')';
  }

  function 读颜色(操作, 键) {
    const v = 操作.get(键);
    if (!Array.isArray(v) || v.length !== 3) return null;
    return v.map((x) => (typeof x === 'number' ? x : (x instanceof R.FloatBox ? x.value : 0)));
  }

  function 内容流(页, 高, 字体序) {
    const 出 = [];
    const 块们 = 页.get('内容') || [];
    for (const 操作 of 块们) {
      const 种 = 操作.get('种');
      if (种 === '文本') {
        const 文本 = 操作.get('文本') || '';
        const 字体 = 操作.get('字体');
        const 字号 = 取字典(操作, '字号');
        const 行距 = 取字典(操作, '行距');
        const x = 取字典(操作, 'x');
        const y = 取字典(操作, 'y');
        const 色 = 读颜色(操作, '颜色') || [0, 0, 0];
        const fi = 字体序.get(字体) || 1;
        出.push('BT');
        出.push(`/F${fi} ${数(字号)} Tf`);
        出.push(`${数(色[0])} ${数(色[1])} ${数(色[2])} rg`);
        出.push(`1 0 0 1 ${数(x)} ${数(高 - y)} Tm`);
        const 行们 = String(文本).split('\n');
        行们.forEach((行, i) => {
          if (i > 0) 出.push(`0 ${数(-行距)} Td`);
          if (行) 出.push(`${串(行)} Tj`);
        });
        出.push('ET');
      } else if (种 === '线') {
        const 色 = 读颜色(操作, '颜色') || [0, 0, 0];
        const x1 = 取字典(操作, 'x1');
        const y1 = 取字典(操作, 'y1');
        const x2 = 取字典(操作, 'x2');
        const y2 = 取字典(操作, 'y2');
        出.push(`${数(色[0])} ${数(色[1])} ${数(色[2])} RG`);
        出.push(`${数(取字典(操作, '线宽'))} w`);
        出.push(`${数(x1)} ${数(高 - y1)} m ${数(x2)} ${数(高 - y2)} l S`);
      } else if (种 === '矩形') {
        const 填 = 读颜色(操作, '填充');
        if (填) 出.push(`${数(填[0])} ${数(填[1])} ${数(填[2])} rg`);
        const 色 = 读颜色(操作, '颜色') || [0, 0, 0];
        const x = 取字典(操作, 'x');
        const y = 取字典(操作, 'y');
        const w = 取字典(操作, '宽');
        const h = 取字典(操作, '高');
        出.push(`${数(色[0])} ${数(色[1])} ${数(色[2])} RG`);
        出.push(`${数(取字典(操作, '线宽'))} w`);
        出.push(`${数(x)} ${数(高 - y - h)} ${数(w)} ${数(h)} re`);
        出.push(填 ? 'B' : 'S');
      }
    }
    出.push(''); // 保证末尾有换行
    return 出.join('\n');
  }

  function 生成(v) {
    const 文 = 要文档(v);
    const 宽 = 取字典(文, '宽');
    const 高 = 取字典(文, '高');
    const 页们 = 文.get('页们');
    const n = 页们.length;

    // 用到的字体：去重 + 排序（排序是为了逐字节可复现）
    const 用到 = [];
    for (const 页 of 页们) {
      for (const 操作 of (页.get('内容') || [])) {
        if (操作.get('种') === '文本') {
          const f = 操作.get('字体');
          if (!用到.includes(f)) 用到.push(f);
        }
      }
    }
    用到.sort();
    const 字体序 = new Map();
    用到.forEach((名, i) => 字体序.set(名, i + 1));

    const 对象们 = [];
    对象们.push(Buffer.from('<< /Type /Catalog /Pages 2 0 R >>', 'latin1'));
    const 孩子 = [];
    for (let i = 0; i < n; i++) 孩子.push(`${3 + i} 0 R`);
    对象们.push(Buffer.from(`<< /Type /Pages /Kids [${孩子.join(' ')}] /Count ${n} >>`, 'latin1'));
    for (let i = 0; i < n; i++) {
      const 内容号 = 3 + n + i;
      let 字体项 = '';
      if (用到.length) {
        字体项 = ' /Font << ' + 用到
          .map((名, j) => `/F${字体序.get(名)} ${3 + 2 * n + j} 0 R`).join(' ') + ' >>';
      }
      对象们.push(Buffer.from(
        `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${数(宽)} ${数(高)}]`
        + ` /Resources <<${字体项} >> /Contents ${内容号} 0 R >>`, 'latin1'));
    }
    for (const 页 of 页们) {
      const 流 = Buffer.from(内容流(页, 高, 字体序), 'latin1');
      const 头 = Buffer.from(`<< /Length ${流.length} >>\nstream\n`, 'latin1');
      const 尾 = Buffer.from('endstream', 'latin1');
      对象们.push(Buffer.concat([头, 流, 尾]));
    }
    for (const 名 of 用到) {
      对象们.push(Buffer.from(
        `<< /Type /Font /Subtype /Type1 /BaseFont /${名} /Encoding /WinAnsiEncoding >>`, 'latin1'));
    }

    const 段们 = [Buffer.from('%PDF-1.4\n', 'latin1')];
    // 二进制注释：让 FTP / 邮件系统别把文件当文本改坏
    段们.push(Buffer.from([0x25, 0xe2, 0xe3, 0xcf, 0xd3, 0x0a]));
    let 长 = 段们[0].length + 段们[1].length;
    const 偏移们 = [];
    for (let i = 0; i < 对象们.length; i++) {
      偏移们.push(长);
      const 段 = Buffer.concat([
        Buffer.from(`${i + 1} 0 obj\n`, 'latin1'),
        对象们[i],
        Buffer.from('\nendobj\n', 'latin1'),
      ]);
      段们.push(段);
      长 += 段.length;
    }
    const xref位置 = 长;
    const 叉 = [];
    叉.push(Buffer.from(`xref\n0 ${对象们.length + 1}\n`, 'latin1'));
    // 每条正好 20 字节
    叉.push(Buffer.from('0000000000 65535 f \n', 'latin1'));
    for (const 偏 of 偏移们) {
      叉.push(Buffer.from(String(偏).padStart(10, '0') + ' 00000 n \n', 'latin1'));
    }
    叉.push(Buffer.from(`trailer\n<< /Size ${对象们.length + 1} /Root 1 0 R >>\n`, 'latin1'));
    叉.push(Buffer.from(`startxref\n${xref位置}\n`, 'latin1'));
    叉.push(Buffer.from('%%EOF\n', 'latin1'));
    return Buffer.concat(段们.concat(叉));
  }

  // -- 读 -------------------------------------------------------------------

  function 找(数据, 干, 起) {
    const i = 数据.indexOf(干, 起 || 0);
    return i < 0 ? null : i;
  }

  function 整数(数据, i) {
    let j = i;
    while (j < 数据.length && 数据[j] === 0x20) j++;
    const s = j;
    while (j < 数据.length && 数据[j] >= 0x30 && 数据[j] <= 0x39) j++;
    if (j === s) return null;
    return [parseInt(数据.toString('latin1', s, j), 10), j];
  }

  function 拆体(体) {
    const k = 找(体, Buffer.from('stream'), 0);
    if (k === null) return [体, null];
    const 字典 = 体.subarray(0, k);
    let 起 = k + 6;
    if (体[起] === 0x0d && 体[起 + 1] === 0x0a) 起 += 2;
    else if (体[起] === 0x0a || 体[起] === 0x0d) 起 += 1;
    let 终 = null;
    const p = 找(字典, Buffer.from('/Length'), 0);
    if (p !== null) {
      const r = 整数(字典, p + 7);
      if (r) {
        const [长, 尾] = r;
        let q = 尾;
        while (q < 字典.length && 字典[q] === 0x20) q++;
        // `/Length 5 0 R` 是间接引用 ⇒ 退回找 endstream
        const 是引用 = q + 1 < 字典.length
          && 字典[q] >= 0x30 && 字典[q] <= 0x39 && 字典[q + 1] === 0x20;
        if (!是引用 && 长 <= 体.length - 起) 终 = 起 + 长;
      }
    }
    if (终 === null) {
      const j = 找(体, Buffer.from('endstream'), 起);
      终 = j === null ? 体.length : j;
    }
    return [字典, 体.subarray(起, 终)];
  }

  function 解压(字典, 流) {
    if (流 === null || 流 === undefined) return Buffer.alloc(0);
    if (找(字典, Buffer.from('/FlateDecode'), 0) === null) return 流;
    const zlib = require('node:zlib');
    try {
      return zlib.inflateSync(流);
    } catch (e) {
      try {
        return zlib.inflateRawSync(流);
      } catch (e2) {
        throw R.valueErr(`PDF 里的压缩流解不开（${e2.message}）`);
      }
    }
  }

  function 对象表(数据) {
    // 扫 `\d+ \d+ obj`
    const 起们 = [];
    let i = 0;
    while (i < 数据.length) {
      const c = 数据[i];
      if (c >= 0x30 && c <= 0x39 && (i === 0 || !(数据[i - 1] >= 0x30 && 数据[i - 1] <= 0x39))) {
        const s = i;
        let j = i;
        while (j < 数据.length && 数据[j] >= 0x30 && 数据[j] <= 0x39) j++;
        const 号 = parseInt(数据.toString('latin1', s, j), 10);
        let k = j;
        while (k < 数据.length && 数据[k] === 0x20) k++;
        const g = k;
        while (k < 数据.length && 数据[k] >= 0x30 && 数据[k] <= 0x39) k++;
        if (k > g) {
          let m = k;
          while (m < 数据.length && 数据[m] === 0x20) m++;
          if (数据.toString('latin1', m, m + 3) === 'obj') {
            起们.push([s, m + 3, 号]);
            i = m + 3;
            continue;
          }
        }
      }
      i += 1;
    }

    const 表 = new Map();
    for (let k = 0; k < 起们.length; k++) {
      const [, 尾, 号] = 起们[k];
      const 终 = k + 1 < 起们.length ? 起们[k + 1][0] : 数据.length;
      if (!表.has(号)) 表.set(号, 数据.subarray(尾, 终));
    }

    // 拆对象流（PDF 1.5+ 常把页对象压进 /ObjStm）
    const 新增 = [];
    for (const [号, 体] of Array.from(表.entries())) {
      const [字典, 流] = 拆体(体);
      if (找(字典, Buffer.from('/ObjStm'), 0) === null) continue;
      const pn = 找(字典, Buffer.from('/N'), 0);
      const pf = 找(字典, Buffer.from('/First'), 0);
      if (pn === null || pf === null) continue;
      const rn = 整数(字典, pn + 2);
      const rf = 整数(字典, pf + 6);
      if (!rn || !rf) continue;
      const 对数 = rn[0];
      const 首 = rf[0];
      let 解;
      try {
        解 = 解压(字典, 流);
      } catch (e) {
        continue;
      }
      if (首 > 解.length) continue;
      const 头 = 解.toString('latin1', 0, 首).trim().split(/\s+/).filter(Boolean).map(Number);
      const 对们 = [];
      for (let k = 0; k * 2 + 1 < 头.length && k < 对数; k++) {
        对们.push([头[k * 2], 头[k * 2 + 1]]);
      }
      for (let k = 0; k < 对们.length; k++) {
        const [内号, 偏] = 对们[k];
        const 下偏 = k + 1 < 对们.length ? 对们[k + 1][1] : 解.length - 首;
        const a = 首 + 偏;
        const b = Math.min(首 + 下偏, 解.length);
        if (a < b) 新增.push([内号, 解.subarray(a, b)]);
      }
    }
    // 🔴 对象流里的**覆盖**顶层的：打包的对象在顶层本来就不该有实体
    for (const [号, 体] of 新增) 表.set(号, 体);
    return 表;
  }

  function 引用(字典, 键) {
    const p = 找(字典, Buffer.from(键), 0);
    if (p === null) return null;
    const r1 = 整数(字典, p + 键.length);
    if (!r1) return null;
    const r2 = 整数(字典, r1[1]);
    if (!r2) return null;
    let q = r2[1];
    while (q < 字典.length && 字典[q] === 0x20) q++;
    return 字典[q] === 0x52 ? r1[0] : null;   // 'R'
  }

  function 引用们(字典, 键) {
    const p = 找(字典, Buffer.from(键), 0);
    if (p !== null) {
      let q = p + 键.length;
      while (q < 字典.length && 字典[q] === 0x20) q++;
      if (字典[q] === 0x5b) {                 // '['
        const 尾 = 找(字典, Buffer.from(']'), q);
        if (尾 !== null) {
          const 内 = 字典.subarray(q + 1, 尾).toString('latin1');
          const 出 = [];
          const re = /(\d+)\s+\d+\s+R/g;
          let m;
          while ((m = re.exec(内)) !== null) 出.push(parseInt(m[1], 10));
          return 出;
        }
      }
    }
    const 单 = 引用(字典, 键);
    return 单 === null ? [] : [单];
  }

  function 页序(表) {
    let 根 = null;
    const 号们 = Array.from(表.keys()).sort((a, b) => a - b);
    for (const 号 of 号们) {
      const [字典] = 拆体(表.get(号));
      if (找(字典, Buffer.from('/Catalog'), 0) !== null) {
        根 = 引用(字典, '/Pages');
        break;
      }
    }
    if (根 === null || !表.has(根)) {
      // 没有可用的目录：退回按对象号，至少不会一页不给
      return 号们.filter((号) => {
        const [字典] = 拆体(表.get(号));
        return 找(字典, Buffer.from('/Page'), 0) !== null
          && 找(字典, Buffer.from('/Pages'), 0) === null;
      });
    }
    const 出 = [];
    (function 展开(号, 深) {
      if (深 > 12) return;
      const 体 = 表.get(号);
      if (!体) return;
      const [字典] = 拆体(体);
      if (找(字典, Buffer.from('/Pages'), 0) !== null) {
        for (const k of 引用们(字典, '/Kids')) 展开(k, 深 + 1);
      } else {
        出.push(号);
      }
    })(根, 0);
    return 出;
  }

  // -- 内容流词法 -----------------------------------------------------------

  // 词法的分隔符：名字遇到它就结束
  const 分隔 = ' \t\r\n\x0c/[]<>(){}%';
  // 🔴 空白**单独一份**：上表里有 `/`（名字的起始符），拿它当空白会把
  // `/F2` 的斜杠吃掉 —— 名字变成操作符，`Tf` 就认不出字号了
  const 空白 = ' \t\r\n\x0c\x00';

  function 去空白(数据, i) {
    while (i < 数据.length) {
      const c = 数据[i];
      if (空白.includes(String.fromCharCode(c))) i++;
      else if (c === 0x25) {                  // '%' 注释到行尾
        const j = 数据.indexOf(0x0a, i);
        i = j < 0 ? 数据.length : j + 1;
      } else break;
    }
    return i;
  }

  function 读串(数据, i) {
    const 出 = [];
    let 深 = 1;
    i += 1;
    while (i < 数据.length) {
      const c = 数据[i];
      if (c === 0x5c) {                       // 反斜杠
        i += 1;
        if (i >= 数据.length) break;
        const d = 数据[i];
        const 简单 = { 0x6e: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12 };
        if (简单[d] !== undefined) { 出.push(简单[d]); i += 1; } else if (d >= 0x30 && d <= 0x37) {
          let v = 0;
          let k = 0;
          while (k < 3 && i < 数据.length && 数据[i] >= 0x30 && 数据[i] <= 0x37) {
            v = v * 8 + (数据[i] - 0x30);
            i++;
            k++;
          }
          出.push(v & 0xff);
        } else if (d === 0x0a || d === 0x0d) {
          if (d === 0x0d && 数据[i + 1] === 0x0a) i += 1;
          i += 1;
        } else { 出.push(d); i += 1; }
        continue;
      }
      if (c === 0x28) 深 += 1;
      else if (c === 0x29) {
        深 -= 1;
        if (深 === 0) { i += 1; break; }
      }
      出.push(c);
      i += 1;
    }
    return [Buffer.from(出), i];
  }

  function 十六进制(块) {
    let s = '';
    for (const c of 块.toString('latin1')) if (/[0-9A-Fa-f]/.test(c)) s += c;
    if (s.length % 2) s += '0';
    return Buffer.from(s, 'hex');
  }

  function 词们(数据) {
    const 出 = [];
    let i = 0;
    for (;;) {
      i = 去空白(数据, i);
      if (i >= 数据.length) break;
      const c = 数据[i];
      if (c === 0x28) {
        const [s, ni] = 读串(数据, i);
        出.push({ t: '串', v: s });
        i = ni;
        continue;
      }
      if (c === 0x3c) {
        if (数据[i + 1] === 0x3c) { 出.push({ t: '<<' }); i += 2; continue; }
        const j = 数据.indexOf(0x3e, i);
        if (j < 0) break;
        出.push({ t: '串', v: 十六进制(数据.subarray(i + 1, j)) });
        i = j + 1;
        continue;
      }
      if (c === 0x3e) {
        if (数据[i + 1] === 0x3e) { 出.push({ t: '>>' }); i += 2; continue; }
        i += 1;
        continue;
      }
      if (c === 0x5b) {
        // 收到配对的 `]`，中间递归词法
        let 深 = 1;
        let j = i + 1;
        while (j < 数据.length) {
          if (数据[j] === 0x5b) 深 += 1;
          else if (数据[j] === 0x5d) {
            深 -= 1;
            if (深 === 0) break;
          } else if (数据[j] === 0x28) {
            const [, nj] = 读串(数据, j);
            j = nj;
            continue;
          }
          j += 1;
        }
        出.push({ t: '组', v: 词们(数据.subarray(i + 1, Math.min(j, 数据.length))) });
        i = j + 1;
        continue;
      }
      if (c === 0x5d || c === 0x7b || c === 0x7d) { i += 1; continue; }
      if (c === 0x2f) {                       // '/名'
        let j = i + 1;
        while (j < 数据.length && !分隔.includes(String.fromCharCode(数据[j]))) j++;
        出.push({ t: '名', v: 数据.toString('latin1', i + 1, j) });
        i = j;
        continue;
      }
      let j = i;
      while (j < 数据.length && !分隔.includes(String.fromCharCode(数据[j]))) j++;
      if (j === i) { i += 1; continue; }
      const w = 数据.toString('latin1', i, j);
      const f = Number(w);
      出.push(Number.isNaN(f) ? { t: '符', v: w } : { t: '数', v: f });
      i = j;
    }
    return 出;
  }

  // -- ToUnicode CMap -------------------------------------------------------

  function 取十六进制串(s, i) {
    let p = i;
    while (p < s.length && s[p] !== '<') p++;
    if (p >= s.length) return null;
    let q = p + 1;
    while (q < s.length && s[q] !== '>') {
      if (!/[0-9A-Fa-f]/.test(s[q])) return null;
      q++;
    }
    if (q >= s.length) return null;
    return [s.slice(p + 1, q), q + 1];
  }

  function 十六进制串转字节(h) {
    const s = h.length % 2 ? h + '0' : h;
    return Buffer.from(s, 'hex');
  }

  function utf16(h) {
    if (!h) return '';
    let b = 十六进制串转字节(h);
    if (b.length % 2) b = Buffer.concat([b, Buffer.from([0])]);
    let 出 = '';
    for (let i = 0; i + 1 < b.length; i += 2) 出 += String.fromCharCode((b[i] << 8) | b[i + 1]);
    return 出;
  }

  function 加一(s, k) {
    const 字表 = Array.from(s);
    if (!字表.length) return s;
    const 末 = 字表.pop();
    return 字表.join('') + String.fromCodePoint(末.codePointAt(0) + k);
  }

  function 定宽(v, 宽) {
    const w = Math.max(宽, 1);
    const 出 = Buffer.alloc(w);
    for (let i = 0; i < w; i++) 出[w - 1 - i] = (v >> (8 * i)) & 0xff;
    return 出;
  }

  function cmap(数据) {
    const 文 = 数据.toString('latin1');
    const 出 = new Map();
    let 找位 = 0;
    for (;;) {
      const a = 文.indexOf('beginbfchar', 找位);
      if (a < 0) break;
      const 起 = a + 'beginbfchar'.length;
      let 终 = 文.indexOf('endbfchar', 起);
      if (终 < 0) 终 = 文.length;
      找位 = 终 + 1;
      const 块 = 文.slice(起, 终);
      let i = 0;
      for (;;) {
        const r1 = 取十六进制串(块, i);
        if (!r1) break;
        const r2 = 取十六进制串(块, r1[1]);
        if (!r2) break;
        const k = 十六进制串转字节(r1[0]);
        if (k.length) 出.set(k.toString('hex'), utf16(r2[0]));
        i = r2[1];
      }
    }
    找位 = 0;
    for (;;) {
      const a = 文.indexOf('beginbfrange', 找位);
      if (a < 0) break;
      const 起 = a + 'beginbfrange'.length;
      let 终 = 文.indexOf('endbfrange', 起);
      if (终 < 0) 终 = 文.length;
      找位 = 终 + 1;
      const 块 = 文.slice(起, 终);
      let i = 0;
      for (;;) {
        const r1 = 取十六进制串(块, i);
        if (!r1) break;
        const r2 = 取十六进制串(块, r1[1]);
        if (!r2) break;
        const 宽 = Math.ceil(r1[0].length / 2);
        const 低 = parseInt(r1[0], 16);
        const 高 = parseInt(r2[0], 16);
        let p = r2[1];
        while (p < 块.length && /\s/.test(块[p])) p++;
        if (块[p] === '[') {
          let q = p + 1;
          let k = 0;
          for (;;) {
            while (q < 块.length && /\s/.test(块[q])) q++;
            if (q >= 块.length || 块[q] === ']') { q += 1; break; }
            const r3 = 取十六进制串(块, q);
            if (!r3) break;
            出.set(定宽(低 + k, 宽).toString('hex'), utf16(r3[0]));
            k += 1;
            q = r3[1];
          }
          i = q;
        } else {
          const r3 = 取十六进制串(块, p);
          if (!r3) break;
          if (高 >= 低 && 高 - 低 <= 65535) {
            for (let k = 0; k <= 高 - 低; k++) {
              出.set(定宽(低 + k, 宽).toString('hex'), 加一(utf16(r3[0]), k));
            }
          }
          i = r3[1];
        }
      }
    }
    return 出;
  }

  function 解码串(字节, 表) {
    if (!表 || 表.size === 0) return 字节.toString('latin1');
    let 出 = '';
    let i = 0;
    while (i < 字节.length) {
      let 命中 = false;
      for (const 宽 of [2, 1]) {
        if (i + 宽 <= 字节.length) {
          // Map 的键是 hex 字符串（Buffer 按引用比会永远查不到）
          const s = 表.get(字节.subarray(i, i + 宽).toString('hex'));
          if (s !== undefined) { 出 += s; i += 宽; 命中 = true; break; }
        }
      }
      if (!命中) { 出 += String.fromCharCode(字节[i]); i += 1; }
    }
    return 出;
  }

  function 页的cmap(表, 页字典) {
    const 文 = 页字典.toString('latin1');
    const re = /\/(F\w+)\s+(\d+)\s+\d+\s+R/g;
    let m;
    while ((m = re.exec(文)) !== null) {
      const 号 = parseInt(m[2], 10);
      const 体 = 表.get(号);
      if (!体) continue;
      const [字] = 拆体(体);
      const 映 = 引用(字, '/ToUnicode');
      if (映 === null) continue;
      const 体2 = 表.get(映);
      if (!体2) continue;
      const [字2, 流2] = 拆体(体2);
      if (!流2) continue;
      return cmap(解压(字2, 流2));
    }
    return new Map();
  }

  function 文本行们(词表, 表) {
    const 行们 = [];
    let 当前 = '';
    let 上y = null;
    let x = 0;
    let y = 0;
    let 行距 = 12;
    let 操作数 = [];

    function 落行() {
      const 文 = 当前.trim();
      if (文) 行们.push(文);
      当前 = '';
    }

    function 收(字节) {
      const 文 = 解码串(字节, 表);
      if (!文) return;
      if (上y !== null && Math.abs(y - 上y) > 1) 落行();
      上y = y;
      当前 += 文;
    }

    for (const w of 词表) {
      if (w.t !== '符') { 操作数.push(w); continue; }
      const op = w.v;
      const 数们 = 操作数.filter((t) => t.t === '数').map((t) => t.v);
      if (op === 'Tm' && 数们.length >= 6) {
        x = 数们[数们.length - 2];
        y = 数们[数们.length - 1];
      } else if ((op === 'Td' || op === 'TD') && 数们.length >= 2) {
        x += 数们[数们.length - 2];
        y += 数们[数们.length - 1];
        if (op === 'TD') 行距 = -数们[数们.length - 1];
      } else if (op === 'T*') {
        y -= 行距;
      } else if (op === 'Tf' && 数们.length >= 1) {
        行距 = 数们[数们.length - 1] * 1.2;
      } else if (op === 'Tj') {
        for (let i = 操作数.length - 1; i >= 0; i--) {
          if (操作数[i].t === '串') { 收(操作数[i].v); break; }
        }
      } else if (op === "'" || op === '"') {
        y -= 行距;
        for (let i = 操作数.length - 1; i >= 0; i--) {
          if (操作数[i].t === '串') { 收(操作数[i].v); break; }
        }
      } else if (op === 'TJ') {
        for (let i = 操作数.length - 1; i >= 0; i--) {
          if (操作数[i].t === '组') {
            for (const x2 of 操作数[i].v) {
              if (x2.t === '串') 收(x2.v);
              // TJ 里的负数是**字距加宽**，大到一定程度才当空格
              else if (x2.t === '数' && x2.v < -100) 当前 += ' ';
            }
            break;
          }
        }
      }
      操作数 = [];
    }
    落行();
    return 行们;
  }

  function 页们文(数据) {
    const 表 = 对象表(数据);
    const 序 = 页序(表);
    const 出 = [];
    for (const 号 of 序) {
      const 体 = 表.get(号) || Buffer.alloc(0);
      const [字典] = 拆体(体);
      const 映 = 页的cmap(表, 字典);
      const 行们 = [];
      for (const 流号 of 引用们(字典, '/Contents')) {
        const 体2 = 表.get(流号);
        if (!体2) continue;
        const [字, 流] = 拆体(体2);
        const 解 = 解压(字, 流);
        if (解.length) 行们.push(...文本行们(词们(解), 映));
      }
      出.push(行们);
    }
    return 出;
  }

  function 打开(路径) {
    const fs = require('node:fs');
    const p = pText(路径);
    let 数据;
    try {
      数据 = fs.readFileSync(p);
    } catch (e) {
      // 三宿主的文案要一致：**不存在**单独一句
      if (e.code === 'ENOENT') throw R.fileErr(`找不到文件「${p}」`);
      throw R.fileErr(`读不了「${p}」：${e.message}`);
    }
    if (数据.toString('latin1', 0, 5) !== '%PDF-') {
      throw R.valueErr(`「${p}」不是 PDF 文件（开头不是 %PDF-）`, undefined, undefined,
        'Word 文档要读就用 Word.读文档；.doc 是老格式，不是 PDF');
    }
    const 页们 = 页们文(数据);
    if (页们.length === 0) {
      throw R.valueErr(`「${p}」里一页都没找到 —— 文件可能不完整`, undefined, undefined,
        '用阅读器打开确认一下；能打开的话把文件发出来看');
    }
    if (!页们.some((行) => 行.length)) {
      throw R.valueErr(
        `这个 PDF 没有文本层（${页们.length} 页里一个字都读不出来）—— 大概是扫描件`,
        undefined, undefined,
        '扫描件要 OCR 才行，这一版不做；要提文字请先用别的工具转成文本');
    }
    return 页们;
  }

  // -- 内建 -----------------------------------------------------------------

  return {
    新建: new R.Builtin('新建', (宽v, 高v) => {
      const 宽 = (宽v === undefined || 宽v === null) ? A4宽 : 数取(宽v, '新建', '宽');
      const 高 = (高v === undefined || 高v === null) ? A4高 : 数取(高v, '新建', '高');
      if (宽 < 1 || 宽 > 20000) throw R.valueErr(`页宽要在 1 到 20000 点之间，现在是 ${宽}`);
      if (高 < 1 || 高 > 20000) throw R.valueErr(`页高要在 1 到 20000 点之间，现在是 ${高}`);
      return new Map([
        ['宽', new R.FloatBox(宽)],
        ['高', new R.FloatBox(高)],
        ['页们', [new Map([['内容', []]])]],
      ]);
    }),

    加页: new R.Builtin('加页', (文档) => {
      const 文 = 要文档(文档);
      const 页们 = 文.get('页们');
      页们.push(new Map([['内容', []]]));
      return 页们.length;
    }),

    写文本: new R.Builtin('写文本', (文档, 文本, xv, yv, 选项v) => {
      const 文 = pText(文本);
      查ASCII(文, '写文本');
      let 选项;
      if (选项v === undefined || 选项v === null) 选项 = new Map();
      else if (选项v instanceof Map) 选项 = 选项v;
      else throw R.typeErr(`「选项」要给一个字典（比如 {"字号": 20}），给的是「${pText(选项v)}」`);

      const 字号 = 选项.has('字号') ? 数取(选项.get('字号'), '字号', '字号') : 12;
      if (字号 < 1 || 字号 > 400) throw R.valueErr(`「字号」要在 1 到 400 点之间，现在是 ${字号}`);
      const 行距 = 选项.has('行距') ? 数取(选项.get('行距'), '行距', '行距') : 字号 * 1.4;
      if (行距 <= 0) throw R.valueErr('「行距」要大于 0');
      const x = (xv === undefined || xv === null) ? 72 : 数取(xv, '写文本', 'x');
      const y = (yv === undefined || yv === null) ? 72 : 数取(yv, '写文本', 'y');
      const 字体 = 字体名(选项, '写文本');
      const 色 = 要颜色(选项.has('颜色') ? 选项.get('颜色') : undefined, '写文本');
      当前内容(文档).push(new Map([
        ['种', '文本'], ['文本', 文], ['x', new R.FloatBox(x)], ['y', new R.FloatBox(y)],
        ['字体', 字体], ['字号', new R.FloatBox(字号)], ['行距', new R.FloatBox(行距)],
        ['颜色', 色.map((c) => new R.FloatBox(c))],
      ]));
      return null;
    }),

    画线: new R.Builtin('画线', (文档, x1, y1, x2, y2, 选项v) => {
      let 选项 = 选项v;
      if (选项 === undefined || 选项 === null) 选项 = new Map();
      else if (!(选项 instanceof Map)) {
        throw R.typeErr(`「选项」要给一个字典，给的是「${pText(选项)}」`);
      }
      const 线宽 = 选项.has('线宽') ? 数取(选项.get('线宽'), '画线', '线宽') : 1;
      if (!(线宽 > 0 && 线宽 <= 100)) {
        throw R.valueErr(`「线宽」要在 0 到 100 点之间，现在是 ${线宽}`);
      }
      const 色 = 要颜色(选项.has('颜色') ? 选项.get('颜色') : undefined, '画线');
      当前内容(文档).push(new Map([
        ['种', '线'],
        ['x1', new R.FloatBox(数取(x1, '画线', 'x1'))],
        ['y1', new R.FloatBox(数取(y1, '画线', 'y1'))],
        ['x2', new R.FloatBox(数取(x2, '画线', 'x2'))],
        ['y2', new R.FloatBox(数取(y2, '画线', 'y2'))],
        ['线宽', new R.FloatBox(线宽)],
        ['颜色', 色.map((c) => new R.FloatBox(c))],
      ]));
      return null;
    }),

    画矩形: new R.Builtin('画矩形', (文档, x, y, w, h, 选项v) => {
      let 选项 = 选项v;
      if (选项 === undefined || 选项 === null) 选项 = new Map();
      else if (!(选项 instanceof Map)) {
        throw R.typeErr(`「选项」要给一个字典，给的是「${pText(选项)}」`);
      }
      const 线宽 = 选项.has('线宽') ? 数取(选项.get('线宽'), '画矩形', '线宽') : 1;
      if (!(线宽 > 0 && 线宽 <= 100)) {
        throw R.valueErr(`「线宽」要在 0 到 100 点之间，现在是 ${线宽}`);
      }
      const 宽 = 数取(w, '画矩形', '宽');
      const 高 = 数取(h, '画矩形', '高');
      if (宽 <= 0 || 高 <= 0) throw R.valueErr('「画矩形」的宽和高要大于 0');
      const 色 = 要颜色(选项.has('颜色') ? 选项.get('颜色') : undefined, '画矩形');
      const 填充v = 选项.has('填充') ? 选项.get('填充') : undefined;
      const 填充 = 要颜色(填充v, '画矩形').map((c) => new R.FloatBox(c));
      当前内容(文档).push(new Map([
        ['种', '矩形'],
        ['x', new R.FloatBox(数取(x, '画矩形', 'x'))],
        ['y', new R.FloatBox(数取(y, '画矩形', 'y'))],
        ['宽', new R.FloatBox(宽)],
        ['高', new R.FloatBox(高)],
        ['线宽', new R.FloatBox(线宽)],
        ['颜色', 色.map((c) => new R.FloatBox(c))],
        ['填充', 填充v === undefined || 填充v === null ? null : 填充],
      ]));
      return null;
    }),

    保存: new R.Builtin('保存', (文档, 路径) => {
      const fs = require('node:fs');
      const p = pText(路径);
      if (!p) throw R.valueErr('保存路径不能是空的');
      if (!p.toLowerCase().endsWith('.pdf')) {
        throw R.valueErr(`保存的文件名要以 .pdf 结尾，现在是「${p}」`);
      }
      const 数据 = 生成(文档);
      try {
        fs.writeFileSync(p, 数据);
      } catch (e) {
        throw R.fileErr(`写不了「${p}」：${e.message}`);
      }
      return null;
    }),

    页数: new R.Builtin('页数', (路径) => 打开(路径).length),

    读文本: new R.Builtin('读文本', (路径) => {
      const 页们 = 打开(路径);
      return new Map([['页数', 页们.length], ['页们', 页们]]);
    }),
  };
};
