//! 一个**够用就停**的 XML 解析器（M194）。
//!
//! ## 为什么不引 crate
//!
//! `.docx` / `.xlsx` / `.tmx` 里的 XML 都是**机器生成**的：标签规整、属性永远
//! 带引号、没有 DTD、没有自定义实体。为一个这样的输入引一个完整 XML 库，
//! 换来的是依赖、编译时间和 wasm 体积，而用到的功能不到它的百分之几。
//!
//! ## 认什么
//!
//! | 认 | 不认（遇到就跳过，不报错） |
//! |---|---|
//! | 元素 / 自闭合元素 | DTD（`<!DOCTYPE …>`） |
//! | 属性（单双引号都行） | 自定义实体（只认五个预定义 + 数字实体） |
//! | 文本 + 五个预定义实体 + `&#nn;` / `&#xNN;` | XML 命名空间的语义（只按**本地名**认标签） |
//! | 注释 / 处理指令 / CDATA | 命名空间作用域、默认命名空间 |
//!
//! ## 为什么按「本地名」认标签
//!
//! `w:p` / `w14:p` / 没前缀的 `p` 是**同一个元素**，前缀本来就是任意取的
//! （Word 有时用 `w:`，有时同一文档里混用别的）。按本地名认，三种写法都能读；
//! 代价是「同名不同命名空间」分不开 —— 对 Office 文档不存在这种冲突。
//!
//! ## 已知的松
//!
//! 标签不闭合时**不报错**，按「遇到闭合就弹栈、最后剩下的一律当根的子」处理。
//! 这样读坏文件不会 panic，但**也不会告诉你文件坏了** —— 判据里因此有一条
//! 「读一个不是 docx 的 zip 要报中文错」（靠的是更外层的检查，不是这里）。

/// 一个元素。`名` / 属性名都是**本地名**（去掉了 `w:` 这样的前缀）。
#[derive(Debug, Clone, Default)]
pub(crate) struct 元素 {
    pub 名: String,
    pub 属性: Vec<(String, String)>,
    pub 子们: Vec<元素>,
    pub 文本: String,
}

impl 元素 {
    pub fn 取属性(&self, 名: &str) -> &str {
        for (k, v) in &self.属性 {
            if k == 名 {
                return v;
            }
        }
        ""
    }

    /// 深度优先遍历（含自己）。
    pub fn 遍历(&self, f: &mut impl FnMut(&元素)) {
        f(self);
        for c in &self.子们 {
            c.遍历(f);
        }
    }

    /// 直接子元素里叫 `名` 的那些。
    pub fn 子们叫(&self, 名: &str) -> Vec<&元素> {
        self.子们.iter().filter(|c| c.名 == 名).collect()
    }

    /// 整棵子树里的文字拼起来（`t` 的内容 + `tab` + `br`）。
    pub fn 全部文字(&self) -> String {
        let mut out = String::new();
        收集文字(self, &mut out);
        out
    }
}

fn 收集文字(el: &元素, out: &mut String) {
    match el.名.as_str() {
        "t" => out.push_str(&el.文本),
        "tab" => out.push('\t'),
        "br" => out.push('\n'),
        _ => {}
    }
    for c in &el.子们 {
        收集文字(c, out);
    }
}

/// 本地名：`w:p` → `p`（这里的前缀已经在解析时去掉了，这个函数是防御性的）。
fn 本地名(s: &str) -> &str {
    match s.rfind(':') {
        Some(i) => &s[i + 1..],
        None => s,
    }
}

fn 实体(s: &str) -> String {
    if !s.contains('&') {
        return s.to_string();
    }
    let mut out = String::with_capacity(s.len());
    let b: Vec<char> = s.chars().collect();
    let mut i = 0;
    while i < b.len() {
        if b[i] != '&' {
            out.push(b[i]);
            i += 1;
            continue;
        }
        // 找分号（最多往后看 12 个字符，防止把整个文档当成一个实体）
        let mut j = i + 1;
        while j < b.len() && j < i + 12 && b[j] != ';' {
            j += 1;
        }
        if j >= b.len() || b[j] != ';' {
            out.push('&');
            i += 1;
            continue;
        }
        let 名: String = b[i + 1..j].iter().collect();
        let 换 = match 名.as_str() {
            "amp" => Some('&'),
            "lt" => Some('<'),
            "gt" => Some('>'),
            "quot" => Some('"'),
            "apos" => Some('\''),
            _ => {
                // 数字实体：&#65; / &#x41;
                if let Some(rest) = 名.strip_prefix('#') {
                    let 码 = if let Some(h) = rest.strip_prefix('x').or_else(|| rest.strip_prefix('X')) {
                        u32::from_str_radix(h, 16).ok()
                    } else {
                        rest.parse::<u32>().ok()
                    };
                    码.and_then(char::from_u32)
                } else {
                    None
                }
            }
        };
        match 换 {
            Some(c) => {
                out.push(c);
                i = j + 1;
            }
            None => {
                // 不认识的实体：**原样留着**（改成空会把内容吃掉）
                out.push('&');
                i += 1;
            }
        }
    }
    out
}

/// 在**字符数组**里从 `i` 开始找 `pat`，返回字符下标。
///
/// 🔴 为什么不用 `s[i..].find(...)`：`i` 是**字符**下标，而 `s[..]` 要**字节**
/// 下标 —— 中文文本（`文字`）会让两者错开，`s[i..]` 直接 panic
/// 「byte index is not a char boundary」。这个坑在第一次跑单测就撞到了。
fn 找(b: &[char], i: usize, pat: &str) -> Option<usize> {
    let p: Vec<char> = pat.chars().collect();
    if p.is_empty() || b.len() < p.len() {
        return None;
    }
    let mut k = i;
    while k + p.len() <= b.len() {
        if b[k..k + p.len()] == p[..] {
            return Some(k);
        }
        k += 1;
    }
    None
}

fn 前缀(b: &[char], i: usize, pat: &str) -> bool {
    let mut k = 0;
    for c in pat.chars() {
        if i + k >= b.len() || b[i + k] != c {
            return false;
        }
        k += 1;
    }
    true
}

/// 解析一份 XML。返回值一定是合法的树（根名叫 `#root` 时说明文件是空的）。
pub(crate) fn 解析(s: &str) -> 元素 {
    let b: Vec<char> = s.chars().collect();
    let mut 栈: Vec<元素> = vec![元素 { 名: "#root".into(), ..Default::default() }];
    let mut i = 0usize;
    let mut 文本 = String::new();

    let 冲文本 = |栈: &mut Vec<元素>, 文本: &mut String| {
        if !文本.is_empty() {
            let t = 实体(文本);
            文本.clear();
            if let Some(top) = 栈.last_mut() {
                top.文本.push_str(&t);
            }
        }
    };

    while i < b.len() {
        if b[i] != '<' {
            文本.push(b[i]);
            i += 1;
            continue;
        }
        // `<!--` 注释
        if 前缀(&b, i, "<!--") {
            match 找(&b, i, "-->") {
                Some(k) => {
                    i = k + 3;
                    continue;
                }
                None => break,
            }
        }
        // `<![CDATA[` —— 里面是**纯文本**，不解析实体
        if 前缀(&b, i, "<![CDATA[") {
            match 找(&b, i, "]]>") {
                Some(k) => {
                    let 内容: String = b[i + 9..k].iter().collect();
                    if let Some(top) = 栈.last_mut() {
                        top.文本.push_str(&内容);
                    }
                    i = k + 3;
                    continue;
                }
                None => break,
            }
        }
        // `<!DOCTYPE …>` 之类：跳到最近的 `>`
        if 前缀(&b, i, "<!") {
            match 找(&b, i, ">") {
                Some(k) => {
                    i = k + 1;
                    continue;
                }
                None => break,
            }
        }
        // `<?xml … ?>` 处理指令
        if 前缀(&b, i, "<?") {
            match 找(&b, i, "?>") {
                Some(k) => {
                    i = k + 2;
                    continue;
                }
                None => break,
            }
        }
        // 闭合标签 `</name>`
        if 前缀(&b, i, "</") {
            冲文本(&mut 栈, &mut 文本);
            match 找(&b, i, ">") {
                Some(k) => {
                    i = k + 1;
                    if 栈.len() > 1 {
                        let 完 = 栈.pop().expect("栈里至少还有根");
                        if let Some(top) = 栈.last_mut() {
                            top.子们.push(完);
                        }
                    }
                    continue;
                }
                None => break,
            }
        }
        // 开标签 `<name attr="v">` / `<name/>`
        冲文本(&mut 栈, &mut 文本);
        match 找(&b, i, ">") {
            Some(k) => {
                let 原: String = b[i + 1..k].iter().collect();
                let 自闭合 = 原.ends_with('/');
                let 体 = if 自闭合 { &原[..原.len() - 1] } else { &原[..] };
                let (名, 属性) = 拆标签(体);
                let el = 元素 { 名, 属性, 子们: Vec::new(), 文本: String::new() };
                i = k + 1;
                if 自闭合 {
                    if let Some(top) = 栈.last_mut() {
                        top.子们.push(el);
                    }
                } else {
                    栈.push(el);
                }
            }
            None => break,
        }
    }
    冲文本(&mut 栈, &mut 文本);
    // 没闭合的标签：一层层弹出来，最后留下的那个当根
    while 栈.len() > 1 {
        let 完 = 栈.pop().expect("栈里至少还有根");
        if let Some(top) = 栈.last_mut() {
            top.子们.push(完);
        }
    }
    let mut 根 = 栈.pop().expect("栈里至少还有根");
    if 根.子们.len() == 1 {
        let mut 真根 = 根.子们.remove(0);
        // 根外面**不该有**非空白文本；真有就丢掉（XML 声明后面可能有换行）
        真根.文本.insert_str(0, "");
        return 真根;
    }
    根.文本 = 根.文本.trim().to_string();
    根
}

/// `<w:p w:val="x"/>` 的主体 → (本地名, [(本地属性名, 值)])
fn 拆标签(s: &str) -> (String, Vec<(String, String)>) {
    let b: Vec<char> = s.chars().collect();
    let mut i = 0;
    // 名字
    while i < b.len() && !b[i].is_whitespace() {
        i += 1;
    }
    let 名 = 本地名(&b[..i].iter().collect::<String>()).to_string();
    let mut 属性 = Vec::new();
    while i < b.len() {
        while i < b.len() && b[i].is_whitespace() {
            i += 1;
        }
        if i >= b.len() {
            break;
        }
        let 起 = i;
        while i < b.len() && b[i] != '=' && !b[i].is_whitespace() {
            i += 1;
        }
        let 键: String = b[起..i].iter().collect();
        while i < b.len() && (b[i].is_whitespace() || b[i] == '=') {
            i += 1;
        }
        if i >= b.len() {
            if !键.is_empty() {
                属性.push((本地名(&键).to_string(), String::new()));
            }
            break;
        }
        let 引 = b[i];
        if 引 != '"' && 引 != '\'' {
            // 没引号的属性值：吃到空白为止（不合规，但不炸）
            let 起2 = i;
            while i < b.len() && !b[i].is_whitespace() {
                i += 1;
            }
            let 值: String = b[起2..i].iter().collect();
            属性.push((本地名(&键).to_string(), 实体(&值)));
            continue;
        }
        i += 1;
        let 起2 = i;
        while i < b.len() && b[i] != 引 {
            i += 1;
        }
        let 值: String = b[起2..i].iter().collect();
        if i < b.len() {
            i += 1;
        }
        if !键.is_empty() {
            属性.push((本地名(&键).to_string(), 实体(&值)));
        }
    }
    (名, 属性)
}

#[cfg(test)]
mod 测试 {
    use super::*;

    #[test]
    fn 基本结构与属性() {
        let r = 解析(r#"<a x="1"><b><t>文字</t></b><c/></a>"#);
        assert_eq!(r.名, "a");
        assert_eq!(r.取属性("x"), "1");
        assert_eq!(r.子们.len(), 2);
        assert_eq!(r.子们[0].全部文字(), "文字");
    }

    #[test]
    fn 实体与数字实体() {
        let r = 解析(r#"<a>A&amp;B &#65; &#x42;</a>"#);
        assert_eq!(r.文本, "A&B A B");
    }

    #[test]
    fn 前缀按本地名认() {
        let r = 解析(r#"<w:p w:val="x"><w:t>hi</w:t></w:p>"#);
        assert_eq!(r.名, "p");
        assert_eq!(r.取属性("val"), "x");
        assert_eq!(r.全部文字(), "hi");
    }

    #[test]
    fn 注释与声明被跳过() {
        let r = 解析("<?xml version=\"1.0\"?><!-- 注释 --><a>1</a>");
        assert_eq!(r.名, "a");
        assert_eq!(r.文本, "1");
    }

    #[test]
    fn 不闭合也不炸() {
        let r = 解析("<a><b>1");
        assert_eq!(r.名, "a");
        assert_eq!(r.子们[0].名, "b");
    }
}
