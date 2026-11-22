//! 安装器内核（M200）：装 / 卸 / 升的**判定**与「带备份执行」。
//!
//! ## 为什么要单起一个文件
//!
//! M171/M172 的安装逻辑长在 `src/bin/packstub.rs` 里（那是「存根」形态），
//! M198 又在外面套了一层 Inno。M200 要**去掉 Inno**、并且要能**升级** ——
//! 而升级里最要紧的三件事（版本怎么比、用户数据怎么留、失败怎么回滚）都是
//! **纯逻辑**，不该埋在某个 bin 的 `main` 里：
//!
//! * 它要能被**判据直接调**（不需要真装一遍，几十毫秒跑完）；
//! * 它要能被**向导**（M200 第二步）与**静默安装**（`/S`）共用 —— 两处各写一份，
//!   「升级时保留用户数据」这种规则迟早分叉，而且分叉了**看不出来**。
//!
//! ## 升级的三条规矩（都是踩过或必然会踩的）
//!
//! 1. **只删「旧清单里有、新清单里没有」的文件**。
//!    用户自己放进安装目录的东西（`data/`、配置）不在旧清单里 ⇒ 天然不碰。
//!    ⚠️ 别用「清空目录再解包」那种写法：那是把用户数据一起删掉。
//! 2. **失败要能回到装之前的样子**，不是「装了一半停在那儿」。
//!    做法是先把整个安装目录复制到**安装目录之外**的备份区，失败就整个还原。
//!    为什么不「只备份会被覆盖的文件」：回滚时还要删掉**新写进去**的文件，
//!    而失败那一刻拿不到「新清单」（包可能连读都没读完）—— 整目录还原最省心。
//! 3. **版本相同就不装**（除非显式 `--重装`）。
//!    双击同一个安装包两次不该产生两次「升级」，更不该把用户数据搞出新副本。

use std::cmp::Ordering;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

/// 包里那份清单的文件名 —— 与 `stdlib/pack.rs` 的 `清单名` 是同一个。
/// （这里重写一遍而不是 `pub(crate) use`：那个是 `const` 且在 `stdlib` 子模块里，
/// 而安装器要在**装完之后**还能读它，语义上是「安装器的输入格式」，不是打包的内部细节。）
pub const 清单名: &str = "清单.json";

// ---------------------------------------------------------------------------
// 版本
// ---------------------------------------------------------------------------

/// 比版本号（`parse_version` 取三元组，与包管理 `packages.rs` **同一套规则**）。
///
/// ⚠️ 这里**故意**不支持 `1.2.3-beta` 那种预发布标记：包管理那边也不支持
/// （`parse_version` 把非数字段当 0）。安装器与包管理给出两套版本语义，
/// 用户会以为是 bug —— 要加就两边一起加。
pub fn 比版本(a: &str, b: &str) -> Ordering {
    let (x1, x2, x3) = crate::packages::parse_version(a);
    let (y1, y2, y3) = crate::packages::parse_version(b);
    (x1, x2, x3).cmp(&(y1, y2, y3))
}

// ---------------------------------------------------------------------------
// 已装的是谁
// ---------------------------------------------------------------------------

/// 一个已经装好的程序（从安装目录里的 `清单.json` 读出来）。
#[derive(Debug, Clone)]
pub struct 已装 {
    pub 名字: String,
    pub 版本: String,
    pub 目录: PathBuf,
    /// 清单里列出的相对路径（`/` 分隔）。**升级时要靠它判断哪些是「我们的文件」**。
    pub 文件们: Vec<String>,
}

/// 读安装目录里的清单。目录不存在 / 没有清单 / 清单坏了 → `None`。
///
/// 📌 「清单坏了」也给 `None`（当全新安装）：报错退出会让用户**装不上**，
/// 而重装恰好能修好它 —— 代价只是用户手放的文件可能被新清单「覆盖同名」，
/// 那本来就该覆盖。
pub fn 读已装(目录: &Path) -> Option<已装> {
    let 文本 = fs::read_to_string(目录.join(清单名)).ok()?;
    let j = crate::json::parse(&文本).ok()?;
    取文(&j, "名字")?;
    let 版本 = 取文(&j, "版本").unwrap_or_default();
    let mut 文件们: Vec<String> = Vec::new();
    if let Some(crate::json::Json::Arr(items)) = j.get("文件们") {
        for it in items {
            if let Some(p) = 取文(it, "路径") {
                文件们.push(p);
            }
        }
    }
    Some(已装 {
        名字: 取文(&j, "名字").unwrap_or_default(),
        版本,
        目录: 目录.to_path_buf(),
        文件们,
    })
}

fn 取文(j: &crate::json::Json, k: &str) -> Option<String> {
    j.get(k)
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
}

// ---------------------------------------------------------------------------
// 该干什么
// ---------------------------------------------------------------------------

/// 这次安装**实际会做什么**。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum 动作 {
    /// 目标目录里没有可识别的安装。
    全新,
    /// 装着旧版本，这次是往上走。
    升级 { 从: String },
    /// 装着更新的版本，这次是往下走（默认**拒绝**）。
    降级 { 从: String },
    /// 版本一样（默认**跳过**）。
    相同,
}

impl 动作 {
    /// 一句话描述（给控制台与向导共用，别两处各写一种说法）。
    pub fn 说明(&self) -> String {
        match self {
            动作::全新 => "全新安装".to_string(),
            动作::升级 { 从 } => format!("从 {从} 升级"),
            动作::降级 { 从 } => format!("从 {从} **降级**"),
            动作::相同 => "已经是这个版本".to_string(),
        }
    }
}

/// 判定这次安装的动作。
///
/// `允许降级` 只在用户显式要的场合为真（`--允许降级` / 向导里点了「仍然安装」）：
/// 降级会丢掉新版本才有的功能，**不能是默认行为**。
pub fn 判断(已装: Option<&已装>, 新版本: &str, 允许降级: bool) -> 动作 {
    let Some(旧) = 已装 else { return 动作::全新 };
    match 比版本(新版本, &旧.版本) {
        Ordering::Greater => 动作::升级 { 从: 旧.版本.clone() },
        Ordering::Equal => 动作::相同,
        Ordering::Less => {
            if 允许降级 {
                动作::降级 { 从: 旧.版本.clone() }
            } else {
                // ⚠️ 调用方看到 `降级` 时**必须**停下来问一句，不能顺手装 ——
                //    这里返回「降级」而不是「全新」，就是为了让调用方看见这件事。
                动作::降级 { 从: 旧.版本.clone() }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// 备份 / 回滚
// ---------------------------------------------------------------------------

/// 一次「带备份的执行」。
///
/// 用法：
/// ```ignore
/// let 备 = 备份::做(&目标, &名字)?;
/// match 真装(...) {
///     Ok(()) => 备.丢弃(),
///     Err(e) => { 备.还原(); Err(e) }
/// }
/// ```
pub struct 备份 {
    源: PathBuf,
    副本: Option<PathBuf>,
}

impl 备份 {
    /// 把安装目录整个复制到**安装目录之外**的备份区。
    ///
    /// 为什么在外面（`%LOCALAPPDATA%\jishi-upgrade-backup\…`）而不是
    /// `<安装目录>\.备份\`：
    /// * 安装目录可能被用户设成只读/网络盘；
    /// * 回滚要「删掉安装目录里所有不在备份里的文件」，备份若在目录里，
    ///   这条规则会把自己删掉 —— 那是最恶心的一类自毁（还看不出原因）。
    pub fn 做(安装目录: &Path, 名字: &str) -> Result<备份, String> {
        if !安装目录.is_dir() {
            return Ok(备份 { 源: 安装目录.to_path_buf(), 副本: None });
        }
        let 副本 = 备份区()?.join(安全名(名字));
        // 上一轮遗留（上次崩在回滚前）直接清掉：它已经没有价值了。
        let _ = fs::remove_dir_all(&副本);
        复刻目录树(安装目录, &副本)
            .map_err(|e| format!("建备份失败（{}）：{e}", 副本.display()))?;
        Ok(备份 { 源: 安装目录.to_path_buf(), 副本: Some(副本) })
    }

    /// 装完了 —— 扔掉备份。
    pub fn 丢弃(mut self) {
        if let Some(副本) = self.副本.take() {
            let _ = fs::remove_dir_all(副本);
        }
    }

    /// 装砸了 —— 把安装目录还原成备份时的样子，然后扔掉备份。
    ///
    /// 返回 `true` = 还原成功；`false` = 还原也失败了（那时**必须**把备份路径
    /// 告诉用户，否则东西就真丢了）。
    pub fn 还原(&self) -> bool {
        let Some(副本) = &self.副本 else {
            // 装之前目录就不存在 ⇒ 回滚 = 把半成品目录删掉
            return fs::remove_dir_all(&self.源).is_ok() || !self.源.exists();
        };
        // 先把安装目录清空（半成品文件不能留），再把备份搬回去。
        // 🔴 不能「先复制再清空」：同名文件会被新写的半成品顶掉。
        if self.源.exists() && fs::remove_dir_all(&self.源).is_err() {
            return false;
        }
        if 复刻目录树(副本, &self.源).is_err() {
            return false;
        }
        let _ = fs::remove_dir_all(副本);
        true
    }

    /// 还原失败时给用户的那句话（带备份在哪）。
    pub fn 备份位置(&self) -> Option<PathBuf> {
        self.副本.clone()
    }
}

impl Drop for 备份 {
    fn drop(&mut self) {
        // ⚠️ **故意不自动删**：正常路径要显式 `丢弃()`，失败路径要显式 `还原()`。
        //    自动删会让「忘了调」变成静默丢数据 —— 而这里的数据是用户装好的程序。
        //    只把「还没处理」这件事记在日志里，别让它无声无息地占着磁盘。
        if let Some(副本) = &self.副本 {
            if 副本.exists() {
                日志(&format!(
                    "备份没用上也没清（下次安装会覆盖）：{}",
                    副本.display()
                ));
            }
        }
    }
}

/// 备份区根目录（`%LOCALAPPDATA%\jishi-upgrade-backup`）。
fn 备份区() -> Result<PathBuf, String> {
    let 基 = std::env::var("LOCALAPPDATA")
        .ok()
        .filter(|s| !s.is_empty())
        .or_else(|| std::env::var("XDG_CACHE_HOME").ok().filter(|s| !s.is_empty()))
        .or_else(|| std::env::var("HOME").ok().map(|h| format!("{h}/.cache")))
        .unwrap_or_else(|| ".".to_string());
    let p = Path::new(&基).join("jishi-upgrade-backup");
    fs::create_dir_all(&p).map_err(|e| format!("建备份区「{}」失败：{e}", p.display()))?;
    Ok(p)
}

/// 把程序名变成安全的目录名（`名字` 里可能有 `/`、空格、中文）。
fn 安全名(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        if c.is_alphanumeric() || c == '-' || c == '_' || c == '.' {
            out.push(c);
        } else {
            out.push('_');
        }
    }
    if out.is_empty() {
        out.push_str("未命名");
    }
    // 加进程号：两个安装器同时跑（用户手快）时不互相覆盖备份
    format!("{out}-{}", std::process::id())
}

/// 递归复制目录树（`fs::copy` 不建目录，得自己来）。
fn 复刻目录树(从: &Path, 到: &Path) -> io::Result<()> {
    fs::create_dir_all(到)?;
    for e in fs::read_dir(从)? {
        let e = e?;
        let 源 = e.path();
        let 目标 = 到.join(e.file_name());
        let ty = e.file_type()?;
        if ty.is_dir() {
            复刻目录树(&源, &目标)?;
        } else if ty.is_file() {
            fs::copy(&源, &目标)?;
        }
        // 符号链接/设备文件跳过：安装目录里不该有，复制它们只会引入歧义
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// 升级时该删哪些文件
// ---------------------------------------------------------------------------

/// 「旧清单里有、新清单里没有」⇒ 升级后要删掉的相对路径。
///
/// 🔴 **只按清单算**：用户自己放进安装目录的文件不在旧清单里 ⇒ 一个都不碰。
/// 这是「升级保留用户数据」的全部实现 —— 不需要另开一个 `data/` 约定。
pub fn 该删的(旧: &已装, 新文件们: &[String]) -> Vec<String> {
    let 新集: std::collections::BTreeSet<&str> = 新文件们.iter().map(|s| s.as_str()).collect();
    旧.文件们
        .iter()
        .filter(|p| !新集.contains(p.as_str()))
        .filter(|p| p.as_str() != 清单名)   // 清单自己要重写，不删
        .cloned()
        .collect()
}

/// 把相对路径（`/` 分隔）变成安装目录下的绝对路径，并**挡住越界**。
///
/// 清单是打包时生成的、理论上可信；但安装器读的是**磁盘上的文件**，
/// 一个被改过的 `清单.json` 里写 `../../Windows/System32/x.dll` 就能让
/// 「升级时删旧文件」变成删系统文件。这条检查只有几行，别省。
pub fn 相对路径(根: &Path, 相对: &str) -> Option<PathBuf> {
    if 相对.is_empty() || 相对.starts_with('/') || 相对.contains(':') {
        return None;
    }
    if 相对.split('/').any(|段| 段 == ".." || 段.is_empty()) {
        return None;
    }
    Some(根.join(相对.replace('/', std::path::MAIN_SEPARATOR_STR)))
}

// ---------------------------------------------------------------------------
// 日志
// ---------------------------------------------------------------------------

/// 安装器的日志（写 `%LOCALAPPDATA%\jishi-upgrade-backup\安装.log`）。
///
/// 为什么要：安装器是**双击就跑、窗口一闪而过**的东西。装砸了以后用户手里
/// 只剩一句「失败了」—— 事后想查「到底哪一步」只能靠日志。
/// 写文件失败一律吞掉（日志不该让安装失败）。
pub fn 日志(行: &str) {
    use std::io::Write;
    let Ok(区) = 备份区() else { return };
    let p = 区.join("安装.log");
    if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(&p) {
        let _ = writeln!(f, "{} {行}", 时间戳());
    }
}

fn 时间戳() -> String {
    // 不引第三方时间库：安装器只要「大概什么时候」，秒级精度的本地时间够用。
    let d = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    format!("[epoch {d}]")
}

// ---------------------------------------------------------------------------
// 单实例
// ---------------------------------------------------------------------------

/// 一次安装的独占锁（`<备份区>/安装.lock`）。
///
/// 为什么要有：用户手快会双击两次安装包；两次同时写同一个目录，
/// 轻则「文件被占用」、重则**一边回滚一边写入**，装完是个混合体。
///
/// 实现故意简单（锁文件 + 陈旧判定）：安装器只活几秒，不需要内核互斥量那么重。
/// * 抢不到 → 返回 `None`，调用方提示「已经有一个在装」并退出；
/// * 文件超过 30 分钟没动 → 当上次崩了留下的，删掉重来。
pub struct 独占 {
    路径: PathBuf,
}

impl 独占 {
    pub fn 抢(名字: &str) -> Option<独占> {
        let 区 = 备份区().ok()?;
        let 路径 = 区.join(format!("{}.lock", 安全名(名字)));
        if let Ok(元) = fs::metadata(&路径) {
            let 陈旧 = 元
                .modified()
                .ok()
                .and_then(|t| t.elapsed().ok())
                .map(|d| d.as_secs() > 30 * 60)
                .unwrap_or(false);
            if !陈旧 {
                return None;
            }
            let _ = fs::remove_file(&路径);
        }
        let f = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&路径)
            .ok()?;
        // 里面写 PID：下次判「陈旧」时人工能看出是谁留下的
        drop(f);
        let _ = fs::write(&路径, format!("{}", std::process::id()));
        Some(独占 { 路径 })
    }
}

impl Drop for 独占 {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.路径);
    }
}

// ---------------------------------------------------------------------------
// 判据用的纯函数小测试
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 版本按数字段比() {
        assert_eq!(比版本("1.0.0", "1.0.0"), Ordering::Equal);
        assert_eq!(比版本("1.0.10", "1.0.9"), Ordering::Greater);
        assert_eq!(比版本("2", "1.9.9"), Ordering::Greater);
        assert_eq!(比版本("1.2", "1.2.0"), Ordering::Equal);
    }

    #[test]
    fn 同版本判相同_高版本判升级_低版本判降级() {
        let 旧 = 已装 {
            名字: "甲".into(),
            版本: "1.2.0".into(),
            目录: PathBuf::from("/x"),
            文件们: vec![],
        };
        assert_eq!(判断(Some(&旧), "1.2.0", false), 动作::相同);
        assert_eq!(判断(Some(&旧), "1.3.0", false), 动作::升级 { 从: "1.2.0".into() });
        assert_eq!(判断(Some(&旧), "1.1.0", false), 动作::降级 { 从: "1.2.0".into() });
        assert_eq!(判断(None, "1.0.0", false), 动作::全新);
    }

    #[test]
    fn 升级只删旧清单里有而新清单没有的() {
        let 旧 = 已装 {
            名字: "甲".into(),
            版本: "1.0".into(),
            目录: PathBuf::from("/x"),
            文件们: vec!["a.jsh".into(), "旧模块.jsh".into(), 清单名.into()],
        };
        let 该删 = 该删的(&旧, &["a.jsh".into(), "新模块.jsh".into()]);
        assert_eq!(该删, vec!["旧模块.jsh".to_string()]);
    }

    #[test]
    fn 清单里的越界路径要被挡住() {
        let 根 = Path::new("/安装");
        assert!(相对路径(根, "../../etc/passwd").is_none());
        assert!(相对路径(根, "a/../../b").is_none());
        assert!(相对路径(根, "C:/Windows/x.dll").is_none());
        assert!(相对路径(根, "/abs/x").is_none());
        assert_eq!(相对路径(根, "a/b.jsh"), Some(根.join("a").join("b.jsh")));
    }
}
