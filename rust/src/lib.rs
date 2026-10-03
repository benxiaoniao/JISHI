//! 基石（jishi）中文编程语言的 Rust 字节码解释器（M13.3）。
//!
//! 消费 M13.1 产出的 JSON 字节码，脱离 Python 独立执行基石代码。
//! 语义与 Python VM / JS VM 对齐。

use std::cell::{Ref, RefCell, RefMut};
use std::io::BufRead;
use std::collections::HashMap;
use std::rc::Rc;
use std::sync::atomic::{AtomicUsize, Ordering as AtomicOrdering};

mod crypto;
mod http;
mod inflate;
mod json;
mod regex;
mod stdlib;
// M51：标准库函数签名表（由 `tools/gen_stdlib_sigs.py` 从 Python 侧
// jishi/stdlib/*.py 生成）。命名参数要按「模块.函数」查它才能映射回位置参数。
mod stdlib_sigs;
mod zip;
use json::Json;

/// 宿主认的字节码格式版本 —— **四处同步**：`jishi/serialize.py` 的
/// `FORMAT_VERSION`、`core/src/serialize.rs` 的 `FORMAT_VERSION`、
/// `node/index.js` 的 `FORMAT_VERSION`，以及这里。改一处就要改四处。
/// 3: R6.6 起 Code 增加 `local_hints`（按槽位的「赋值前读局部」提示）
pub const BYTECODE_VERSION: i64 = 3;

mod decimal;
use decimal::{BigInt, Dec, Round};

// ---------------------------------------------------------------------------
// 值表示
// ---------------------------------------------------------------------------
//
// 列表与字典用 `Rc<RefCell<…>>` 包住（M28）：**基石里的容器是引用类型**，
// `令 b = a` 之后 `b.追加(3)` 必须让 `a` 也变（Python / JS 侧都是这个语义，
// C VM 有自己的引用计数）。
//
// 以前这里是裸 `Vec<Val>`，而 `Val` 是值类型、`Clone` 是深拷贝——于是
// `a.追加(3)` 改的是**副本**，原变量纹丝不动：不报错、结果就是错的。
// 推导式（内部走 `列表.追加`）因此返回空列表。
//
// 换成 `Rc<RefCell<…>>` 之后，`Val::clone` 只复制指针，同一份数据被共享，
// 方法内的修改就能被原变量看见。

/// 基石列表（共享可变）
pub(crate) type JList = Rc<RefCell<Vec<Val>>>;
/// 基石字典（共享可变，保持插入顺序 + O(1) 查找）。
///
/// `items` 保持插入顺序（迭代、`键()`、`值()`、显示、`==` 都依赖它）；
/// `index` 给「可哈希的键」提供 O(1) 定位。
///
/// ⚠️ 为什么不是 `HashMap<Val, _>`：`Val` 的 `==` 是**跨类型数值相等**
/// （`1 == 1.0 == 精确("1")` 都是真），而浮点 / 精确小数 / 整数的哈希天然不同——
/// 直接给 `Val` 实现 `Hash` 会「哈希与相等不一致」（两个相等的键落到不同桶，
/// 字典就查不到了）。所以哈希键单独规范化成 `DictKey`（见 `dict_key`）。
/// ⚠️ 也不能只有 `HashMap` 没有 `items`：基石字典对齐 Python 的**有序** dict，
/// 迭代顺序必须确定（同一程序两次运行输出相同）。
#[derive(Clone)]
pub(crate) struct Dict {
    items: Vec<(Val, Val)>,
    index: HashMap<DictKey, usize>,
}

/// 字典键的「可哈希规范化」—— 保证 `==` 相等的两个值哈希到**同一个** `DictKey`。
#[derive(Hash, PartialEq, Eq, Clone)]
enum DictKey {
    None_,
    /// `Int` / 整数 `Float`（|f|<2^53 且 fract==0）/ **布尔**都归一到这。
    ///
    /// 归一到一起的判据是 `Val::eq`：`1 == 1.0`、`真 == 1`、`假 == 0`
    /// 都是真 → 它们的哈希键必须相同（哈希与相等必须一致）。
    Int(i128),
    Str(String),
    /// **非整数浮点**（`1.5` / `±inf`）。存 `to_bits()`，`-0.0` 已归一成正零。
    ///
    /// ⚠️ **只有集合用它**（`set_key`）—— 字典**不能**：精确小数可以做字典键，
    /// 而 `1.5 == 精确("1.5")` 为真；浮点走索引、精确小数回退线性的活，
    /// 「同一个键」会裂成两个。集合不收精确小数（`push` 就拒了），没这顾虑。
    Float(u64),
}
pub(crate) type JDict = Rc<RefCell<Dict>>;

impl Dict {
    /// 按插入顺序迭代（与旧的 `Vec` 版 `.iter()` 同形，减少调用点改动）。
    fn iter(&self) -> std::slice::Iter<'_, (Val, Val)> {
        self.items.iter()
    }
    fn iter_mut(&mut self) -> std::slice::IterMut<'_, (Val, Val)> {
        self.items.iter_mut()
    }
    fn is_empty(&self) -> bool {
        self.items.is_empty()
    }
    fn len(&self) -> usize {
        self.items.len()
    }
    fn clear(&mut self) {
        self.items.clear();
        self.index.clear();
    }
    /// 拆出底层有序项（**放弃索引**）。调用方要对项做重排 / 消费时用——
    /// 重排之后 `index` 就失效了，不能再当字典用（所以是**消费**掉）。
    pub(crate) fn into_items(self) -> Vec<(Val, Val)> {
        self.items
    }
    /// 克隆出有序项（不动索引）。
    pub(crate) fn to_items(&self) -> Vec<(Val, Val)> {
        self.items.clone()
    }
}

/// `for (k, v) in &字典` 也能走（按插入顺序）。
impl<'a> IntoIterator for &'a Dict {
    type Item = &'a (Val, Val);
    type IntoIter = std::slice::Iter<'a, (Val, Val)>;
    fn into_iter(self) -> Self::IntoIter {
        self.items.iter()
    }
}

impl IntoIterator for Dict {
    type Item = (Val, Val);
    type IntoIter = std::vec::IntoIter<(Val, Val)>;
    fn into_iter(self) -> Self::IntoIter {
        self.items.into_iter()
    }
}

/// 字典 `==` 与 Python 一致：**顺序无关**，按「键相等 + 对应值相等」判。
/// ⚠️ 旧实现直接比 `Vec<(Val,Val)>`（**顺序相关**）—— `{1:2,3:4} == {3:4,1:2}`
/// 会给出 `false`，而 Python / JS / C 侧都给 `真`。顺手修掉这个跨语言分叉。
impl PartialEq for Dict {
    fn eq(&self, other: &Self) -> bool {
        if self.items.len() != other.items.len() {
            return false;
        }
        self.items.iter().all(|(k, v)| {
            dict_get(other, k).map(|ov| &ov == v).unwrap_or(false)
        })
    }
}
/// 基石集合（共享可变，**按插入顺序**去重——对齐 Python 侧 JishiSet：
/// 迭代顺序必须确定，**不能依赖哈希种子**，否则同一程序两次运行输出不同）。
///
/// `items` 保序（迭代 / 显示 / `转列表` / `==` 都依赖它），`index` 让
/// `包含` / `添加` 的去重 / `移除` 的定位从 O(n) 降到 O(1)。
///
/// ⚠️ **`items` 里可能有空洞**（`None`）：保序集合删掉中间某个元素之后，
/// 其余元素的顺序不能变，把整个尾巴前移是 O(n)，于是「删 n 个」就是 O(n²)。
/// 改成**打墓碑**（置 `None`）+ **空洞过半时压缩一次** → 摊还 O(1) 一次删除。
/// 压缩会重建 `items` **和** `index`（下标全变了，两者必须一起重建）。
///
/// ⚠️ 为什么不是 `HashSet<Val>`：见 `Dict` 的同一段注释（`==` 是跨类型数值
/// 相等 → 不能给 `Val` 直接实现 `Hash`）；另外这里还多一条 **顺序必须确定**。
pub(crate) struct Set {
    items: Vec<Option<Val>>,
    /// 空洞个数（`len()` 要减掉）。
    holes: usize,
    index: HashMap<DictKey, usize>,
}

impl Set {
    fn new() -> Set {
        Set { items: Vec::new(), holes: 0, index: HashMap::new() }
    }
    /// 按插入顺序迭代，**跳过空洞**。
    fn iter(&self) -> impl Iterator<Item = &Val> {
        self.items.iter().filter_map(|slot| slot.as_ref())
    }
    fn len(&self) -> usize {
        self.items.len() - self.holes
    }
    fn is_empty(&self) -> bool {
        self.len() == 0
    }
    fn clear(&mut self) {
        self.items.clear();
        self.holes = 0;
        self.index.clear();
    }
    /// 元素是否在集合里。
    fn contains(&self, v: &Val) -> bool {
        self.position(v).is_some()
    }
    /// 元素在 `items` 里的下标（不在则 `None`）。索引里的下标**一定指向实体**
    /// （删除时同步摘掉索引项），所以槽位是 `Some`。
    fn position(&self, v: &Val) -> Option<usize> {
        match set_key(v) {
            Some(k) => self.index.get(&k).copied(),
            // 只有 NaN 走到这（见 `set_key`）：线性比，`NaN != NaN` → 永远找不到 ✓
            None => self.items.iter().position(|slot| slot.as_ref().is_some_and(|x| x == v)),
        }
    }
    /// 放入一个元素（已存在就忽略）。类型检查见 `set_push`。
    fn push(&mut self, v: Val) -> Result<(), JishiError> {
        set_push(self, v)
    }
    /// 删掉一个元素，返回是否真删掉了。
    fn remove(&mut self, v: &Val) -> bool {
        let pos = match self.position(v) {
            Some(p) => p,
            None => return false,
        };
        // 打墓碑：**不移动**后面的元素（保序 + O(1)）
        self.items[pos] = None;
        self.holes += 1;
        if let Some(k) = set_key(v) {
            self.index.remove(&k);
        }
        // 空洞过半才压（于是 n 次删除里大约压一次，摊还 O(1) 每次）
        if self.holes * 2 > self.items.len() {
            self.compact();
        }
        true
    }
    /// 压掉空洞：重建 `items` 与 `index`（下标全变，必须一起重建）。
    fn compact(&mut self) {
        let mut items: Vec<Option<Val>> = Vec::with_capacity(self.len());
        let mut index: HashMap<DictKey, usize> = HashMap::with_capacity(self.index.len());
        for slot in self.items.drain(..) {
            if let Some(v) = slot {
                if let Some(k) = set_key(&v) {
                    index.insert(k, items.len());
                }
                items.push(Some(v));
            }
        }
        self.items = items;
        self.holes = 0;
        self.index = index;
    }
    pub(crate) fn to_items(&self) -> Vec<Val> {
        self.iter().cloned().collect()
    }
}

pub(crate) type JSet = Rc<RefCell<Set>>;

/// 给「一堆值」建临时索引，用于集合运算（并 / 交 / 差 / 对称差）的成员判断。
///
/// 为什么不直接用 `Set`：这些运算的另一个操作数是 `as_iterable` 来的任意
/// `Vec<Val>`，**可能含容器 / 精确小数**（那些不能进集合，但**可以**参与比较——
/// 老实现就是拿 `any(|y| y == x)` 逐个比的）。直接建 `Set` 会平白多报一个
/// 「不能放进集合」的错，是**语义变更**。
///
/// 所以这里只把**可哈希**的放进 `index`，其余留在 `rest` 线性比 ——
/// 两者合起来与逐个 `==` 等价（`set_key` 与 `Val::eq` 一致），
/// 而正常输入（元素都是数字 / 文本）走全索引，O(n+m)。
struct Lookup<'a> {
    index: HashMap<DictKey, usize>,
    rest: Vec<&'a Val>,
}

impl<'a> Lookup<'a> {
    fn build(vals: &'a [Val]) -> Lookup<'a> {
        let mut index = HashMap::new();
        let mut rest = Vec::new();
        for (i, v) in vals.iter().enumerate() {
            match set_key(v) {
                // 重复元素保留第一个（判断「在不在」无影响，但要确定）
                Some(k) => { index.entry(k).or_insert(i); }
                None => rest.push(v),
            }
        }
        Lookup { index, rest }
    }
    fn contains(&self, v: &Val) -> bool {
        match set_key(v) {
            Some(k) => self.index.contains_key(&k),
            None => self.rest.iter().any(|y| *y == v),
        }
    }
}
/// 基石实例（共享可变）。必须是共享的：`自身.字段 = 值` 走 SET_ATTR，
/// 拿到的是 `Val` 的一份拷贝——不共享的话就等于改副本，**不报错、静默失效**。
type JInst = Rc<RefCell<Instance>>;
/// 闭包单元（共享可变，M54 修）。
///
/// ⚠️ **必须共享**，与 `JList` / `JInst` 同一个道理，但踩的坑更深：
/// Python / Node 侧 cell 是**对象**，`LOAD_CELL` 压的是它本身、
/// `STORE_DEREF` 改的是**它里面的值**；Rust 侧原先是 `Vec<Cell>`（值语义），
/// 于是 `LOAD_CELL` 克隆一份、`MAKE_FUNCTION` 把闭包单元的**值**拷进 `Func`、
/// `STORE_DEREF` 又**整体替换**帧里的那个 —— 闭包拿着的是**独立快照**。
///
/// 后果：**「先创建闭包、后给它赋值」永远失效**。最典型的写法就是
/// **嵌套的递归函数**（编译器先 `LOAD_CELL` 捕获自己的单元、再
/// `STORE_DEREF` 填入函数体），于是内层函数看不见自己 →
/// `「空」不能调用`。顶层递归没事（走 `LOAD_GLOBAL`），所以极难发现。
/// 详见 `docs/设计决策.md` D46。
pub(crate) type JCell = Rc<RefCell<Cell>>;

#[derive(Clone)]
pub(crate) enum Val {
    None_,
    Bool(bool),
    // i128 而不是 i64（M32）：i64 装不下 `2 ** 62`、`2 ** 64` 这些常见写法，
    // 而且溢出会**静默回绕**（`9223372036854775807 + 1` 变成负数）——那是最
    // 危险的一类错。i128 覆盖到约 1.7e38，超出时明确报错而不是给错值。
    // 真正的任意精度整数留待需要时再做（Python/JS 侧是任意精度的）。
    Int(i128),
    Float(f64),
    Str(String),
    /// 精确小数（M29）。`Rc` 只是为了让 `Val::clone` 便宜；内容不可变。
    Dec(Rc<Dec>),
    /// 日期时间（M33）：`日期.解析` 的返回值。
    ///
    /// Python 侧返回的是 `datetime`，所以这里也得是个独立类型——
    /// 显示成 `2026-09-01 00:00:00`、`类型()` 给 `datetime`。
    /// 拿 `Val::Str` 糊弄会让 `类型()` 给出「文本」，三个宿主就对不上了。
    /// 日期只到秒，且**没有时区**（与 Python 的 naive datetime 一致）。
    Date(Rc<crate::stdlib::datetime::Dt>),
    /// 切片（M33）：`a[1:3]` 编译成 BUILD_SLICE + GET_ITEM。
    ///
    /// 三个分量都可能缺省（`a[:2]` 的起点、`a[::2]` 的终点…），所以是
    /// `Option`——缺省与「显式给 0」在负步长下含义完全不同。
    Slice(Rc<SliceVal>),
    /// 表格（M33）：`表格.读表格` 的返回值。
    ///
    /// Python 侧是个 `_Table` 实例，显示 `<表格 3 行 x 2 列>`。
    /// 用字典代替的话，显示与 `类型()` 就都对不上了。
    Table(Rc<crate::stdlib::tables::Table>),
    List(JList),
    Dict(JDict),
    Set(JSet),
    File(Rc<RefCell<FileHandle>>),
    Func(Func),
    Class(Class),
    Instance(JInst),
    Module(String, HashMap<String, Val>),
    Builtin(&'static str, fn(&[Val], &mut VM) -> Result<Val, JishiError>),
    ExcType(String),
    /// `html.原样` 的标记（M50）：这段内容是**已经生成好的 HTML**，
    /// 放进标签里时不再转义。
    ///
    /// ⚠️ 为什么不是「字符串子类」：Python 侧最初用 `str` 子类实现，但 M47 把
    /// 文本改成 C 侧原生字符串之后，**子类身份在 C VM 下会丢**（`c2py` 直接
    /// 读字节再 decode 成普通 `str`）——于是 C VM 会把本该原样输出的片段
    /// **再转义一遍**，而且不报错。三个执行器 + 两个宿主都统一用「独立类型」。
    HtmlRaw(String),
    /// 循环信号（`中断`/`继续`）在「穿过 finally」时的临时载体。
    ///
    /// 与 Python 侧把 `RunBreak`/`RunContinue` 当异常对象在块栈里传递同一手法：
    /// 循环信号要先经过 finally 代码，再到外层循环，中间得能放在值栈上。
    /// 1 = 中断，2 = 继续。
    LoopSignal(i64),
    BoundMethod(Box<BoundMethod>),
    Cell(JCell),
    /// `超()` 的返回值（M56）。见 `SuperVal`。
    Super(Rc<SuperVal>),
    /// 被 `捕获` 住的异常（M56）。见 `ExcValue`。
    Exc(Rc<ExcValue>),
}

/// 造一个列表值（把裸 Vec 包成共享容器）。
pub(crate) fn list_new(items: Vec<Val>) -> Val {
    Val::List(Rc::new(RefCell::new(items)))
}

/// 造一个字典值。
pub(crate) fn dict_new(pairs: Vec<(Val, Val)>) -> Val {
    let mut d = Dict { items: Vec::with_capacity(pairs.len()), index: HashMap::new() };
    for (k, v) in pairs {
        dict_set(&mut d, k, v);
    }
    Val::Dict(Rc::new(RefCell::new(d)))
}

/// f64 能精确表示整数的上界（2^53）。超出这个范围的 `Int`/`Float` 键
/// 在 `==` 上会因「转 f64 舍入」而不再传递，哈希表无法一致地处理它们 ——
/// 于是回退到线性扫描（正确但慢，且这种键极罕见）。
const INT_KEY_LIMIT: i128 = 1 << 53;

/// 把字典键规范化成可哈希的 `DictKey`。
///
/// 返回 `None` 表示「不可哈希」（精确小数 / 容器 / 实例 / 超大数值 / NaN 等），
/// 这些键回退到**线性扫描**。覆盖的整数 / 浮点整数 / 文本 / 布尔 / 空
/// 才是真实程序里字典键的主体。
fn dict_key(v: &Val) -> Option<DictKey> {
    match v {
        Val::None_ => Some(DictKey::None_),
        // 布尔按数字归键：`真 == 1`、`假 == 0` 为真（见 `Val::eq`），
        // 哈希必须跟着一致，否则 `d[真]` 与 `d[1]` 会裂成两个键。
        // Node 侧的 `setKey` 一直这么做（注释里写着「Python 里 True == 1」）。
        Val::Bool(b) => Some(DictKey::Int(if *b { 1 } else { 0 })),
        Val::Int(i) if i.abs() < INT_KEY_LIMIT => Some(DictKey::Int(*i)),
        Val::Float(f) => {
            // `-0.0 == 0.0` 为真，所以先归一到正零
            let f = if *f == 0.0 { 0.0 } else { *f };
            if f.is_finite() && f.fract() == 0.0 && f.abs() < INT_KEY_LIMIT as f64 {
                Some(DictKey::Int(f as i128))
            } else {
                None
            }
        }
        Val::Str(s) => Some(DictKey::Str(s.clone())),
        _ => None,
    }
}

/// 集合元素的「可哈希规范化」。
///
/// 与 `dict_key` 只差一处：**非整数浮点也进索引**（`DictKey::Float`）。
/// 字典不能这么做 —— 精确小数可以做字典键，而 `1.5 == 精确("1.5")` 为真，
/// 浮点走索引、精确小数回退线性的话，「同一个键」会裂成两个。集合在 `push`
/// 里就拒收精确小数 / 容器，所以那条顾虑不存在，可以**全量走索引**。
///
/// 返回 `None` 的唯一情形是 `NaN` —— `NaN != NaN`，两个 NaN 是**不同的元素**，
/// 给它任何哈希键都会把「不相等的值」当成同一个（线性比较不会有这个问题）。
fn set_key(v: &Val) -> Option<DictKey> {
    match v {
        Val::None_ => Some(DictKey::None_),
        Val::Bool(b) => Some(DictKey::Int(if *b { 1 } else { 0 })),  // 见 `dict_key`
        Val::Int(i) => Some(DictKey::Int(*i)),
        Val::Float(f) => {
            if f.is_nan() {
                return None;
            }
            // `-0.0 == 0.0` 为真 → 先归一到正零（否则两个「相等」的值落不同桶）
            let f = if *f == 0.0 { 0.0 } else { *f };
            if f.fract() == 0.0 && f.abs() < INT_KEY_LIMIT as f64 {
                Some(DictKey::Int(f as i128))     // `1.0` 与 `1` 同键
            } else {
                Some(DictKey::Float(f.to_bits()))  // `1.5` / `±inf`
            }
        }
        Val::Str(s) => Some(DictKey::Str(s.clone())),
        _ => None,
    }
}

/// 字典赋值：键已存在就**就地替换**（位置不变），否则追加。
/// 与 Python 的 dict 语义一致：插入顺序保持，重复赋值不改变位置。
fn dict_set(d: &mut Dict, k: Val, v: Val) {
    if let Some(key) = dict_key(&k) {
        if let Some(&pos) = d.index.get(&key) {
            d.items[pos].1 = v;
            return;
        }
        let pos = d.items.len();
        d.items.push((k, v));
        d.index.insert(key, pos);
    } else {
        if let Some(slot) = d.items.iter_mut().find(|(ek, _)| *ek == k) {
            slot.1 = v;
        } else {
            d.items.push((k, v));
        }
    }
}

/// 字典取值：可哈希键走 O(1)，其余线性扫描。没找到返回 None。
fn dict_get(d: &Dict, k: &Val) -> Option<Val> {
    if let Some(key) = dict_key(k) {
        return d.index.get(&key).map(|&pos| d.items[pos].1.clone());
    }
    d.items.iter().find(|(ek, _)| ek == k).map(|(_, v)| v.clone())
}

fn dict_contains(d: &Dict, k: &Val) -> bool {
    if let Some(key) = dict_key(k) {
        return d.index.contains_key(&key);
    }
    d.items.iter().any(|(ek, _)| ek == k)
}

/// 删一个键，返回被删的值；没有返回 None。
/// ⚠️ 删除会让 `items` 里后面的元素前移，所以 `index` 里 >pos 的位置都要 -1。
fn dict_remove(d: &mut Dict, k: &Val) -> Option<Val> {
    let key = dict_key(k);
    let pos = match key {
        Some(kk) => {
            let pos = *d.index.get(&kk)?;
            d.index.remove(&kk);
            pos
        }
        None => d.items.iter().position(|(ek, _)| ek == k)?,
    };
    let removed = d.items.remove(pos).1;
    if pos < d.items.len() {
        for idx in d.index.values_mut() {
            if *idx > pos {
                *idx -= 1;
            }
        }
    }
    Some(removed)
}

/// 切片对象（M33）。
#[derive(Clone, Debug)]
pub(crate) struct SliceVal {
    pub(crate) start: Option<i128>,
    pub(crate) stop: Option<i128>,
    pub(crate) step: Option<i128>,
}

/// 把切片换算成实际下标 `(start, stop, step)`——对齐 CPython 的
/// `PySlice_AdjustIndices`：负值先加长度，再按方向裁到边界。
fn slice_indices(sl: &SliceVal, len: usize) -> Result<(i128, i128, i128), JishiError> {
    let step = sl.step.unwrap_or(1);
    if step == 0 {
        return Err(err("数值错误", "切片的步长不能为 0"));
    }
    let len = len as i128;
    // 正步长时下界是 0、上界是 len；负步长时反着来（最容易写错的一处）
    let lower: i128 = if step > 0 { 0 } else { -1 };
    let upper: i128 = if step > 0 { len } else { len - 1 };
    let norm = |v: Option<i128>, dflt: i128| -> i128 {
        match v {
            None => dflt,
            Some(mut x) => {
                if x < 0 {
                    x += len;
                    if x < lower { x = lower; }
                } else if x > upper {
                    x = upper;
                }
                x
            }
        }
    };
    let start = norm(sl.start, if step > 0 { lower } else { upper });
    let stop = norm(sl.stop, if step > 0 { upper } else { lower });
    Ok((start, stop, step))
}

/// 切片覆盖到的下标序列。
fn slice_positions(start: i128, stop: i128, step: i128) -> Vec<i128> {
    let mut out = Vec::new();
    let mut i = start;
    if step > 0 {
        while i < stop {
            out.push(i);
            i += step;
        }
    } else {
        while i > stop {
            out.push(i);
            i += step;
        }
    }
    out
}

fn slice_repr(sl: &SliceVal) -> String {
    let f = |v: Option<i128>| match v {
        Some(x) => x.to_string(),
        None => "空".to_string(),
    };
    format!("切片({}, {}, {})", f(sl.start), f(sl.stop), f(sl.step))
}

/// 造一个集合值（按插入顺序去重，元素重复就丢掉后来的）。
pub(crate) fn set_new(items: Vec<Val>) -> Result<Val, JishiError> {
    let mut out = Set::new();
    for it in items {
        set_push(&mut out, it)?;
    }
    Ok(Val::Set(Rc::new(RefCell::new(out))))
}

/// 往集合里放一个元素（已存在就忽略）。
///
/// 哪些东西能进集合，对齐 Python 侧 `JishiSet._add`：内建 `set` 靠哈希，
/// 所以**列表/字典/集合放不进去**；精确小数也放不进去（Python 侧
/// `__hash__ = None`）。函数/类在 Rust 里是值类型、没有稳定身份，
/// 同样明确拒绝——静默塞进去会让 `2 在 集合` 出现「有时查得到有时查不到」。
pub(crate) fn set_push(s: &mut Set, v: Val) -> Result<(), JishiError> {
    match &v {
        Val::List(_) | Val::Dict(_) | Val::Set(_) | Val::Dec(_) => {
            // ⚠️ 文案与 Python 侧 `JishiSet._add` **逐字相同**：message 短、
            // 该怎么写放在 **hint** 里（渲染成单独一行「提示：…」）。
            // 以前这里把 hint 揉进 message、且少了后半句「列表和字典放不进去」
            // —— 同一件事三个执行器三个说法（Node 又是第三个变体）。R6 主体撞出。
            return Err(err_with_hint(
                "类型错误",
                format!("「{}」不能放进集合", display(&v)),
                "集合的元素要是不可变的（数字、文本…）；列表和字典放不进去".to_string()))
        }
        Val::Func(_) | Val::Class(_) | Val::Builtin(_, _)
        | Val::BoundMethod(_) | Val::Cell(_) => {
            return Err(err("类型错误",
                format!("「{}」不能放进集合（Rust 宿主没有可用的身份）", display(&v))))
        }
        _ => {}
    }
    let key = set_key(&v);
    let dup = match &key {
        Some(k) => s.index.contains_key(k),
        None => s.items.iter().any(|slot| slot.as_ref().is_some_and(|x| *x == v)),
    };
    if !dup {
        s.items.push(Some(v));
        if let Some(k) = key {
            let pos = s.items.len() - 1;
            s.index.insert(k, pos);
        }
    }
    Ok(())
}

/// 列表内容的只读借用（不是列表则给中文错误）。
fn list_ro(v: &Val) -> Result<Ref<'_, Vec<Val>>, JishiError> {
    match v {
        Val::List(l) => Ok(l.borrow()),
        _ => Err(err("类型错误", "需要列表")),
    }
}

#[derive(Clone)]
struct Cell {
    value: Val,
    set: bool,
}

/// 造一个空单元（`set=false` = 还没赋过值，`LOAD_DEREF` 要报「找不到名字」）。
fn new_cell() -> JCell {
    Rc::new(RefCell::new(Cell { value: Val::None_, set: false }))
}

/// `n` 个各自独立的空单元（**不是** `vec![new_cell(); n]` —— 那会共享一个）。
fn new_cells(n: usize) -> Vec<JCell> {
    (0..n).map(|_| new_cell()).collect()
}

/// 类与函数的身份序号（M32）。
///
/// 它们在 Rust 里是值类型、`Clone` 是深拷贝，**没有指针身份**；但
/// `A 是 A` 该为真、`A 是 B` 该为假。所以创建时分配一个递增序号，Clone 时
/// 跟着复制——内容相同且**源自同一次创建**的两个值就有了同一个身份，
/// 正好等价于 Python 的对象身份（`id()`）。
static NEXT_OBJ_ID: AtomicUsize = AtomicUsize::new(1);

fn next_obj_id() -> usize {
    NEXT_OBJ_ID.fetch_add(1, AtomicOrdering::Relaxed)
}

#[derive(Clone)]
struct Func {
    name: String,
    code_idx: usize,
    /// 闭包捕获的**自由变量单元**（共享，见 `JCell`）。`Rc` 克隆 = 共享同一个。
    cells: Vec<JCell>,
    defaults: Option<Vec<Val>>,
    /// 身份序号（M32）：见 `NEXT_OBJ_ID`
    id: usize,
}

#[derive(Clone)]
struct Class {
    name: String,
    methods: HashMap<String, Val>,
    /// 类变量（M34 补上 M23.3）。
    ///
    /// 必须是 `Rc<RefCell<…>>`：`Instance` 里存的是 `Class` 的**值拷贝**，
    /// 用普通 `HashMap` 的话 `类名.计数 = 5` 只改到类自己那一份，已经造出来的
    /// 实例看不到（Python 侧实例持的是类引用，看得见）。共享一份才一致
    /// ——与 M28/M29 给容器、实例换 `Rc<RefCell>` 是同一个理由。
    class_vars: Rc<RefCell<HashMap<String, Val>>>,
    /// **祖先类的身份序号**（最近的在最前），M55 补。
    ///
    /// 为什么需要：Rust 侧建类时是「**把基类的方法表展开合并**」（见
    /// `BUILD_CLASS`），基类指针**用完就丢** —— 于是 `是实例(对象, 基类)`
    /// 没法沿继承链判断。这里把链上的序号记下来（建类时算一次，之后是纯读），
    /// 就能用 `id` 做身份比较 —— 与 Python 侧 `_instance_of` 同语义。
    base_ids: Vec<usize>,
    /// 基类**本身**（M56 补 `超()`）。
    ///
    /// 为什么 M56 才加：`超()` 在 M55 之前**两个宿主根本没实现**（一写就报
    /// 「找不到名字「超」」），所以「基类指针丢了」一直没人发现 —— M55 建
    /// 内建漂移检测时还把它当成「编译器形式、宿主不需要有它」而**排除在比对
    /// 之外**（见 `tests/test_m51_consistency.py` 的旧 `BUILTIN_EXEMPT`）。
    /// 其实编译器把 `超().方法(x)` 脱糖成 `超(自身, "定义类名")`，字节码里
    /// 就是 `LOAD_GLOBAL 超` + `CALL` —— 它是个**货真价实的运行期内建**。
    ///
    /// `超()` 的语义是「从**定义类的基类**起查方法」，所以必须有这条链。
    /// 用 `Rc` 共享：子类与 `超` 视图拿的是同一个基类对象，不深拷贝。
    base: Option<Rc<Class>>,
    /// 身份序号（M32）：见 `NEXT_OBJ_ID`
    id: usize,
}

/// `超()` 的返回值（M56）：从**定义类的基类**起查方法，方法绑定原实例。
///
/// ⚠️ 与 Python 侧 `JishiSuper` 同语义：在 `狗` 的方法里写 `超()`，
/// 只会往 `动物` 及更上层找，**不会又回到 `狗` 自己**（否则就是自递归）。
struct SuperVal {
    instance: JInst,
    /// 定义类的基类 —— 查找从这里开始
    base: Rc<Class>,
    /// 定义类的名字（只为报错文案）
    defining: String,
}

/// 被 `捕获` 住的异常（M56）。
///
/// 以前捕获时只压了一个 `Val::ExcType(类型名)`（源码注释自认是「简化为一个
/// 字符串标记」），于是：
///   · `打印(e)` 只给「类型错误」—— **消息丢了**；
///   · `e.类型` / `e.消息` **报「没有属性」**（Python 与 Node 都能读）；
///   · `类型(e)` 给「异常类型」而 Python / Node 给「异常」。
///
/// 与 Python 侧 `JishiError`、Node 侧 `JishiError` 对齐：异常在宿主里也该是
/// **带内容的对象**，而不是一个类型名字符串。
/// ⚠️ 仍与它们有差距（如实记）：Python 版渲染成多行「错误 E2002：…」+ 行列，
/// 宿主这边是单行「错误（类型）：消息」。**Rust 侧行列本来就没有**
/// （`JishiError` 只有 `type_name` / `message` 两个字段）—— 那是 M56 第 5 条
/// 「宿主错误码与行列」的活，这里只把**消息**补回来。
struct ExcValue {
    /// 基石异常类型名（`类型错误` / `索引错误`…）—— `捕获 X` 与 `e.类型` 都用它。
    type_name: String,
    message: String,
    /// 错误码（M56）：从抛出的 `JishiError` 带过来，`打印(e)` 时与 Python 同形。
    code: Option<String>,
}

#[derive(Clone)]
struct Instance {
    class: Class,
    fields: HashMap<String, Val>,
}

// ---------------------------------------------------------------------------
// 文件对象（M29）
// ---------------------------------------------------------------------------

/// `打开(路径, 模式)` 的返回值。
///
/// 刻意做成**真句柄**而不是一次读进内存：大文件能按行遍历、不必整个装进
/// 列表，而且「用完要关」这件事才有意义——这正是上下文管理器存在的理由。
///
/// 自带 `关闭` 方法（所以能当上下文管理器），也支持 `遍历 行 在 f`。
struct FileHandle {
    file: std::fs::File,
    path: String,
    /// 中文模式名（打印显示用）
    mode: String,
    closed: bool,
    /// 读缓冲：按行遍历不必逐字节去要系统调用
    rbuf: Vec<u8>,
    rpos: usize,
    /// 已消费的**字符**数（M56）—— `位置` / `定位` 的口径。
    ///
    /// 以前 `tell` 给的是**字节偏移**，于是非 ASCII 上三侧各给各的
    /// （Python 给文本 cookie、Node 给字符数、这里给字节数）。
    /// 口径按 M54 在 Node 侧定下的那个：**字符数**
    /// （`tests/test_m26_lang.py::test_file_position_and_seek` 写的就是
    /// 「读 2 个字符 → 位置 = 2」）。`读(n)` 读 n 个字符、位置数到 n，自洽。
    cpos: i64,
    readable: bool,
    writable: bool,
}

impl FileHandle {
    fn state(&self) -> String {
        if self.closed { "已关闭".to_string() } else { self.mode.clone() }
    }

    fn check(&self, name: &str) -> Result<(), JishiError> {
        if self.closed {
            return Err(err("值错误", format!(
                "文件「{}」已经关闭了，不能再「{name}」", self.path)));
        }
        Ok(())
    }

    fn check_read(&self, name: &str) -> Result<(), JishiError> {
        self.check(name)?;
        if !self.readable {
            return Err(err("值错误", format!(
                "文件「{}」是以「{}」模式打开的，不能读", self.path, self.mode)));
        }
        Ok(())
    }

    fn check_write(&self, name: &str) -> Result<(), JishiError> {
        self.check(name)?;
        if !self.writable {
            return Err(err("值错误", format!(
                "文件「{}」是以「{}」模式打开的，不能写", self.path, self.mode)));
        }
        Ok(())
    }

    /// 再读一批进缓冲（顺手把已消费的前缀丢掉）。返回读到的字节数。
    fn fill(&mut self) -> Result<usize, JishiError> {
        if self.rpos > 0 {
            self.rbuf.drain(..self.rpos);
            self.rpos = 0;
        }
        let mut chunk = [0u8; 8192];
        let n = std::io::Read::read(&mut self.file, &mut chunk)
            .map_err(|e| io_fail(&e, &self.path))?;
        self.rbuf.extend_from_slice(&chunk[..n]);
        Ok(n)
    }

    /// 读一「行」（不含行尾换行）。到末尾给 None。
    fn read_line(&mut self) -> Result<Option<String>, JishiError> {
        loop {
            if let Some(i) = self.rbuf[self.rpos..].iter().position(|b| *b == b'\n') {
                let end = self.rpos + i;
                let line = self.rbuf[self.rpos..end].to_vec();
                self.rpos = end + 1;
                let text = decode_utf8(&line)?;
                // 消费的字符数 = 这一行的字符（可能含尾部的 `\r`）+ 那个 `\n`
                self.cpos += text.chars().count() as i64 + 1;
                return Ok(Some(strip_cr(text)));
            }
            if self.fill()? == 0 {
                if self.rpos < self.rbuf.len() {
                    let rest = self.rbuf[self.rpos..].to_vec();
                    self.rpos = self.rbuf.len();
                    let text = decode_utf8(&rest)?;
                    self.cpos += text.chars().count() as i64;   // 末尾没有换行符
                    return Ok(Some(strip_cr(text)));
                }
                return Ok(None);
            }
        }
    }

    fn read_all(&mut self) -> Result<String, JishiError> {
        let mut out = self.rbuf[self.rpos..].to_vec();
        self.rbuf.clear();
        self.rpos = 0;
        std::io::Read::read_to_end(&mut self.file, &mut out)
            .map_err(|e| io_fail(&e, &self.path))?;
        let text = decode_utf8(&out)?;
        self.cpos += text.chars().count() as i64;
        Ok(text)
    }

    fn read_n(&mut self, n: i64) -> Result<String, JishiError> {
        if n < 0 { return self.read_all(); }
        // M56：n 是**字符**数，不是字节数。
        //
        // 以前这里按字节截：`读(4)` 在「abc中文」上只取到 `abc` + 汉字的前 1 个
        // 字节，再解码就报「文件内容不是 UTF-8 文本，读不出来」——
        // **而那个文件明明是合法的 UTF-8**（误导性报错）。Python 与 Node 都按字符。
        let mut out = String::new();
        let mut count = 0i64;
        while count < n {
            // 缓冲区里至少要有一个字节
            if self.rpos >= self.rbuf.len() && self.fill()? == 0 {
                break;                              // 到文件末尾
            }
            // 凑齐一个完整字符（多字节字符可能跨块边界）
            let mut need = utf8_seq_len(self.rbuf[self.rpos]);
            while self.rbuf.len() - self.rpos < need {
                if self.fill()? == 0 { break; }     // 末尾是截断的半字符：按现有字节解
                need = utf8_seq_len(self.rbuf[self.rpos]);
            }
            let end = std::cmp::min(self.rpos + need, self.rbuf.len());
            out.push_str(&String::from_utf8_lossy(&self.rbuf[self.rpos..end]));
            self.rpos = end;
            count += 1;
        }
        self.cpos += count;
        Ok(out)
    }

    fn write_text(&mut self, s: &str) -> Result<(), JishiError> {
        std::io::Write::write_all(&mut self.file, s.as_bytes())
            .map_err(|e| io_fail(&e, &self.path))
    }

    fn flush(&mut self) -> Result<(), JishiError> {
        std::io::Write::flush(&mut self.file).map_err(|e| io_fail(&e, &self.path))
    }

    /// 当前位置：**已消费的字符数**（M56，见 `cpos` 字段的说明）。
    fn tell(&mut self) -> Result<i64, JishiError> {
        Ok(self.cpos)
    }

    /// 挪到第 `pos` 个**字符**位置（M56）。
    ///
    /// 字符位置没法直接换成字节偏移（要扫全文），所以「回到开头 + 按字符走」。
    /// 越界不报错 —— 与 Node 侧 `seek` 同一取舍（后续读自然给空）。
    fn seek(&mut self, pos: i64) -> Result<(), JishiError> {
        // 定位会作废预读缓冲，否则读出来的东西对不上位置
        self.rbuf.clear();
        self.rpos = 0;
        std::io::Seek::seek(&mut self.file, std::io::SeekFrom::Start(0))
            .map_err(|e| io_fail(&e, &self.path))?;
        let pos = pos.max(0);
        if pos > 0 {
            self.read_n(pos)?;      // 读掉（会推进 cpos）
        }
        self.cpos = pos;            // 越界时位置仍记 pos，与 Node 一致
        Ok(())
    }
}

/// 打开文件（模式用中文：读 / 写 / 追加 / 读写，也认 r/w/a/r+/w+/a+）。
/// 一律 UTF-8——Windows 的默认编码是 GBK，中文会乱。
fn open_file(path: &str, mode: &str) -> Result<Rc<RefCell<FileHandle>>, JishiError> {
    let py = match mode { "读" => "r", "写" => "w", "追加" => "a", "读写" => "r+", o => o };
    let mut o = std::fs::OpenOptions::new();
    let (readable, writable) = match py {
        "r" => { o.read(true); (true, false) }
        "w" => { o.write(true).create(true).truncate(true); (false, true) }
        "a" => { o.append(true).create(true); (false, true) }
        "r+" => { o.read(true).write(true); (true, true) }
        "w+" => { o.read(true).write(true).create(true).truncate(true); (true, true) }
        "a+" => { o.read(true).append(true).create(true); (true, true) }
        _ => return Err(err("类型错误", format!(
            "不认识的打开模式「{mode}」，可用：读、写、追加、读写（也认 r/w/a/r+/w+/a+）"))),
    };
    let file = o.open(path).map_err(|e| io_fail(&e, path))?;
    Ok(Rc::new(RefCell::new(FileHandle {
        file,
        path: path.to_string(),
        mode: mode.to_string(),
        closed: false,
        rbuf: Vec::new(),
        rpos: 0,
        cpos: 0,
        readable,
        writable,
    })))
}

/// IO 错误 → 中文（对齐 Python 侧 errors.py 的翻译：
/// 找不到文件不带英文原文，路径本身才是排错要的信息）。
fn io_fail(e: &std::io::Error, path: &str) -> JishiError {
    use std::io::ErrorKind::*;
    match e.kind() {
        NotFound => err("文件错误", format!("找不到文件或目录：{path}")),
        PermissionDenied => err("文件错误", format!("没有权限，访问被拒绝：{path}")),
        _ => err("文件错误", format!("读写文件出错（{path}）：{e}")),
    }
}

fn decode_utf8(bytes: &[u8]) -> Result<String, JishiError> {
    String::from_utf8(bytes.to_vec())
        .map_err(|_| err("文件错误", "文件内容不是 UTF-8 文本，读不出来"))
}

fn strip_cr(s: String) -> String {
    s.strip_suffix('\r').map(|x| x.to_string()).unwrap_or(s)
}

/// UTF-8 首字节 → 这个字符占几个字节（1~4）。M56 起 `读(n)` 按字符读要用它。
///
/// 非法/续字节一律当 1 —— 不在切分这一层猜，交给后面的解码器去报错。
fn utf8_seq_len(b: u8) -> usize {
    if b < 0x80 { 1 }
    else if b >> 5 == 0b110 { 2 }
    else if b >> 4 == 0b1110 { 3 }
    else if b >> 3 == 0b11110 { 4 }
    else { 1 }
}

/// 文件方法分派（放进 `call_builtin_method` 的 `(Val::File, name)` 分支）。
fn file_call(name: &str, f: Rc<RefCell<FileHandle>>,
             args: &[Val]) -> Result<Val, JishiError> {
    match name {
        "读" => {
            need_args(name, args, 0, 1)?;
            let n = match args.first() {
                Some(v) => as_int(v)? as i64,     // 读几个字节，不会超过 i64
                None => -1,
            };
            let mut h = f.borrow_mut();
            h.check_read(name)?;
            Ok(Val::Str(h.read_n(n)?))
        }
        "读行" => {
            need_args(name, args, 0, 0)?;
            let mut h = f.borrow_mut();
            h.check_read(name)?;
            Ok(match h.read_line()? {
                Some(l) => Val::Str(l),
                None => Val::None_,      // 读到末尾给「空」，便于判断
            })
        }
        "读所有行" => {
            need_args(name, args, 0, 0)?;
            let mut h = f.borrow_mut();
            h.check_read(name)?;
            let mut out = Vec::new();
            while let Some(l) = h.read_line()? {
                out.push(Val::Str(l));
            }
            Ok(list_new(out))
        }
        "写" | "写行" => {
            need_args(name, args, 1, 1)?;
            let text = match &args[0] {
                Val::Str(s) => s.clone(),
                other => return Err(err("类型错误", format!(
                    "「{name}」需要文本，得到了「{}」", type_label(other)))),
            };
            let mut h = f.borrow_mut();
            h.check_write(name)?;
            let payload = if name == "写行" { format!("{text}\n") } else { text };
            h.write_text(&payload)?;
            Ok(Val::None_)
        }
        "关闭" => {
            need_args(name, args, 0, 0)?;
            let mut h = f.borrow_mut();
            if !h.closed {                 // 重复关闭不报错，幂等更省心
                let _ = h.flush();
                h.closed = true;
                h.rbuf.clear();
                h.rpos = 0;
            }
            Ok(Val::None_)
        }
        "刷新" => {
            need_args(name, args, 0, 0)?;
            let mut h = f.borrow_mut();
            h.check(name)?;
            h.flush()?;
            Ok(Val::None_)
        }
        "位置" => {
            need_args(name, args, 0, 0)?;
            let mut h = f.borrow_mut();
            h.check(name)?;
            Ok(Val::Int(h.tell()? as i128))
        }
        "定位" => {
            need_args(name, args, 1, 1)?;
            let pos = as_int(&args[0])? as i64;   // 定位偏移不会超过 i64
            let mut h = f.borrow_mut();
            h.check(name)?;
            h.seek(pos)?;
            Ok(Val::None_)
        }
        _ => Err(err("类型错误", format!("「{name}」方法调用未实现"))),
    }
}

/// 值的类型名（中文），与内建「类型」一致。
pub(crate) fn type_label(v: &Val) -> String {
    match v {
        Val::None_ => "空".to_string(),
        Val::Bool(_) => "布尔".to_string(),
        Val::Int(_) => "整数".to_string(),
        Val::Float(_) => "小数".to_string(),
        Val::Str(_) => "文本".to_string(),
        Val::Dec(_) => "精确小数".to_string(),
        // Python 侧 `类型(日期.解析(…))` 给的就是这个
        Val::Date(_) => "datetime".to_string(),
        Val::Table(_) => "表格".to_string(),
        Val::Slice(_) => "切片".to_string(),
        // 原样片段（M50）：Python 侧给「原样片段」（）
        Val::HtmlRaw(_) => "原样片段".to_string(),
        Val::List(_) => "列表".to_string(),
        Val::Dict(_) => "字典".to_string(),
        Val::Set(_) => "集合".to_string(),
        Val::File(_) => "文件".to_string(),
        Val::Func(_) => "函数".to_string(),
        // 实例与类要给**带名字**的结果，与 Python 侧一致（M34 修）：
        // Python 的 `type_name` 对实例给 `A 实例`、对类给 `类 A`，
        // 以前这里一律给「对象」/「类」——用户拿 `类型(x) == "A 实例"`
        // 判断会永远为假，属于「说好的能力给了错答案」。
        Val::Class(c) => format!("类 {}", c.name),
        Val::Instance(inst) => format!("{} 实例", inst.borrow().class.name),
        // 内建函数 / 模块 / 异常类型 / 绑定方法：对齐 Python 的 `type_name`
        Val::Builtin(..) => "内建函数".to_string(),
        Val::Module(..) => "模块".to_string(),
        Val::ExcType(_) => "异常类型".to_string(),
        // 被捕获的异常：Python / Node 的 `类型(e)` 都给「异常」（M56）
        Val::Exc(_) => "异常".to_string(),
        Val::BoundMethod(bm) => match bm.as_ref() {
            BoundMethod::Builtin { .. } => "内建方法".to_string(),
            BoundMethod::User { .. } => "方法".to_string(),
        },
        // 环境记录（`遍历 x 在 …` 的内部用途）不该被用户看到，
        // 但类型名总要给一个——与 Python 侧同样是兜底
        _ => "其他".to_string(),
    }
}

#[derive(Clone)]
enum BoundMethod {
    /// 内建类型（列表 / 文本 / 字典 / 集合 / 文件 / 精确小数）的方法。
    /// `line`/`col` 是**取属性时**的位置（`X.方法` 那一处）—— 方法内部报错
    /// 用它，与 Python 的 `_BoundMethod.__call__` 同源（R6.6）。
    Builtin { name: String, receiver: Box<Val>, line: i64, col: i64 },
    User { func: Func, instance: JInst, name: String },
}

// ---------------------------------------------------------------------------
// 指令号
// ---------------------------------------------------------------------------

mod op {
    pub const LOAD_CONST: i64 = 1;
    pub const LOAD_GLOBAL: i64 = 2;
    pub const STORE_GLOBAL: i64 = 3;
    pub const LOAD_FAST: i64 = 4;
    pub const STORE_FAST: i64 = 5;
    pub const LOAD_DEREF: i64 = 6;
    pub const STORE_DEREF: i64 = 7;
    pub const LOAD_CELL: i64 = 8;
    pub const POP_TOP: i64 = 9;
    pub const DUP_TOP: i64 = 10;
    pub const ROT_TWO: i64 = 11;
    pub const BIN_OP: i64 = 13;
    pub const COMPARE: i64 = 15;
    pub const JUMP: i64 = 16;
    pub const POP_JUMP_IF_FALSE: i64 = 17;
    pub const POP_JUMP_IF_TRUE: i64 = 18;
    pub const JUMP_IF_FALSE_OR_POP: i64 = 19;
    pub const CALL: i64 = 20;
    pub const RETURN: i64 = 21;
    pub const MAKE_FUNCTION: i64 = 22;
    pub const BUILD_LIST: i64 = 23;
    pub const BUILD_DICT: i64 = 24;
    pub const GET_ATTR: i64 = 25;
    pub const SET_ATTR: i64 = 26;
    pub const GET_ITEM: i64 = 27;
    pub const SET_ITEM: i64 = 28;
    pub const GET_ITER: i64 = 29;
    pub const FOR_ITER: i64 = 30;
    pub const UNPACK: i64 = 46;
    pub const LIST_APPEND: i64 = 47;
    // 切片（M33）：与 Python 侧的 opcodes.py 对齐；`a` 是位标志
    // （1=有起点 2=有终点 4=有步长），分量按顺序压在栈上
    pub const BUILD_SLICE: i64 = 48;
    pub const HALT: i64 = 0;
    pub const ROT_THREE: i64 = 12;
    pub const UNARY_OP: i64 = 14;
    pub const IMPORT: i64 = 31;
    pub const STORE_LAST: i64 = 38;
    pub const BUILD_CLASS: i64 = 45;
    pub const SETUP_LOOP: i64 = 34;
    pub const POP_BLOCK: i64 = 35;
    pub const BREAK_LOOP: i64 = 36;
    pub const CONTINUE_LOOP: i64 = 37;
    pub const LOOP_SETUP: i64 = 33;
    pub const DUP_TWO: i64 = 32;
    pub const SETUP_TRY: i64 = 39;
    pub const POP_TRY: i64 = 40;
    pub const THROW: i64 = 41;
    pub const CHECK_SIGNAL: i64 = 42;
    pub const MATCH_EXC: i64 = 43;
    pub const END_FINALLY: i64 = 44;
}

// ---------------------------------------------------------------------------
// 指令
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
struct Instr {
    op: i64,
    a: i64,
    b: i64,
    c: i64,
    /// 源码位置（M56）。字节码里**一直带着**（每条指令是 6 个数字：
    /// `op,a,b,c,line,col`），但这里原来只解前 4 个、把位置丢了 ——
    /// 那是「宿主报错说不出哪一行」的第二个根因（第一个是 `JishiError`
    /// 没有这两个字段）。现在解出来 + 冒泡时统一填进错误。
    line: i64,
    col: i64,
}

// ---------------------------------------------------------------------------
// 代码对象 / 模块
// ---------------------------------------------------------------------------

#[derive(Clone)]
struct Code {
    name: String,
    params: Vec<String>,
    param_local: Vec<i64>,
    /// 形参种类（M25）：0=普通 1=*参数 2=**选项
    param_kind: Vec<i64>,
    param_cell: Vec<i64>,
    instrs: Vec<Instr>,
    nlocals: usize,
    /// 局部槽位 → 名字（报「找不到名字」用；M54 起才解析）。
    local_names: Vec<String>,
    cellvars: Vec<String>,
    freevars: Vec<String>,
    /// 局部槽位 → **按槽位的报错提示**（v3，R6.6）。只给「本函数赋过值、
    /// 外层也有同名变量」的槽位 —— 报「赋值前读局部」时用它替掉通用那句，
    /// 与 Python 的 `Code.local_hints` 同源（以前这份数据**不在字节码 JSON 里**，
    /// 宿主只能说通用说法，同一个错误在两处提示不同）。
    local_hints: Vec<(i64, String)>,
}

#[derive(Clone)]
struct Module {
    consts: Vec<Val>,
    names: Vec<String>,
    kw_names: Vec<Vec<String>>,
    codes: Vec<Code>,
    main: usize,
    /// 源文件名（R6.3）：报错渲染要 `  ┌─ 文件:行:列`，还要**按它去读源文件**
    /// 才能画出源码行与 `^`。字节码里一直带着（payload.filename），
    /// 以前宿主没解出来 —— 于是宿主画不出源码行。
    filename: String,
}

/// 调用链上的一帧：函数名 + **它的调用点**（第几行）。
///
/// ⚠️ 口径与 Python 侧 `vm._frames_to_trace` 逐字对齐（否则同一个错误在两个
/// 执行器里回溯长得不一样）：记的是**调用点**，不是「当前执行到第几行」；
/// 最外层那个模块主帧不进链（它的「函数名」是 `<模块>`，不是用户写的函数）。
#[derive(Debug, Clone)]
pub struct TraceFrame {
    pub func: String,
    pub line: Option<i64>,
}

impl Module {
    /// 源文件的行（1 起）：按需从磁盘读。
    ///
    /// 📌 **为什么不把源码塞进字节码**：那会让每个 `.json` 都背上整份源码
    /// （还和源码文件本身两份、容易不同步）。报错**只在出错那一刻**读一次，
    /// 正常路径零成本。文件读不到（嵌入场景 / 文件被删）就退回「没有源码行」——
    /// 位置那一行照样打得出来。
    fn source_line(&self, line: i64) -> Option<String> {
        if line < 1 {
            return None;
        }
        let text = std::fs::read_to_string(&self.filename).ok()?;
        text.lines().nth((line - 1) as usize).map(|s| s.to_string())
    }
}

// ---------------------------------------------------------------------------
// 异常
// ---------------------------------------------------------------------------

#[derive(Debug)]
pub struct JishiError {
    pub type_name: String,
    pub message: String,
    /// 错误码（M56）：与 Node 侧 `ERROR_CODES`、Python 侧 `errors.py` 同一张表。
    /// 见 `error_code` —— **只映射能确定的**，宽泛的「异常」给不出准确的码。
    pub code: Option<String>,
    /// 出错位置（M56）。以前**完全没有行列** —— 根因有两个：
    /// ① 这个结构体只有类型名与消息两个字段；
    /// ② 反序列化时把指令里的 `line`/`col` **丢掉了**（字节码里一直带着，
    ///    `Instr` 只解了前 4 个数字）。
    /// 两处都补齐后，位置由 `execute` 在错误冒泡时统一填（`fill_error_loc`），
    /// 而不是去改几百处 `err(...)` 调用点。
    pub line: Option<i64>,
    pub col: Option<i64>,
    /// 修法 / 线索提示（M56）：宿主原来**没有**这个字段（M54 记的账），
    /// 于是 Python 侧那句「你是不是想写「长度」？」在宿主上给不出来。
    pub hint: Option<String>,
    /// 调用链（R6.3）：**从外到内**，最外层模块帧不算。
    /// 没进过函数调用就是空（那种情况 Python 侧也不印回溯块）。
    pub trace: Vec<TraceFrame>,
}

/// 类型名 → 错误码（M56）。
///
/// 与 Node 侧 `ERROR_CODES`、Python 侧 `errors.py` 是**同一张表**的事实来源。
/// 宿主只映射「能确定的」那几个：宿主把多种语义错误（找不到名字、参数个数不对、
/// 属性不存在…）都归到「异常」，那个宽泛的名字**给不出准确的码**，宁可不给。
pub(crate) fn error_code(type_name: &str) -> Option<&'static str> {
    Some(match type_name {
        "除零错误" => "E2001",
        "类型错误" => "E2002",
        "索引错误" => "E2003",
        "键错误" => "E2004",
        "值错误" => "E2005",
        "文件错误" => "E2007",
        "断言错误" => "E2008",
        // R6.2：这三个也是**运行期真会出现**的类型名（Python 侧分别是
        // `SemanticNameError` E0301 / `SemanticFuncError` E0302 /
        // `RunNotCallableError` E2006）—— 以前宿主把它们笼统写成「异常」，
        // 于是连错误码都给不出。
        "输入结束" => "E2010",
        "程序被中断" => "E2100",
        T_NAME => "E0301",
        T_FUNC => "E0302",
        T_NOT_CALLABLE => "E2006",
        _ => return None,
    })
}

type JsResult<T> = Result<T, JishiError>;

/// 类型名 → **显示用的标题**（M56）。与 Python 侧 `errors.py` 的 `title` 同一张表。
///
/// 为什么不直接显示 `type_name`：那是**捕获匹配**用的标识
/// （`捕获 断言错误 为 e`），而 Python 侧的报错标题更口语 ——
/// 「断言没通过」「不能除以零」「索引越界」。宿主对齐过去，报错才长得一样。
pub fn error_title(type_name: &str) -> &str {
    match type_name {
        "除零错误" => "不能除以零",
        "断言错误" => "断言没通过",
        "值错误" => "数值不对",
        "索引错误" => "索引越界",
        "键错误" => "找不到这个键",
        // 这几个在 Python 侧 `title` 就是类型名本身（找不到这个名字 /
        // 找不到这个函数 / 这个东西不能调用 / 类型错误 / 文件错误…）
        _ => type_name,
    }
}

pub(crate) fn err(t: &str, msg: impl Into<String>) -> JishiError {
    JishiError { type_name: t.to_string(), message: msg.into(),
                 code: error_code(t).map(|s| s.to_string()),
                 line: None, col: None, hint: None, trace: Vec::new() }
}

/// 带提示语的报错（M56）：`hint` 会渲染成「  提示：…」那一行。
pub(crate) fn err_with_hint(t: &str, msg: impl Into<String>, hint: String) -> JishiError {
    JishiError { type_name: t.to_string(), message: msg.into(),
                 code: error_code(t).map(|s| s.to_string()),
                 line: None, col: None, hint: Some(hint), trace: Vec::new() }
}

// ---------------------------------------------------------------------------
// 「你是不是想写」的候选名（M56）
// ---------------------------------------------------------------------------
//
// 为什么要**逐字复刻** Python 的 difflib：三侧的候选名必须一样 ——
// **不一致比不给候选更糟**（用户会以为另一侧是错的）。这里照 CPython 3.13 的
// `SequenceMatcher.ratio()` 与 `get_close_matches` 写了一份，由
// `tests/test_m51_consistency.py` 的五引擎对拍钉住（Node 侧是同一份算法的 JS 版）。
//
// ⚠️ 两个最容易抄错的点（都实测踩过）：
//   ① 最长公共块要用 difflib 的 **DP 版**（`j2len`），且按 `k > bestsize`
//      **严格大于**保留先遇到的。换个等价选法（暴力取最长），同长度不同起点时
//      会选出**另一个块**，递归下去 M 就不同了。
//   ② `get_close_matches` 收集的是 `(分数, 名字)` 元组，所以**同分时按名字降序**
//      —— 不是「保持名字池原序」。抄错这点，3000 组随机对拍里有 103 组
//      候选顺序不同（例：`e文` 该给 `['文本','中文']`）。
//      ⚠️ Rust 的 `String` 比较按 UTF-8 **字节**序，恰好与 Python 的**码点**序
//      一致（UTF-8 保序），所以这里能直接用 `cmp`。

/// `difflib.SequenceMatcher.ratio()`：`2*M / (len(a)+len(b))`，M = 匹配字符总数。
pub(crate) fn sequence_ratio(s1: &str, s2: &str) -> f64 {
    if s1 == s2 {
        return 1.0;
    }
    let a: Vec<char> = s1.chars().collect();
    let b: Vec<char> = s2.chars().collect();
    let n = a.len() + b.len();
    if n == 0 {
        return 1.0;                     // difflib 的 `_calculate_ratio`
    }
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    2.0 * matching_total(&a, &b) as f64 / n as f64
}

/// `get_matching_blocks` 的 size 之和（合并相邻块不改变和，所以直接累加）。
fn matching_total(a: &[char], b: &[char]) -> usize {
    let mut b2j: HashMap<char, Vec<usize>> = HashMap::new();
    for (i, c) in b.iter().enumerate() {
        b2j.entry(*c).or_default().push(i);
    }
    let mut total = 0usize;
    let mut queue: Vec<(usize, usize, usize, usize)> = vec![(0, a.len(), 0, b.len())];
    while let Some((alo, ahi, blo, bhi)) = queue.pop() {
        let (i, j, k) = find_longest_match(a, &b2j, alo, ahi, blo, bhi);
        if k > 0 {
            total += k;
            if alo < i && blo < j {
                queue.push((alo, i, blo, j));
            }
            if i + k < ahi && j + k < bhi {
                queue.push((i + k, ahi, j + k, bhi));
            }
        }
    }
    total
}

/// 复刻 `SequenceMatcher.find_longest_match`（DP 版 `j2len`）。
fn find_longest_match(a: &[char], b2j: &HashMap<char, Vec<usize>>,
                      alo: usize, ahi: usize, blo: usize, bhi: usize)
                      -> (usize, usize, usize) {
    let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0usize);
    let mut j2len: HashMap<usize, usize> = HashMap::new();
    for i in alo..ahi {
        let mut newj2len: HashMap<usize, usize> = HashMap::new();
        if let Some(js) = b2j.get(&a[i]) {
            for &j in js {
                if j < blo {
                    continue;
                }
                if j >= bhi {
                    break;
                }
                let prev = if j == 0 { 0 } else { j2len.get(&(j - 1)).copied().unwrap_or(0) };
                let k = prev + 1;
                newj2len.insert(j, k);
                if k > bestsize {       // ⚠️ 严格大于 = 并列取先遇到的
                    besti = i - k + 1;
                    bestj = j - k + 1;
                    bestsize = k;
                }
            }
        }
        j2len = newj2len;
    }
    (besti, bestj, bestsize)
}

/// `difflib.get_close_matches(word, pool, n, cutoff)` 的等价实现。
pub(crate) fn close_matches(word: &str, pool: &[String], n: usize, cutoff: f64) -> Vec<String> {
    let mut hits: Vec<(f64, String)> = Vec::new();
    for x in pool {
        let r = sequence_ratio(x, word);
        if r >= cutoff {
            hits.push((r, x.clone()));
        }
    }
    // 元组降序：分数降，**同分时名字降**（difflib 收集的是 (分数, 名字)）
    hits.sort_by(|p, q| {
        q.0.partial_cmp(&p.0).unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| q.1.cmp(&p.1))
    });
    hits.into_iter().take(n).map(|(_, x)| x).collect()
}

/// 名字池里全是中文标识符，用英文名一般是「从 Python 抄来的」。
fn py_name_hint(name: &str) -> Option<&'static str> {
    Some(match name {
        "True" => "基石里布尔值写「真」，不是 Python 的 True",
        "False" => "基石里布尔值写「假」，不是 Python 的 False",
        "None" => "基石里空值写「空」，不是 Python 的 None",
        "null" => "基石里空值写「空」，不是 null",
        _ => return None,
    })
}

/// 「找不到名字」的提示语（M56）。算不出候选就给 None（那就只报错、不带提示）。
///
/// ⚠️ 与 Python 侧 `runtime.lookup_name` 同一顺序：**先查英文名对照表**
/// （那是确定的、比模糊候选更有用），再算相近候选。
pub(crate) fn name_hint(name: &str, pool: &[String]) -> Option<String> {
    if let Some(h) = py_name_hint(name) {
        return Some(h.to_string());
    }
    let matches = close_matches(name, pool, 3, 0.5);
    if matches.is_empty() {
        return None;
    }
    Some(format!("你是不是想写「{}」？", matches.join("」或「")))
}

// 循环信号
#[derive(Debug)]
enum Signal { Break, Continue }

// ---------------------------------------------------------------------------
// 帧
// ---------------------------------------------------------------------------

struct Frame {
    code_idx: usize,
    ip: usize,
    base: usize,
    /// **本帧是在哪被调用的**（R6.3）—— 调用链要的就是这个，不是「当前跑到哪」。
    /// 主帧没有调用点（`None`），它也不进链。
    call_line: Option<i64>,
    call_col: Option<i64>,
    locals: Vec<Val>,
    /// 局部槽「赋过值没有」（M54）。
    ///
    /// ⚠️ 为什么不能用 `Val::None_` 当哨兵（Python 侧用 `_UNSET` 就是这个道理）：
    /// 基石里 `空` 是**合法值**，读一个尚未赋值的局部变量与读一个真的 `空`
    /// 必须分得开。Rust 侧原先不分，于是
    /// `函数 外(): 如果 假: 令 y = 1  返回 y` 在 Python 上报 E0301、
    /// 在 Rust 上**静默给出 `空`** —— 那是「不报错的错答案」，比报错难查一百倍。
    locals_set: Vec<bool>,
    /// 本帧的单元表 = `[cellvars… , freevars…]`（共享，见 `JCell`）。
    cells: Vec<JCell>,
    /// (记录时的栈深, 继续目标, 中断目标, 登记序号)
    loops: Vec<(usize, i64, i64, i64)>,
    handlers: Vec<Handler>,
    /// 被 finally 挂起的返回值（`返回` 落在 try 块里时用）。
    /// 对齐 Python 侧 `_Frame.pending_return`。
    pending_return: Option<Val>,
    /// 块栈登记序号：越大越内层。信号分发时用来判断
    /// 「最近的循环」和「最近的 try」谁更深。
    block_seq: i64,
}

#[derive(Clone)]
struct Handler {
    catch_ip: i64,
    finally_ip: i64,
    sp: usize,
    seq: i64,
}

// ---------------------------------------------------------------------------
// 虚拟机
// ---------------------------------------------------------------------------

pub struct VM {
    module: Module,
    /// 全局变量表：**按名字下标索引**（`ins.a` 就是 `module.names` 的下标）。
    ///
    /// ⚠️ 以前这里是 `HashMap<String, Val>`，而且每次 `LOAD_GLOBAL` 还先
    /// `names[a].clone()`（**分配一个 String**）再哈希查找。实测（n=500000 的
    /// `当` 循环）：同样的逻辑写在**模块级**（走这里）要 **198ms**，
    /// 放进函数内（走局部变量槽位）只要 **91ms** —— 而 C VM 两者一样
    /// （26 vs 28ms，它的 `globals` 本来就是 `JVal *globals` 数组）。
    /// 差的就是这一处：**HashMap + String 分配 vs 一次数组索引**。
    ///
    /// `None` = 这个名字**没有定义**（读它要报「找不到名字」）。内建在加载时
    /// 一次性铺进去（见 `init_globals`）。
    globals: Vec<Option<Val>>,
    /// **全部内建的名字**（加载时建一次）。
    ///
    /// 报错路径要它：「找不到名字」的候选提示，池子不只是「本文件里出现过的
    /// 名字」，还包括**全部内建** —— `打印(长渡([1]))` 里根本没出现「长度」，
    /// 但提示要说「你是不是想写「长度」？」。C VM 侧就是这么做的
    /// （`cvm_bind.py`：`pool = dict(self.globals)` 再补用户全局名）。
    /// ⚠️ 我第一版只拿「`names` 里已定义的槽位」当池子，于是**候选提示没了**
    /// —— 被 `test_m51_consistency.py` 的「宿主也给候选名」抓住。
    builtin_names: Vec<String>,
    stack: Vec<Val>,
    frames: Vec<Frame>,
    last_value: Val,
    output: String,
}

/// 建全局表：长度 = 名字个数，**内建按名字预填**，其余留 `None`。
/// 顺带给出「全部内建名」（报错路径的候选池用，见 `builtin_names`）。
///
/// 照 C VM 的做法（`cvm_bind.py` 那边也是「按 names 顺序把内建铺成数组」
/// 再交给 C）—— 两个宿主的全局表就同构了。
fn init_globals(module: &Module) -> (Vec<Option<Val>>, Vec<String>) {
    let builtins = new_builtins();
    let names = builtins.keys().cloned().collect();
    let slots = module.names.iter().map(|n| builtins.get(n).cloned()).collect();
    (slots, names)
}

impl VM {
    pub fn new(module: Module) -> Self {
        let (globals, builtin_names) = init_globals(&module);
        VM {
            module,
            globals,
            builtin_names,
            stack: Vec::new(),
            frames: Vec::new(),
            last_value: Val::None_,
            output: String::new(),
        }
    }

    pub fn run(&mut self) -> Result<(), JishiError> {
        let main_idx = self.module.main;
        let main = &self.module.codes[main_idx];
        let nlocals = main.nlocals;
        let ncells = main.cellvars.len() + main.freevars.len();
        let frame = Frame {
            code_idx: main_idx,
            ip: 0,
            base: 0,
            call_line: None,        // 模块主帧没有调用点（也不进调用链）
            call_col: None,
            locals: vec![Val::None_; nlocals],
            // 模块级帧没有形参，所有槽都算「没赋过值」（首次 `STORE_FAST` / `LOOP_SETUP` 才置位）
            locals_set: vec![false; nlocals],
            cells: new_cells(ncells),
            loops: Vec::new(),
            handlers: Vec::new(),
            pending_return: None,
            block_seq: 0,
        };
        self.frames.push(frame);
        self.execute(None)
    }

    fn execute(&mut self, stop_at: Option<usize>) -> Result<(), JishiError> {
        // 帧深（stop_at 用帧索引简化）
        loop {
            if self.frames.is_empty() {
                return Ok(());
            }
            let fi = self.frames.len() - 1;
            let result = self.execute_frame(fi);
            match result {
                Ok(Flow::Return) => {
                    // 正常弹帧，继续外层
                    self.frames.pop();
                    if Some(fi) == stop_at { return Ok(()); }
                }
                Ok(Flow::Call) => {
                    // 帧已在 CALL 里压好，什么都不做、继续循环即可。
                    // （这条分支以前不存在，导致新帧被 pop 掉。）
                }
                Ok(Flow::Halt) => {
                    self.frames.pop();
                    return Ok(());
                }
                Ok(Flow::Error(mut e)) => {
                    // 异常分发
                    self.fill_error_loc(&mut e, fi);
                    // 拍调用链的时机与 Python 侧一样：**错误第一次冒出来那一刻**
                    // （再往外抛的路上帧早就被弹干净了）。见 `snapshot_trace`。
                    self.snapshot_trace(&mut e);
                    if !self.dispatch_exception(&e, stop_at)? {
                        return Err(e);
                    }
                }
                Ok(Flow::Signal(s)) => {
                    if !self.dispatch_signal(s, stop_at)? {
                        return Err(err("运行期错误", "中断/继续没有对应的循环"));
                    }
                }
                Err(mut e) => {
                    // 出错（除零、类型不符、`抛出`…）也要走异常分发去找捕获/最终。
                    // 以前这里直接 `return Err(e)`，于是 try/捕获/最终 对「内置错误»
                    // 完全失效——只有 Flow::Error 才分发，而那个变体根本没人构造。
                    //
                    // M56：出错的**位置**在这里统一补 —— 不必去动几百处 `err(...)`
                    // 调用点（内建与标准库也不知道自己在第几行）。
                    self.fill_error_loc(&mut e, fi);
                    // 拍调用链的时机与 Python 侧一样：**错误第一次冒出来那一刻**
                    // （再往外抛的路上帧早就被弹干净了）。见 `snapshot_trace`。
                    self.snapshot_trace(&mut e);
                    if !self.dispatch_exception(&e, stop_at)? {
                        return Err(e);
                    }
                }
            }
        }
    }

    /// 给错误补上「出错位置」（M56）。
    ///
    /// 取「当前帧正跑到的那条指令」的 `line`/`col`（主循环每步把 `ip` 写回帧，
    /// 见 `execute_frame`）。**只填还没填的** —— 内层若已给出更准的位置，
    /// 不要被外层覆盖。
    fn fill_error_loc(&self, e: &mut JishiError, fi: usize) {
        if e.line.is_some() {
            return;
        }
        let Some(frame) = self.frames.get(fi) else { return };
        let Some(code) = self.module.codes.get(frame.code_idx) else { return };
        let Some(ins) = code.instrs.get(frame.ip) else { return };
        e.line = Some(ins.line);
        e.col = Some(ins.col);
    }

    /// 把当前帧栈拍成调用链（只拍一次；已经有链就不动）。
    ///
    /// ⚠️ 口径照 Python 的 `vm._frames_to_trace`：
    /// * **跳过 frames[0]**（模块主帧 —— 它的名字是 `<模块>`，不是用户写的函数）；
    /// * 每帧记「函数名 + **它的调用点**」（`call_line`），不是「当前跑到第几行」；
    /// * 匿名函数（`函数(x)：…` 脱糖出来的）的 code 名以 `<` 开头，
    ///   报错里显示成「匿名函数」更好认。
    fn snapshot_trace(&self, e: &mut JishiError) {
        if !e.trace.is_empty() {
            return;
        }
        let mut chain = Vec::new();
        for fr in self.frames.iter().skip(1) {
            let Some(code) = self.module.codes.get(fr.code_idx) else { continue };
            let name = if code.name.starts_with('<') {
                "匿名函数".to_string()
            } else {
                code.name.clone()
            };
            chain.push(TraceFrame { func: name, line: fr.call_line });
        }
        e.trace = chain;
    }

    fn execute_frame(&mut self, fi: usize) -> Result<Flow, JishiError> {
        let frame = &self.frames[fi];
        let code_idx = frame.code_idx;
        let code = &self.module.codes[code_idx];
        let instrs = code.instrs.clone();
        let mut ip = frame.ip;
        let base = frame.base;

        loop {
            if ip >= instrs.len() {
                return Ok(Flow::Return);
            }
            // M56：把「正跑到哪一条」写回帧 —— 出错冒泡到 `execute` 时，
            // 靠它查「第几行第几列」（见 `fill_error_loc`）。
            // 每步一次写回的代价可忽略，换来的是**全部**报错自动带位置。
            self.frames[fi].ip = ip;
            let ins = &instrs[ip];
            ip += 1;

            match ins.op {
                op::LOAD_CONST => {
                    let c = self.module.consts[ins.a as usize].clone();
                    self.stack.push(c);
                }
                op::LOAD_GLOBAL => {
                    // 名字**已经在加载时**按 `ins.a` 铺成槽位了（见 `globals`），
                    // 所以正常路径是「一次数组索引 + 一次 clone」——
                    // 不再分配 String、不再哈希查找。
                    let slot = ins.a as usize;
                    match self.globals.get(slot).and_then(|v| v.as_ref()).cloned() {
                        Some(v) => self.stack.push(v),
                        None => {
                            // ⚠️ 只有**报错路径**才去碰名字字符串 / 候选名池：
                            // 正常路径上一分钱都不花（M56 的老规矩）。
                            let name = self.module.names.get(slot).cloned()
                                .unwrap_or_else(|| format!("?全局{slot}"));
                            // 候选池 = **全部内建** + 「本文件里已定义的用户全局」。
                            // ⚠️ 不能只拿「names 里已定义的槽位」：内建里有一堆名字
                            // 本文件根本没出现（`长渡` 的提示要说「长度」，而
                            // `names` 里没有「长度」）。顺序无所谓 —— `close_matches`
                            // 有全序排序（分数降序 + 同分名字降序）。
                            let mut pool: Vec<String> = self.builtin_names.clone();
                            for (v, n) in self.globals.iter().zip(self.module.names.iter()) {
                                if v.is_some() && !self.builtin_names.contains(n) {
                                    pool.push(n.clone());
                                }
                            }
                            let msg = format!("找不到名字「{name}」");
                            // ⚠️ 类型名要写 Python 侧那个**真名**（`SemanticNameError`
                            // 的 title「找不到这个名字」，E0301）：写「异常」的话
                            // `e.类型` 与 `捕获` 的匹配都对不上（R6.2 对齐）。
                            return Err(match name_hint(&name, &pool) {
                                Some(h) => err_with_hint(T_NAME, msg, h),
                                None => err(T_NAME, msg),
                            });
                        }
                    }
                }
                op::STORE_GLOBAL => {
                    let v = self.stack.pop().unwrap_or(Val::None_);
                    if let Some(cell) = self.globals.get_mut(ins.a as usize) {
                        *cell = Some(v);
                    }
                }
                op::LOAD_FAST => {
                    // 与 Python / Node 对齐：没赋过值就读 → 报「找不到名字」，
                    // **不许静默给 `空`**（M54）。`空` 是合法值，两者必须分得开。
                    let f = &self.frames[fi];
                    let slot = ins.a as usize;
                    if !f.locals_set.get(slot).copied().unwrap_or(false) {
                        let code = &self.module.codes[f.code_idx];
                        let name = code.local_names.get(slot)
                            .cloned().unwrap_or_else(|| format!("?局部{slot}"));
                        // 文案与类型名都要照 Python 的 `runtime.unbound_local`：
                        // 「**赋值之前被用到**」而不是「找不到名字」—— 后者会让人以为
                        // 名字写错了。宿主原来两样都不一样（R6.2 对齐）。
                        // ⚠️ **提示语**：外层也有同名变量时用**按槽位**的那句
                        // （字节码 v3 的 `local_hints`，与 Python 同源）；没有就用
                        // 通用那句。R6.2 时这份数据不在格式里，只能给通用的（R6.6 收）。
                        let hint = code.local_hints.iter()
                            .find(|(k, _)| *k == slot as i64)
                            .map(|(_, v)| v.clone())
                            .unwrap_or_else(|| unbound_hint(&name));
                        return Err(err_with_hint(T_NAME, unbound_msg(&name), hint));
                    }
                    let v = f.locals[slot].clone();
                    self.stack.push(v);
                }
                op::STORE_FAST => {
                    let v = self.stack.pop().unwrap_or(Val::None_);
                    let slot = ins.a as usize;
                    self.frames[fi].locals[slot] = v;
                    self.frames[fi].locals_set[slot] = true;
                }
                op::LOAD_DEREF => {
                    // 读的是**单元里的值**（不是单元本身 —— 那是 `LOAD_CELL`）。
                    // `set == false` = 还没赋过值：与 Python / Node 一样报
                    // 「找不到名字」，**不许静默给 `空`**（M54 补齐的漂移）。
                    let cell = self.frames[fi].cells[ins.a as usize].clone();
                    let c = cell.borrow();
                    if !c.set {
                        let name = cell_name(&self.module.codes[self.frames[fi].code_idx],
                                             ins.a as usize);
                        // 同 LOAD_FAST：单元没赋过值时 Python 也报「赋值之前被用到」
                        return Err(err_with_hint(
                            T_NAME, unbound_msg(&name), unbound_hint(&name)));
                    }
                    let v = c.value.clone();
                    drop(c);
                    self.stack.push(v);
                }
                op::STORE_DEREF => {
                    let v = self.stack.pop().unwrap_or(Val::None_);
                    // ⚠️ **改单元里的值，不是换掉单元**（M54）：闭包抓走的是同一个
                    // `Rc<RefCell<Cell>>`，整体替换就断了 —— 见 `JCell` 的说明。
                    let cell = self.frames[fi].cells[ins.a as usize].clone();
                    let mut c = cell.borrow_mut();
                    c.value = v;
                    c.set = true;
                }
                op::LOAD_CELL => {
                    // 压的是**单元本身**（共享的 `Rc`），好让 `MAKE_FUNCTION` 抓走
                    // 同一个 —— 「先捕获、后赋值」才成立（嵌套递归函数就靠这个，M54）。
                    let cell = self.frames[fi].cells[ins.a as usize].clone();
                    self.stack.push(Val::Cell(cell));
                }
                op::BIN_OP => {
                    let r = self.stack.pop().unwrap_or(Val::None_);
                    let l = self.stack.pop().unwrap_or(Val::None_);
                    let out = binop(ins.a, l, r)?;
                    self.stack.push(out);
                }
                op::COMPARE => {
                    let r = self.stack.pop().unwrap_or(Val::None_);
                    let l = self.stack.pop().unwrap_or(Val::None_);
                    let out = compare(ins.a, l, r)?;
                    self.stack.push(out);
                }
                op::UNARY_OP => {
                    let v = self.stack.pop().unwrap_or(Val::None_);
                    let out = if ins.a == 0 { unary_neg(v) } else { Val::Bool(!truthy(v)) };
                    self.stack.push(out);
                }
                op::JUMP => ip = ins.a as usize,
                op::POP_JUMP_IF_FALSE => {
                    let v = self.stack.pop().unwrap_or(Val::None_);
                    if !truthy(v) { ip = ins.a as usize; }
                }
                op::POP_JUMP_IF_TRUE => {
                    let v = self.stack.pop().unwrap_or(Val::None_);
                    if truthy(v) { ip = ins.a as usize; }
                }
                op::JUMP_IF_FALSE_OR_POP => {
                    let top = self.stack.last().cloned().unwrap_or(Val::None_);
                    if truthy(top) { self.stack.pop(); } else { ip = ins.a as usize; }
                }
                op::POP_TOP => { self.stack.pop(); }
                op::DUP_TOP => {
                    let v = self.stack.last().cloned().unwrap_or(Val::None_);
                    self.stack.push(v);
                }
                op::DUP_TWO => {
                    let len = self.stack.len();
                    let a = self.stack[len - 2].clone();
                    let b = self.stack[len - 1].clone();
                    self.stack.push(a); self.stack.push(b);
                }
                op::ROT_TWO => {
                    let len = self.stack.len();
                    self.stack.swap(len - 1, len - 2);
                }
                op::ROT_THREE => {
                    let len = self.stack.len();
                    let a = self.stack[len - 3].clone();
                    let b = self.stack[len - 2].clone();
                    let c = self.stack[len - 1].clone();
                    self.stack[len - 3] = c;
                    self.stack[len - 2] = a;
                    self.stack[len - 1] = b;
                }
                op::CALL => {
                    let nargs = ins.a as usize;
                    let kwi = ins.b;
                    let knames: Vec<String> = if kwi >= 0 { self.module.kw_names[kwi as usize].clone() } else { Vec::new() };
                    let nkw = knames.len();
                    let fn_base = self.stack.len() - (1 + nargs + nkw);
                    // 取 fn 和 args
                    let fn_val = self.stack[fn_base].clone();
                    let args: Vec<Val> = self.stack[fn_base + 1 .. fn_base + 1 + nargs].to_vec();
                    let kwvals: Vec<Val> = if nkw > 0 { self.stack[fn_base + 1 + nargs ..].to_vec() } else { Vec::new() };
                    // 截栈
                    self.stack.truncate(fn_base);
                    match fn_val {
                        Val::Func(f) => {
                            self.push_func_frame(Some((fi, ip)), f, args, &knames,
                                                 &kwvals, fn_base,
                                                 (ins.line, ins.col))?;
                            return Ok(Flow::Call);
                        }
                        Val::Builtin(name, bfn) => {
                            // 命名参数在这里映射回位置参数（M51）。以前是直接
                            // `bfn(&args, self)` —— kwargs 被**静默丢掉**：
                            // `加密.摘要("abc", 算法="md5")` 会悄悄用 sha256。
                            let mapped = map_kwargs(name, &args, &knames, &kwvals)?;
                            let out = bfn(&mapped, self)?;
                            self.stack.push(out);
                        }
                        Val::Class(c) => {
                            reject_kwargs(&format!("类「{}」", c.name), &knames)?;
                            let inst = self.make_instance(c, &args)?;
                            self.stack.push(Val::Instance(inst));
                        }
                        Val::BoundMethod(bm) => {
                            // ⚠️ **内建方法携带的「取属性位置」**（R6.6）：方法内部的
                            // 错要落在 `X.方法` 上（`a.追加()` 指那一列 `.`），
                            // 与 Python 的 `_BoundMethod.__call__` 同源。宿主以前
                            // 一路用指令位置（`(` 那一列），四种方法相关的错全差三列。
                            // 📌 模块属性（`数.开方`）走的是裸 `Builtin`、不是这里 ——
                            // 那边 Python 也用调用点，本来就一致。
                            let bm_pos = match bm.as_ref() {
                                BoundMethod::Builtin { line, col, .. } => Some((*line, *col)),
                                BoundMethod::User { .. } => None,
                            };
                            match *bm {
                                BoundMethod::User { func, instance, .. } => {
                                    // 用户方法：把实例放进第一个形参（自身），再压新帧。
                                    // 与「函数」调用共用一条压帧路径——方法不过是个
                                    // 首参被预先绑定的函数。
                                    let mut all = Vec::with_capacity(args.len() + 1);
                                    all.push(Val::Instance(instance));
                                    all.extend(args.into_iter());
                                    // ⚠️ **关键字参数要一起传下去**（R6.7）：以前这里
                                    // 写的是 `&[]`，于是 `o.方法(不存在的名=1)` 的 kwargs
                                    // 被**静默丢掉**、报成「缺少参数：x」（看着像另一件事），
                                    // 而 Python / Node 报「函数「方法」没有叫「不存在」的参数」。
                                    // 宿主静默丢参数是本项目最恨的一类 bug（M51 修过同款）。
                                    self.push_func_frame(Some((fi, ip)), func, all,
                                                         &knames, &kwvals, fn_base,
                                                         (ins.line, ins.col))?;
                                    return Ok(Flow::Call);
                                }
                                builtin => {
                                    reject_kwargs("这个方法", &knames)?;
                                    match self.call_bound(builtin, &args) {
                                        Ok(out) => self.stack.push(out),
                                        Err(mut e) => {
                                            // 只补「还没填过」的 —— 与 `fill_error_loc`
                                            // 同一条规矩（内层更准的不许被覆盖）。
                                            if let Some((l, c)) = bm_pos {
                                                if e.line.is_none() && l > 0 {
                                                    e.line = Some(l);
                                                    e.col = Some(c);
                                                }
                                            }
                                            return Err(e);
                                        }
                                    }
                                }
                            }
                        }
                        Val::ExcType(t) => {
                            reject_kwargs("异常类型", &knames)?;
                            let msg = args.first().map(display).unwrap_or_default();
                            return Err(err(&t, msg));
                        }
                        // 类型名与提示都照 Python 的 `runtime.call_value`：
                        // 「这个东西不能调用」是 `RunNotCallableError` 的 title（E2006），
                        // 写「运行期错误」会让 `e.类型` / `捕获` 都对不上（R6.2 对齐）。
                        other => return Err(err_with_hint(
                            T_NOT_CALLABLE,
                            format!("「{}」不能调用", display(&other)),
                            "只有函数或带括号的对象才能调用".to_string())),
                    }
                }
                op::RETURN => {
                    let value = self.stack.pop().unwrap_or(Val::None_);
                    // `返回` 落在 try 块里时，先去跑最近的 finally（返回值挂起），
                    // 跑完在 END_FINALLY 处收尾。对齐 Python 侧 `_return_via_finally`。
                    match self.step_return(fi, value) {
                        ReturnStep::Jump(target) => { ip = target; }
                        ReturnStep::Done(v) => {
                            self.stack.truncate(base);
                            self.stack.push(v);
                            return Ok(Flow::Return);
                        }
                    }
                }
                op::MAKE_FUNCTION => {
                    let code_idx = ins.a as usize;
                    let ncells = ins.b as usize;
                    let ndefaults = ins.c as usize;
                    let defaults = if ndefaults > 0 {
                        let n = self.stack.len();
                        let tail: Vec<Val> = self.stack.drain(n - ndefaults..).collect();
                        // 默认值只属于「普通参数」的尾部——`*参数` / `**选项`
                        // 不能有默认值，也不参与对齐。所以 pad 要按
                        // 「普通参数个数」算，不能按全部形参个数算：
                        // `函数 带默认(甲, 乙 = 10, *余)` 若按 3 个形参 pad，
                        // 默认值会被算到「余」头上，`带默认(1)` 就报「缺少参数：乙」。
                        // （Python 侧 vm.py 在 M25 修过同一个坑。）
                        let sub_kinds = &self.module.codes[code_idx].param_kind;
                        let sub_nparams = self.module.codes[code_idx].params.len();
                        let sub_npos = sub_kinds.iter().position(|k| *k != 0)
                            .unwrap_or(sub_nparams);
                        let mut d = vec![Val::None_; sub_npos - ndefaults];
                        d.extend(tail);
                        Some(d)
                    } else { None };
                    let cells = if ncells > 0 {
                        let n = self.stack.len();
                        let c: Vec<Val> = self.stack.drain(n - ncells..).collect();
                        c.into_iter().map(|v| match v {
                            // 共享**同一个**单元（`Rc` 克隆），不是拷值 —— M54 修
                            Val::Cell(rc) => rc,
                            _ => new_cell(),
                        }).collect()
                    } else { Vec::new() };
                    let name = self.module.codes[code_idx].name.clone();
                    self.stack.push(Val::Func(Func {
                        name, code_idx, cells, defaults, id: next_obj_id(),
                    }));
                }
                op::BUILD_LIST => {
                    let n = ins.a as usize;
                    let items = if n > 0 {
                        let l = self.stack.len();
                        self.stack.drain(l - n..).collect()
                    } else { Vec::new() };
                    self.stack.push(list_new(items));
                }
                op::BUILD_SLICE => {
                    // 分量按 起点→终点→步长 的顺序在栈上，缺省的不压栈，
                    // 所以要按位标志**倒着**弹
                    let flags = ins.a;
                    let step = if flags & 4 != 0 { Some(as_int(&self.stack.pop().unwrap())?) } else { None };
                    let stop = if flags & 2 != 0 { Some(as_int(&self.stack.pop().unwrap())?) } else { None };
                    let start = if flags & 1 != 0 { Some(as_int(&self.stack.pop().unwrap())?) } else { None };
                    if step == Some(0) {
                        return Err(err("数值错误", "切片的步长不能为 0"));
                    }
                    self.stack.push(Val::Slice(Rc::new(SliceVal { start, stop, step })));
                }
                op::BUILD_DICT => {
                    let n = ins.a as usize;
                    let flat = if n > 0 {
                        let l = self.stack.len();
                        self.stack.drain(l - 2 * n..).collect()
                    } else { Vec::new() };
                    let mut pairs = Vec::new();
                    for i in (0..flat.len()).step_by(2) {
                        pairs.push((flat[i].clone(), flat[i + 1].clone()));
                    }
                    self.stack.push(dict_new(pairs));
                }
                op::GET_ATTR => {
                    let name = self.module.names[ins.a as usize].clone();
                    let obj = self.stack.pop().unwrap_or(Val::None_);
                    let out = get_attr(obj, &name, ins.line, ins.col)?;
                    self.stack.push(out);
                }
                op::SET_ATTR => {
                    let name = self.module.names[ins.a as usize].clone();
                    let val = self.stack.pop().unwrap_or(Val::None_);
                    let obj = self.stack.pop().unwrap_or(Val::None_);
                    set_attr(obj, &name, val)?;
                }
                op::GET_ITEM => {
                    let idx = self.stack.pop().unwrap_or(Val::None_);
                    let obj = self.stack.pop().unwrap_or(Val::None_);
                    let out = get_item(obj, idx)?;
                    self.stack.push(out);
                }
                op::SET_ITEM => {
                    let val = self.stack.pop().unwrap_or(Val::None_);
                    let idx = self.stack.pop().unwrap_or(Val::None_);
                    let obj = self.stack.pop().unwrap_or(Val::None_);
                    set_item(obj, idx, val)?;
                }
                op::GET_ITER => {
                    let obj = self.stack.pop().unwrap_or(Val::None_);
                    let it = self.make_iter(obj)?;
                    self.stack.push(it);
                }
                op::FOR_ITER => {
                    let next = iter_next(self.stack.last_mut().unwrap())?;
                    match next {
                        Some(v) => self.stack.push(v),
                        None => { self.stack.pop(); ip = ins.a as usize; }
                    }
                }
                op::UNPACK => {
                    let n = ins.a as usize;
                    let v = self.stack.pop().unwrap_or(Val::None_);
                    let items = unpack_values(v, n, ins.b)?;
                    for item in items.into_iter().rev() {
                        self.stack.push(item);
                    }
                }
                op::LIST_APPEND => {
                    let val = self.stack.pop().unwrap_or(Val::None_);
                    let obj = self.stack.pop().unwrap_or(Val::None_);
                    let bm = get_attr(obj, "追加", ins.line, ins.col)?;
                    if let Val::BoundMethod(bm) = bm {
                        self.call_bound(*bm, &[val])?;
                    }
                    self.stack.push(Val::None_);
                }
                op::BUILD_CLASS => {
                    let n = ins.a as usize;
                    let has_base = ins.b != 0;
                    let name = display(&self.stack.pop().unwrap_or(Val::None_));
                    let mut methods = HashMap::new();
                    if n > 0 {
                        let l = self.stack.len();
                        let fns: Vec<Val> = self.stack.drain(l - n..).collect();
                        for f in fns {
                            if let Val::Func(fu) = f { methods.insert(fu.name.clone(), Val::Func(fu)); }
                        }
                    }
                    // 类变量（M34）：先把基类的那份**拷进来**（不是共用同一个
                    // `Rc`）——与 Python 侧「子类持有独立的类变量表」一致；
                    // 子类自己声明的随后由 SET_ATTR 覆盖上去。
                    let mut class_vars: HashMap<String, Val> = HashMap::new();
                    // 祖先链的序号（M55）：最近的在最前，`是实例` 靠它走继承
                    let mut base_ids: Vec<usize> = Vec::new();
                    // 基类**本身**（M56）：`超()` 要沿链找「定义类」再取它的基类，
                    // 光有序号不够（序号没法反查回类对象）。用 `Rc` 共享。
                    let mut base_rc: Option<Rc<Class>> = None;
                    if has_base {
                        // 基类方法合并（简化：直接展开基类）
                        if let Val::Class(base) = self.stack.pop().unwrap_or(Val::None_) {
                            base_ids.push(base.id);
                            base_ids.extend(base.base_ids.iter().copied());
                            // ⚠️ 这里以前是**移动**（`for (k, v) in base.methods`）——
                            // 一旦要保留 base 本身就不能再移动，改成借用迭代 + clone。
                            for (k, v) in base.methods.iter() {
                                methods.entry(k.clone()).or_insert_with(|| v.clone());
                            }
                            for (k, v) in base.class_vars.borrow().iter() {
                                class_vars.insert(k.clone(), v.clone());
                            }
                            base_rc = Some(Rc::new(base));
                        }
                    }
                    self.stack.push(Val::Class(Class {
                        name,
                        methods,
                        class_vars: Rc::new(RefCell::new(class_vars)),
                        base_ids,
                        base: base_rc,
                        id: next_obj_id(),
                    }));
                }
                op::IMPORT => {
                    let name = self.module.names[ins.a as usize].clone();
                    let src = ins.b;
                    if src != 0 {
                        return Err(err("运行期错误", format!("Rust 引擎不支持「从 python/本地包」导入「{name}」")));
                    }
                    let modv = stdlib_module(&name)?;
                    self.stack.push(modv);
                }
                op::STORE_LAST => {
                    self.last_value = self.stack.pop().unwrap_or(Val::None_);
                }
                op::SETUP_LOOP => {
                    let seq = self.frames[fi].block_seq;
                    self.frames[fi].block_seq += 1;
                    self.frames[fi].loops.push((self.stack.len(), ins.a, ins.b, seq));
                }
                op::POP_BLOCK => { self.frames[fi].loops.pop(); }
                op::BREAK_LOOP => {
                    return Ok(Flow::Signal(Signal::Break));
                }
                op::CONTINUE_LOOP => {
                    return Ok(Flow::Signal(Signal::Continue));
                }
                op::LOOP_SETUP => {
                    let v = self.stack.pop().unwrap_or(Val::None_);
                    let n = match v { Val::Int(i) => i, Val::Float(f) => f as i128, _ => return Err(err("类型错误", format!("「{}」不是有效的循环次数", display(&v)))) };
                    self.frames[fi].locals[ins.a as usize] = Val::Int(n);
                    self.frames[fi].locals_set[ins.a as usize] = true;
                }
                op::SETUP_TRY => {
                    let seq = self.frames[fi].block_seq;
                    self.frames[fi].block_seq += 1;
                    self.frames[fi].handlers.push(Handler {
                        catch_ip: ins.a, finally_ip: ins.b,
                        sp: self.stack.len(), seq,
                    });
                }
                op::POP_TRY => { self.frames[fi].handlers.pop(); }
                // 只查看不弹出：栈顶是循环信号就跳到「未匹配路径」
                // （先跑 finally，末尾的 THROW 再把信号重新抛出）
                op::CHECK_SIGNAL => {
                    if let Some(Val::LoopSignal(_)) = self.stack.last() {
                        ip = ins.a as usize;
                    }
                }
                op::THROW => {
                    let v = self.stack.pop().unwrap_or(Val::None_);
                    // 循环信号借道 finally：CHECK_SIGNAL 后的 THROW 会重新抛出它，
                    // 这时要还原成「信号」而不是错误（对齐 Python 侧的块栈语义）
                    if let Val::LoopSignal(code) = v {
                        return Ok(Flow::Signal(if code == 1 {
                            Signal::Break
                        } else {
                            Signal::Continue
                        }));
                    }
                    return Err(make_exception(v));
                }
                op::MATCH_EXC => {
                    let cond = self.stack.pop().unwrap_or(Val::None_);
                    let exc = self.stack.last().cloned().unwrap_or(Val::None_);
                    let m = exception_matches(&exc, &cond);
                    self.stack.push(Val::Bool(m));
                }
                op::END_FINALLY => {
                    // finally 跑完了：如果有挂起的返回，继续走「返回」流程
                    if let Some(pr) = self.frames[fi].pending_return.take() {
                        match self.step_return(fi, pr) {
                            ReturnStep::Jump(target) => { ip = target; }
                            ReturnStep::Done(v) => {
                                self.stack.truncate(base);
                                self.stack.push(v);
                                return Ok(Flow::Return);
                            }
                        }
                    }
                }
                op::HALT => return Ok(Flow::Halt),
                _ => return Err(err("运行期错误", format!("未知指令 {}", ins.op))),
            }
        }
    }

    fn dispatch_exception(&mut self, e: &JishiError, stop_at: Option<usize>) -> Result<bool, JishiError> {
        // 从当前帧向外找 handler
        while !self.frames.is_empty() {
            let fi = self.frames.len() - 1;
            if Some(fi) == stop_at { return Ok(false); }
            let h = self.frames[fi].handlers.last().cloned();
            if let Some(h) = h {
                let sp = h.sp;
                let catch_ip = h.catch_ip;
                self.frames[fi].handlers.pop();
                self.stack.truncate(sp);
                // M56：把异常**连同消息**压栈 —— 以前只压一个 `Val::ExcType(类型名)`
                // （源码注释自认是「简化为字符串标记」），于是 `打印(e)` 只给
                // 「类型错误」、`e.消息` 直接报「没有属性」。现在 `e` 是带内容的
                // 异常对象，与 Python / Node 侧对齐。
                self.stack.push(Val::Exc(Rc::new(ExcValue {
                    type_name: e.type_name.clone(),
                    message: e.message.clone(),
                    code: e.code.clone(),
                })));
                self.frames[fi].ip = catch_ip as usize;
                return Ok(true);
            }
            let base = self.frames[fi].base;
            self.frames.pop();
            self.stack.truncate(base);
        }
        Ok(false)
    }

    /// 分发循环信号（`中断`/`继续`）：最近的循环和最近的 try 谁更内层谁先接。
    ///
    /// 与 Python 侧 `_dispatch_exception` 同一套「就近原则」：
    /// - try 内的循环里「中断」→ 循环接住，try 正常结束后才走最终；
    /// - 循环内的 try 里「中断」→ 先穿最终、再被外层循环接住。
    /// 后者靠把信号当作一个值压栈、跳到 handler 的 catch_ip（CHECK_SIGNAL
    /// 之后的 finally → THROW 重新抛出）来兑现。
    fn dispatch_signal(&mut self, s: Signal, stop_at: Option<usize>) -> Result<bool, JishiError> {
        let code = match s { Signal::Break => 1i64, Signal::Continue => 2i64 };
        while !self.frames.is_empty() {
            let fi = self.frames.len() - 1;
            if Some(fi) == stop_at { return Ok(false); }
            let lp = self.frames[fi].loops.last().cloned();
            let h = self.frames[fi].handlers.last().cloned();
            let loop_wins = match (&lp, &h) {
                (None, _) => false,
                (Some(_), None) => true,
                (Some(l), Some(hh)) => l.3 > hh.seq,
            };
            if loop_wins {
                let (depth, cont, brk, _) = lp.unwrap();
                // **不要在这里弹循环块**（M34 修）：`继续` 只是跳回迭代头，
                // 循环还在；弹掉的话「继续之后还要中断」就找不到循环了
                // （报「中断/继续没有对应的循环」）。弹的动作归 POP_BLOCK，
                // 与 Python 侧 `_signal_needs_dispatch` 之后只 `del stack[depth:]`
                // + 改 ip 完全一致。
                self.stack.truncate(depth);
                self.frames[fi].ip = (match s { Signal::Break => brk, Signal::Continue => cont }) as usize;
                return Ok(true);
            }
            if h.is_some() {
                let h = h.unwrap();
                self.frames[fi].handlers.pop();
                self.stack.truncate(h.sp);
                self.stack.push(Val::LoopSignal(code));
                self.frames[fi].ip = h.catch_ip as usize;
                return Ok(true);
            }
            let base = self.frames[fi].base;
            self.frames.pop();
            self.stack.truncate(base);
        }
        Ok(false)
    }

    /// `返回` 的收尾一步：如果当前帧还有带 finally 的处理器，就去跑它（返回值挂起）；
    /// 否则直接完成返回。
    fn step_return(&mut self, fi: usize, value: Val) -> ReturnStep {
        let mut value = value;
        loop {
            match self.frames[fi].handlers.pop() {
                None => return ReturnStep::Done(value),
                Some(h) if h.finally_ip >= 0 => {
                    self.stack.truncate(h.sp);
                    self.frames[fi].pending_return = Some(value);
                    return ReturnStep::Jump(h.finally_ip as usize);
                }
                // 纯捕获块：返回不走它，弹掉继续往外找
                Some(_) => {}
            }
        }
    }

    /// 压入一个函数帧（`CALL` 到用户函数/方法、内建回调用户函数，两条路共用）。
    ///
    /// `set_ip` 给 `Some((帧号, 新 ip))` 时会把调用者的 ip 写回帧里——只有从
    /// `CALL` 指令进来才需要（`execute` 靠帧里的 ip 回到调用点）。从内建里
    /// 回调（见 `call_value`）不需要：调用者的 `execute_frame` 还在 Rust 栈上、
    /// 用它自己的局部 ip 继续跑。
    /// `call_site` = 这次调用发生在哪（`ins.line` / `ins.col`）——
    /// 调用链要的是**这个**（不是新帧里跑到第几行）。
    fn push_func_frame(&mut self, set_ip: Option<(usize, usize)>, func: Func,
                       args: Vec<Val>, knames: &[String], kwvals: &[Val],
                       base: usize, call_site: (i64, i64))
        -> Result<(), JishiError> {
        let sub_idx = func.code_idx;
        let (locals, locals_set, values) = {
            let sub = &self.module.codes[sub_idx];
            bind_args(sub, &args, knames, kwvals, func.defaults.as_deref())?
        };
        // 单元表布局 `[cellvars… , freevars…]`：`cellvars` 是本帧自己的（新造，
        // **各自独立**），`freevars` 来自闭包 —— `Rc` 克隆 ⇒ **与捕获者共享**。
        let mut cells = {
            let sub = &self.module.codes[sub_idx];
            new_cells(sub.cellvars.len())
        };
        cells.extend(func.cells.iter().cloned());
        {
            let sub = &self.module.codes[sub_idx];
            store_cells(sub, &mut cells, &values);
        }
        let nf = Frame {
            code_idx: sub_idx,
            ip: 0,
            base,
            call_line: if call_site.0 > 0 { Some(call_site.0) } else { None },
            call_col: if call_site.0 > 0 { Some(call_site.1) } else { None },
            locals,
            locals_set,
            cells,
            loops: Vec::new(),
            handlers: Vec::new(),
            pending_return: None,
            block_seq: 0,
        };
        if let Some((fi, ip)) = set_ip {
            self.frames[fi].ip = ip;
        }
        self.frames.push(nf);
        Ok(())
    }

    /// 从内建/方法里**同步**调用一个基石可调用对象，拿到返回值。
    ///
    /// 这是「数据流水线」（映射/过滤/归约/排序按）、`初始化`、自定义对象打印
    /// 这些东西得以存在的前提——在这之前，内建没有任何办法回过头去跑用户函数。
    ///
    /// 实现要点：压一个帧，然后在**自己的帧深**上停下来（`stop_at`），
    /// 返回值就留在栈顶。异常不会漏到外层帧：`dispatch_exception` 见到
    /// `stop_at` 就返回，错误顺着 `?` 传回调用方。
    fn call_value(&mut self, callee: Val, args: Vec<Val>) -> Result<Val, JishiError> {
        match callee {
            Val::Builtin(_, f) => f(&args, self),
            Val::Func(func) => self.call_frame(func, args, None),
            Val::BoundMethod(bm) => match *bm {
                BoundMethod::User { func, instance, .. } =>
                    self.call_frame(func, args, Some(Val::Instance(instance))),
                builtin => self.call_bound(builtin, &args),
            },
            other => Err(err("类型错误",
                format!("「{}」不能当函数调用", display(&other)))),
        }
    }

    /// 压帧跑到底，取回返回值（`self_arg` 为 Some 时插在实参最前面）。
    fn call_frame(&mut self, func: Func, args: Vec<Val>,
                  self_arg: Option<Val>) -> Result<Val, JishiError> {
        let mut all = Vec::with_capacity(args.len() + 1);
        if let Some(s) = self_arg { all.push(s); }
        all.extend(args);
        let depth = self.frames.len();
        let base = self.stack.len();
        // 同步调用（内建里回调基石函数）没有源码位置 —— 记 (0, 0)。
        // 这一帧本来也不会出现在用户看的报错里（它没有对应的源码行）。
        self.push_func_frame(None, func, all, &[], &[], base, (0, 0))?;
        self.execute(Some(depth))?;
        Ok(if self.stack.len() > base {
            self.stack.pop().unwrap_or(Val::None_)
        } else {
            Val::None_
        })
    }

    /// 造实例：先建空对象，再调它的 `初始化`（首参是实例自己）。
    fn make_instance(&mut self, class: Class, args: &[Val]) -> Result<JInst, JishiError> {
        let inst = Rc::new(RefCell::new(Instance {
            class: class.clone(), fields: HashMap::new(),
        }));
        if let Some(Val::Func(f)) = class.methods.get("初始化").cloned() {
            let mut all = Vec::with_capacity(args.len() + 1);
            all.push(Val::Instance(inst.clone()));
            all.extend(args.iter().cloned());
            self.call_value(Val::Func(f), all)?;
        } else if !args.is_empty() {
            return Err(err("类型错误", format!("类「{}」没有构造方法", class.name)));
        }
        Ok(inst)
    }

    /// 取迭代器（`GET_ITER`）。
    ///
    /// 自定义对象定义了 `迭代()` 就调它，把结果当可迭代对象（对齐 Python 侧
    /// `JishiInstance.__iter__`）；其余类型转给自由函数 `make_iterator`。
    fn make_iter(&mut self, obj: Val) -> Result<Val, JishiError> {
        if let Val::Instance(inst_rc) = &obj {
            let has = inst_rc.borrow().class.methods.contains_key("迭代");
            if has {
                let got = self.get_attr_on(obj.clone(), "迭代")?;
                let out = self.call_value(got, Vec::new())?;
                // 返回自身会无限循环——Python 侧也拦（见 M24 的迭代协议）
                if let (Val::Instance(a), Val::Instance(b)) = (&out, &obj) {
                    if Rc::ptr_eq(a, b) {
                        return Err(err("运行期错误",
                            "「迭代()」返回了对象自身，这样遍历会无限循环"));
                    }
                }
                return make_iterator(out);
            }
        }
        make_iterator(obj)
    }

    /// 取属性（`get_attr` 的方法版：`self` 仅为将来可能需要的 VM 相关属性留位）。
    fn get_attr_on(&mut self, obj: Val, attr: &str) -> Result<Val, JishiError> {
        get_attr(obj, attr, 0, 0)
    }

    /// 调用一个「已绑定」的方法（内建方法 + 用户函数回调）。
    ///
    /// 表里的方法都先 `borrow_mut()` 改**共享容器本身**，改完原变量能看见；
    /// 需要回调用户函数的那几个（映射/过滤/排序按/归约）先**快照**再回调，
    /// 否则「拿着 RefCell 的借用去回调」会撞上运行时借用冲突。
    fn call_bound(&mut self, bm: BoundMethod, args: &[Val]) -> Result<Val, JishiError> {
        match bm {
            BoundMethod::User { func, instance, .. } =>
                self.call_frame(func, args.to_vec(), Some(Val::Instance(instance))),
            BoundMethod::Builtin { name, receiver, .. } => {
                self.call_builtin_method(name, *receiver, args)
            }
        }
    }
    /// 内建方法分发表（列表 / 字典 / 文本 / 集合 / 文件）。
    ///
    /// `映射`/`过滤`/`排序按`/`归约` 需要回调用户函数，所以先**快照**容器内容
    /// 再回调——拿着 `RefCell` 的借用去回调，回调里又改同一个容器就会撞上
    /// 运行时借用冲突。
    fn call_builtin_method(&mut self, name: String, receiver: Val,
                           args: &[Val]) -> Result<Val, JishiError> {
        match (receiver, name.as_str()) {
            // -- 列表方法 --
            // 这里全部走 `borrow_mut()`：改的是共享容器本身，
            // 所以 `a.追加(3)` 之后原变量 `a` 也会变（M28 修的就是这个）。
            (Val::List(l), "追加") => { need_args(&name, args, 1, 1)?; l.borrow_mut().push(args[0].clone()); Ok(Val::None_) }
            (Val::List(l), "插入") => {
                need_args(&name, args, 2, 2)?;
                let i = as_int(&args[0])?;
                let mut l = l.borrow_mut();
                // ⚠️ Python 的 `list.insert(i, v)` 的**夹取规则**（R6.5）：
                // 负数先 `i + len`，再夹到 `[0, len]` —— 所以
                // `[1,2,3].插入(-1, 9)` 给 `[1, 2, 9, 3]`。
                // 以前这里是 `as_int(...) as usize` —— 负数直接变成巨大的 usize、
                // 再被 `min(len)` 夹到**末尾**，于是同一个程序在 Rust 上给
                // `[1, 2, 3, 9]`、在其它四个执行器上给 `[1, 2, 9, 3]`。
                let n = l.len() as i128;
                let at = if i < 0 { (n + i).max(0) } else { i.min(n) } as usize;
                l.insert(at, args[1].clone());
                Ok(Val::None_)
            }
            (Val::List(l), "弹出") => {
                need_args(&name, args, 0, 1)?;
                let mut l = l.borrow_mut();
                // 文案照 Python 的 `_m_list_pop`：空列表与下标越界**是两回事**
                // （`[1].弹出(9)` 里列表并不空）。R6.2 起两边都分开并逐字对齐。
                if l.is_empty() {
                    return Err(err("值错误", "列表是空的，没有东西可以弹出"));
                }
                // 负下标先折算（R6.5）：`列.弹出(-1)` 弹最后一个
                let v = if args.is_empty() {
                    l.pop()
                } else {
                    let i = as_int(&args[0])?;
                    norm_index(i, l.len()).map(|j| l.remove(j))
                };
                v.ok_or_else(|| err("索引错误", INDEX_MSG))
            }
            (Val::List(l), "排序") => { need_args(&name, args, 0, 0)?; l.borrow_mut().sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal)); Ok(Val::None_) }
            (Val::List(l), "反转") => { need_args(&name, args, 0, 0)?; l.borrow_mut().reverse(); Ok(Val::None_) }
            (Val::List(l), "清空") => { need_args(&name, args, 0, 0)?; l.borrow_mut().clear(); Ok(Val::None_) }
            (Val::List(l), "索引") => { need_args(&name, args, 1, 1)?; l.borrow().iter().position(|x| *x == args[0]).map(|i| Val::Int(i as i128)).ok_or_else(|| err("值错误", "找不到元素")) }
            (Val::List(l), "计数") => { need_args(&name, args, 1, 1)?; Ok(Val::Int(l.borrow().iter().filter(|x| **x == args[0]).count() as i128)) }
            (Val::List(l), "包含") => { need_args(&name, args, 1, 1)?; Ok(Val::Bool(l.borrow().contains(&args[0]))) }
            (Val::List(l), "移除") => {
                need_args(&name, args, 1, 1)?;
                let mut l = l.borrow_mut();
                if let Some(pos) = l.iter().position(|x| *x == args[0]) { l.remove(pos); Ok(Val::None_) }
                else {
                    // 照 Python 的 `_m_list_remove`：**要说清是哪个元素**
                    // （「列表里没有这个元素」查起来得自己猜是哪一个）
                    Err(err("值错误",
                            format!("列表里没有「{}」，没法移除", display(&args[0]))))
                }
            }
            // -- 字典方法 --
            (Val::Dict(d), "获取") => {
                need_args(&name, args, 1, 2)?;
                let dflt = if args.len() == 2 { args[1].clone() } else { Val::None_ };
                Ok(dict_get(&d.borrow(), &args[0]).unwrap_or(dflt))
            }
            (Val::Dict(d), "键") => { need_args(&name, args, 0, 0)?; Ok(list_new(d.borrow().iter().map(|(k, _)| k.clone()).collect())) }
            (Val::Dict(d), "值") => { need_args(&name, args, 0, 0)?; Ok(list_new(d.borrow().iter().map(|(_, v)| v.clone()).collect())) }
            (Val::Dict(d), "包含") => { need_args(&name, args, 1, 1)?; Ok(Val::Bool(dict_contains(&d.borrow(), &args[0]))) }
            // M36 补：`更新` / `弹出` 此前在 Rust 宿主上**根本没实现**
            // （报「方法调用未实现」——是明确报错，但确实是漏的）
            (Val::Dict(d), "更新") => {
                need_args(&name, args, 1, 1)?;
                match &args[0] {
                    Val::Dict(other) => {
                        let items = other.borrow().items.clone();
                        let mut cur = d.borrow_mut();
                        for (k, v) in items { dict_set(&mut cur, k, v); }
                        Ok(Val::None_)
                    }
                    other => Err(err("类型错误", format!(
                        "「更新」需要一个字典参数，但传了「{}」（{}）",
                        display(other), type_label(other)))),
                }
            }
            (Val::Dict(d), "合并") => {
                need_args(&name, args, 1, 1)?;
                match &args[0] {
                    Val::Dict(other) => {
                        // 返回**新字典**：双方都不动（与就地改的「更新」分工清楚）
                        let mut merged = d.borrow().clone();
                        let items = other.borrow().items.clone();
                        for (k, v) in items { dict_set(&mut merged, k, v); }
                        Ok(Val::Dict(Rc::new(RefCell::new(merged))))
                    }
                    other => Err(err("类型错误", format!(
                        "「合并」需要一个字典参数，但传了「{}」（{}）",
                        display(other), type_label(other)))),
                }
            }
            (Val::Dict(d), "弹出") => {
                need_args(&name, args, 1, 2)?;
                let mut cur = d.borrow_mut();
                if let Some(v) = dict_remove(&mut cur, &args[0]) {
                    Ok(v)
                } else if args.len() == 2 {
                    Ok(args[1].clone())
                } else {
                    Err(err("键错误", missing_key_msg(&args[0])))
                }
            }
            (Val::Dict(d), "清空") => { need_args(&name, args, 0, 0)?; d.borrow_mut().clear(); Ok(Val::None_) }
            // -- 字符串方法 --
            (Val::Str(s), "大写") => { need_args(&name, args, 0, 0)?; Ok(Val::Str(s.to_uppercase())) }
            (Val::Str(s), "小写") => { need_args(&name, args, 0, 0)?; Ok(Val::Str(s.to_lowercase())) }
            (Val::Str(s), "去空白") => { need_args(&name, args, 0, 0)?; Ok(Val::Str(s.trim().to_string())) }
            (Val::Str(s), "拆分") => {
                need_args(&name, args, 0, 1)?;
                let sep = args.first().map(display).unwrap_or_default();
                Ok(list_new(s.split(&sep).map(|p| Val::Str(p.to_string())).collect()))
            }
            (Val::Str(s), "查找") => {
                // M54 补：以前 Rust 宿主**根本没有这个方法**（`文本` 少这一个，
                // 是 M53 靠五引擎对拍偶然撞见的 —— 方法漂移检测就是为此建的）。
                need_args(&name, args, 1, 1)?;
                let needle = match &args[0] {
                    Val::Str(n) => n.clone(),
                    other => return Err(err("类型错误", format!(
                        "「查找」需要文本参数，得到了「{}」", type_label(other)))),
                };
                // ⚠️ `str::find` 给的是**字节**偏移，而基石/Python 的口径是
                // **字符**偏移（`"你好吗".查找("吗") == 2`）。含中文时两者不等，
                // 得把前缀的字符数出来。
                Ok(Val::Int(match s.find(&needle) {
                    Some(b) => s[..b].chars().count() as i128,
                    None => -1,
                }))
            }
            (Val::Str(s), "替换") => { need_args(&name, args, 2, 2)?; let a = display(&args[0]); let b = display(&args[1]); Ok(Val::Str(s.replace(&a, &b))) }
            (Val::Str(s), "包含") => { need_args(&name, args, 1, 1)?; Ok(Val::Bool(s.contains(&display(&args[0])))) }
            (Val::Str(s), "开头是") => { need_args(&name, args, 1, 1)?; Ok(Val::Bool(s.starts_with(&display(&args[0])))) }
            (Val::Str(s), "结尾是") => { need_args(&name, args, 1, 1)?; Ok(Val::Bool(s.ends_with(&display(&args[0])))) }
            (Val::Str(s), "转整数") => { need_args(&name, args, 0, 0)?; s.parse::<i128>().map(Val::Int).map_err(|_| err("值错误", format!("「{s}」不能转成整数"))) }
            (Val::Str(s), "转小数") => { need_args(&name, args, 0, 0)?; s.parse::<f64>().map(Val::Float).map_err(|_| err("值错误", "不能转成小数")) }
            // -- 列表高阶方法（数据流水线，M29）--
            (Val::List(l), "映射") => {
                need_args(&name, args, 1, 1)?;
                let f = ensure_callable(&args[0], "映射")?;
                let snapshot: Vec<Val> = l.borrow().clone();
                let mut out = Vec::with_capacity(snapshot.len());
                for x in snapshot {
                    out.push(self.call_value(f.clone(), vec![x])?);
                }
                Ok(list_new(out))
            }
            (Val::List(l), "过滤") => {
                need_args(&name, args, 1, 1)?;
                let f = ensure_callable(&args[0], "过滤")?;
                let snapshot: Vec<Val> = l.borrow().clone();
                let mut out = Vec::new();
                for x in snapshot {
                    let keep = self.call_value(f.clone(), vec![x.clone()])?;
                    if truthy(keep) { out.push(x); }
                }
                Ok(list_new(out))
            }
            (Val::List(l), "排序按") => {
                need_args(&name, args, 1, 1)?;
                let f = ensure_callable(&args[0], "排序按")?;
                let snapshot: Vec<Val> = l.borrow().clone();
                let mut keyed: Vec<(Val, Val)> = Vec::with_capacity(snapshot.len());
                for x in snapshot {
                    let k = self.call_value(f.clone(), vec![x.clone()])?;
                    keyed.push((k, x));
                }
                // 键之间必须两两可比：混着数字和文本时 Python 会 TypeError，
                // 我们在这儿先报中文错，别用「相等」糊过去（那会悄悄给出错顺序）。
                // 只和第一个键比一遍即可（同类型键彼此可比，这是传递的）。
                if let Some(first) = keyed.first().map(|p| p.0.clone()) {
                    for (k, _) in &keyed {
                        if k.partial_cmp(&first).is_none() {
                            return Err(err("类型错误",
                                "「排序按」算出来的排序键没法互相比较"));
                        }
                    }
                }
                keyed.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
                Ok(list_new(keyed.into_iter().map(|(_, x)| x).collect()))
            }
            (Val::List(l), "归约") => {
                need_args(&name, args, 2, 2)?;
                let f = ensure_callable(&args[0], "归约")?;
                let mut acc = args[1].clone();
                let snapshot: Vec<Val> = l.borrow().clone();
                for x in snapshot {
                    acc = self.call_value(f.clone(), vec![acc, x])?;
                }
                Ok(acc)
            }
            // -- 集合方法（M29）--
            // 与列表一样，改的是共享容器本身；`并集` 等返回**新**集合。
            (Val::Set(s), "添加") => { need_args(&name, args, 1, 1)?; s.borrow_mut().push(args[0].clone())?; Ok(Val::None_) }
            (Val::Set(s), "移除") => {
                need_args(&name, args, 1, 1)?;
                if s.borrow_mut().remove(&args[0]) {
                    Ok(Val::None_)
                } else {
                    Err(err("值错误",
                        format!("集合里没有「{}」，没法移除", display(&args[0]))))
                }
            }
            (Val::Set(s), "丢弃") => {
                need_args(&name, args, 1, 1)?;
                s.borrow_mut().remove(&args[0]);
                Ok(Val::None_)
            }
            (Val::Set(s), "包含") => { need_args(&name, args, 1, 1)?; Ok(Val::Bool(s.borrow().contains(&args[0]))) }
            (Val::Set(s), "并集") => {
                need_args(&name, args, 1, 1)?;
                let other = as_iterable(&args[0], "「并集」的另一个集合")?;
                let mut v = s.borrow().to_items(); v.extend(other); set_new(v)
            }
            (Val::Set(s), "交集") => {
                need_args(&name, args, 1, 1)?;
                let other = as_iterable(&args[0], "「交集」的另一个集合")?;
                let look = Lookup::build(&other);
                let v: Vec<Val> = s.borrow().iter().filter(|x| look.contains(x)).cloned().collect();
                set_new(v)
            }
            (Val::Set(s), "差集") => {
                need_args(&name, args, 1, 1)?;
                let other = as_iterable(&args[0], "「差集」的另一个集合")?;
                let look = Lookup::build(&other);
                let v: Vec<Val> = s.borrow().iter().filter(|x| !look.contains(x)).cloned().collect();
                set_new(v)
            }
            (Val::Set(s), "对称差") => {
                need_args(&name, args, 1, 1)?;
                let other = as_iterable(&args[0], "「对称差」的另一个集合")?;
                let look = Lookup::build(&other);
                let a: Vec<Val> = s.borrow().iter().filter(|x| !look.contains(x)).cloned().collect();
                let b: Vec<Val> = other.iter().filter(|x| !s.borrow().contains(x)).cloned().collect();
                let mut v = a; v.extend(b); set_new(v)
            }
            (Val::Set(s), "清空") => { need_args(&name, args, 0, 0)?; s.borrow_mut().clear(); Ok(Val::None_) }
            (Val::Set(s), "转列表") => { need_args(&name, args, 0, 0)?; Ok(list_new(s.borrow().to_items())) }
            (Val::Set(s), "复制") => { need_args(&name, args, 0, 0)?; set_new(s.borrow().to_items()) }
            // -- 文件方法（M29）--
            (Val::File(f), _) => file_call(&name, f, args),
            // -- 精确小数方法（M29）--
            (Val::Dec(d), _) => dec_call(&name, &d, args),
            _ => Err(err("类型错误", format!("「{name}」方法调用未实现"))),
        }
    }

    /// 值 → 文本（**唯一实现**，对齐 Python 侧 `jishi_repr`，M27/M29）。
    ///
    /// `top=true`：直接展示这个值（`打印(值)`、`文本(值)`）——文本不加引号，
    /// 用户要的就是那串字。`top=false`：嵌套在容器里——文本加引号，这样
    /// `打印(["甲", 变量])` 能看出哪个是文本。
    ///
    /// 比自由函数 `display` 强的地方是**有 VM**：自定义对象的「文本()」方法
    /// 要跑用户代码，`display` 拿不到 VM。容器里的对象也因此不再掉回
    /// `<甲 实例>`。
    fn format_value(&mut self, v: &Val, top: bool) -> Result<String, JishiError> {
        match v {
            Val::Instance(inst_rc) => {
                let has_text = inst_rc.borrow().class.methods.contains_key("文本");
                if !has_text {
                    return Ok(display(v));
                }
                let m = self.get_attr_on(v.clone(), "文本")?;
                let got = self.call_value(m, Vec::new())?;
                match got {
                    Val::Str(s) => Ok(s),
                    other => {
                        let cls = inst_rc.borrow().class.name.clone();
                        Err(err("类型错误", format!(
                            "类「{cls}」的「文本」方法返回了「{}」，要返回文本",
                            type_label(&other))))
                    }
                }
            }
            Val::List(l) => {
                let items = l.borrow().clone();
                let mut parts = Vec::with_capacity(items.len());
                for x in &items { parts.push(self.format_value(x, false)?); }
                Ok(format!("[{}]", parts.join(", ")))
            }
            Val::Dict(d) => {
                let pairs = d.borrow().clone();
                let mut parts = Vec::with_capacity(pairs.len());
                for (k, val) in &pairs {
                    parts.push(format!("{}: {}",
                        self.format_value(k, false)?, self.format_value(val, false)?));
                }
                Ok(format!("{{{}}}", parts.join(", ")))
            }
            Val::Set(s) => {
                let items = s.borrow().to_items();
                let mut parts = Vec::with_capacity(items.len());
                for x in &items { parts.push(self.format_value(x, false)?); }
                if parts.is_empty() { Ok("集合()".to_string()) }
                else { Ok(format!("{{{}}}", parts.join(", "))) }
            }
            other => Ok(if top { display(other) } else { py_repr(other) }),
        }
    }

    /// 找对象上名为 `name` 的「无参可调用」；没有则 None。
    ///
    /// 供 `进入上下文` / `退出上下文` 用（对齐 Python 侧 `_ctx_lookup`）。
    /// 只认**明确的**方法名，不做「任何名字都包一个绑定方法」那种宽松处理——
    /// 否则 `用 [1, 2] 为 x：` 会先被当成可以清理、再在调用时才报错。
    fn lookup_method(&mut self, obj: &Val, name: &str) -> Option<Val> {
        match obj {
            Val::Instance(inst_rc) => {
                if inst_rc.borrow().class.methods.contains_key(name) {
                    self.get_attr_on(obj.clone(), name).ok()
                } else {
                    None
                }
            }
            _ => match name {
                "退出" | "关闭" => match get_attr(obj.clone(), name, 0, 0) {
                    Ok(v @ Val::BoundMethod(_)) => Some(v),
                    _ => None,
                },
                _ => None,
            },
        }
    }
}

// ---------------------------------------------------------------------------
// 流程控制
// ---------------------------------------------------------------------------

enum Flow {
    /// 本帧执行完毕（跑到指令末尾或 RETURN），**调用方应弹掉它**。
    Return,    /// 刚压入一个新帧（CALL 到用户函数），**调用方绝不能弹帧**，直接继续循环。
    ///
    /// 以前这里复用 `Return`，于是 `execute` 每次函数调用后都把新帧弹掉，
    /// 外层接着执行那个还没拿到实参的 CALL → `fn_base` 下溢 → panic。
    /// 两个语义必须分开，否则「调用函数」这件事根本跑不起来。
    Call,
    Halt,
    Error(JishiError),
    Signal(Signal),
}

/// `返回` 的下一步（见 `VM::step_return`）。
enum ReturnStep {
    /// 还有 finally 要跑，跳到这个 ip（返回值已挂进 `frame.pending_return`）。
    Jump(usize),
    /// 没有 finally 了，可以完成返回。
    Done(Val),
}

// ---------------------------------------------------------------------------
// 语义函数
// ---------------------------------------------------------------------------

pub(crate) fn truthy(v: Val) -> bool {
    match v {
        Val::None_ => false,
        Val::Bool(b) => b,
        Val::Int(i) => i != 0,
        Val::Float(f) => f != 0.0,
        Val::Str(s) => !s.is_empty(),
        Val::Dec(d) => !d.is_zero(),
        Val::List(l) => !l.borrow().is_empty(),
        Val::Dict(d) => !d.borrow().is_empty(),
        Val::Set(s) => !s.borrow().is_empty(),
        _ => true,
    }
}

pub(crate) fn display(v: &Val) -> String {
    match v {
        Val::None_ => "空".to_string(),
        Val::Bool(true) => "真".to_string(),
        Val::Bool(false) => "假".to_string(),
        Val::Int(i) => i.to_string(),
        Val::Float(f) => format_float(*f),
        Val::Str(s) => s.clone(),
        // 原样片段显示成它本身（与 Python 侧 `_Raw.__str__` 同行为）
        Val::HtmlRaw(s) => s.clone(),
        Val::Dec(d) => d.to_string(),
        // 与 Python 的 `str(datetime)` 一致：`2026-09-01 00:00:00`
        Val::Date(d) => d.to_display(),
        Val::Table(t) => t.display(),
        Val::Slice(s) => slice_repr(s),
        Val::List(l) => {
            let items: Vec<String> = l.borrow().iter().map(py_repr).collect();
            format!("[{}]", items.join(", "))
        }
        Val::Dict(d) => {
            let items: Vec<String> = d.borrow().iter()
                .map(|(k, v)| format!("{}: {}", py_repr(k), py_repr(v))).collect();
            format!("{{{}}}", items.join(", "))
        }
        // 空集合不写成 `{}`——那是空字典（与 Python 侧 `JishiSet.__repr__` 一致）
        Val::Set(s) => {
            let items: Vec<String> = s.borrow().iter().map(py_repr).collect();
            if items.is_empty() { "集合()".to_string() }
            else { format!("{{{}}}", items.join(", ")) }
        }
        Val::Func(f) => format!("<函数 {}>", f.name),
        Val::Class(c) => format!("<类 {}>", c.name),
        // 自定义的「文本」方法需要 VM 才能调用，而 display 拿不到 VM——
        // 所以这里仍是落回写法；`打印`/`文本` 在**有 VM** 的地方先问「文本()」
        // （见内建「打印」与 `format_value`）。
        Val::Instance(i) => {
            let cls = i.borrow().class.name.clone();
            format!("<{} 实例>", cls)
        }
        Val::File(f) => {
            let h = f.borrow();
            format!("<文件 {}（{}）>", h.path, h.state())
        }
        Val::Module(n, _) => format!("<模块 {n}>"),
        Val::Builtin(n, _) => format!("<内建函数 {n}>"),
        Val::ExcType(t) => t.clone(),
        // 只在「信号穿过 finally」的内部流程里短暂出现，用户看不到
        Val::LoopSignal(c) => if *c == 1 { "中断".to_string() } else { "继续".to_string() },
        Val::BoundMethod(_) => "<方法>".to_string(),
        Val::Cell(_) => "<单元>".to_string(),
        // 与 Python 侧 `JishiSuper.__repr__` 一致
        Val::Super(sp) => format!("<超 {}>", sp.defining),
        // 与 Node 侧 `JishiError.render()` 同一套（M56）：
        // 有码时「错误 E2008：断言没通过（故意失败）」，没码时「错误（异常）：…」
        Val::Exc(e) => match &e.code {
            Some(code) => {
                let title = error_title(&e.type_name);
                if e.message.is_empty() || e.message == title {
                    format!("错误 {code}：{title}")
                } else {
                    format!("错误 {code}：{title}（{}）", e.message)
                }
            }
            None => format!("错误（{}）：{}", e.type_name, e.message),
        },
    }
}

pub(crate) fn py_repr(v: &Val) -> String {
    match v {
        Val::Str(s) => format!("'{s}'"),
        // 空/真/假 用中文——与 Python 侧 jishi_repr、JS 侧 pyRepr 一致（M27）
        Val::Bool(true) => "真".to_string(),
        Val::Bool(false) => "假".to_string(),
        Val::None_ => "空".to_string(),
        _ => display(v),
    }
}

pub(crate) fn format_float(f: f64) -> String {
    if f == f.trunc() && f.is_finite() { format!("{:.1}", f) } else { f.to_string() }
}

pub(crate) fn binop(op: i64, l: Val, r: Val) -> Result<Val, JishiError> {
    use Val::*;
    // 至少一边是精确小数 → 走十进制（0.1 + 0.2 才等于 0.3）
    if matches!(op, 0..=6) && has_dec(&l, &r) {
        return dec_binop(op, &l, &r);
    }
    let result = match op {
        // 整数加减乘用 checked：i128 也会到边界，release 模式下溢出是
        // **静默回绕**（给一个错的数）。到边界就报错——本宿主不做任意精度，
        // 但也绝不悄悄给错值（M32）。
        0 => match (l, r) { (Int(a), Int(b)) => Int(big_ok(a.checked_add(b))?), (a, b) => numeric_add(a, b)? },
        1 => match (l, r) { (Int(a), Int(b)) => Int(big_ok(a.checked_sub(b))?), (a, b) => numeric_sub(a, b)? },
        2 => match (l, r) { (Int(a), Int(b)) => Int(big_ok(a.checked_mul(b))?), (a, b) => numeric_mul(a, b)? },
        // ⚠️ 除零的**消息是空的**（Python 侧就是 `RunZeroDivisionError("")`）：
        // 标题已经是「不能除以零」，再来一条同义的消息会让 `e.消息` 与三个
        // Python 执行器不一样（R6.2 对齐）。渲染那边本来就会跳过空消息。
        3 => { let (a, b) = to_num_pair_op(l, r, "/")?; if b == 0.0 { return Err(err("除零错误", "")); } Float(a / b) }
        4 => { let (a, b) = to_num_pair_op(l, r, "//")?; if b == 0.0 { return Err(err("除零错误", "")); } Int((a / b).floor() as i128) }
        5 => { let (a, b) = to_num_pair_op(l, r, "%")?; if b == 0.0 { return Err(err("除零错误", "")); } Int(a as i128 % b as i128) }
        6 => {
            // 整数底数 + 非负整数指数 → 走整数幂：Python 里 `2 ** 64` 是 int
            // 而不是 float（M32）。用快速幂，免得 `1 ** 1000000000` 这类
            // 退化成十亿次循环；真溢出时由 big_ok 报错。
            if let (Int(a), Int(b)) = (l.clone(), r.clone()) {
                if b >= 0 {
                    let mut base = a;
                    let mut e = b as u128;
                    let mut acc: i128 = 1;
                    while e > 0 {
                        if e & 1 == 1 { acc = big_ok(acc.checked_mul(base))?; }
                        e >>= 1;
                        if e > 0 { base = big_ok(base.checked_mul(base))?; }
                    }
                    return Ok(Int(acc));
                }
            }
            let (a, b) = to_num_pair(l, r)?;
            Float(a.powf(b))
        }
        _ => return Err(err("运行期错误", "不支持的运算符")),
    };
    Ok(result)
}

fn numeric_add(a: Val, b: Val) -> Result<Val, JishiError> {
    // 报错分支要用到两个操作数，而 `match (a, b)` 会把它们移走 —— 先留一份
    // 副本（只在出错的那条路上才会用到）。
    let (la, lb) = (a.clone(), b.clone());
    match (a, b) {
        (Val::Int(x), Val::Float(y)) | (Val::Float(y), Val::Int(x)) => Ok(Val::Float(x as f64 + y)),
        (Val::Float(x), Val::Float(y)) => Ok(Val::Float(x + y)),
        (Val::Str(x), Val::Str(y)) => Ok(Val::Str(x + &y)),
        // `[1] + [2]` 造一个**新**列表，不改动任何一边（Python 语义）
        (Val::List(x), Val::List(y)) => {
            let mut out = x.borrow().clone();
            out.extend(y.borrow().iter().cloned());
            Ok(list_new(out))
        }
        _ => Err(err("类型错误", add_type_msg(&la, &lb))),
    }
}

fn numeric_sub(a: Val, b: Val) -> Result<Val, JishiError> {
    match (a, b) {
        (Val::Int(x), Val::Float(y)) => Ok(Val::Float(x as f64 - y)),
        (Val::Float(x), Val::Int(y)) => Ok(Val::Float(x - y as f64)),
        (Val::Float(x), Val::Float(y)) => Ok(Val::Float(x - y)),
        _ => Err(err("类型错误", "不支持的减法")),
    }
}

fn numeric_mul(a: Val, b: Val) -> Result<Val, JishiError> {
    match (a, b) {
        (Val::Int(x), Val::Float(y)) | (Val::Float(y), Val::Int(x)) => Ok(Val::Float(x as f64 * y)),
        (Val::Float(x), Val::Float(y)) => Ok(Val::Float(x * y)),
        (Val::Str(s), Val::Int(n)) | (Val::Int(n), Val::Str(s)) => Ok(Val::Str(s.repeat(n as usize))),
        // `[1, 2] * 2` 同样造新列表
        (Val::List(l), Val::Int(n)) | (Val::Int(n), Val::List(l)) => {
            let src = l.borrow();
            let mut out = Vec::new();
            for _ in 0..n.max(0) { out.extend(src.iter().cloned()); }
            Ok(list_new(out))
        }
        _ => Err(err("类型错误", "不支持的乘法")),
    }
}

/// `不能比较「1」和「a」` —— 照 Python 的 `runtime.compare_values`
/// （它把 Python 的 `TypeError` 翻译成这条；宿主原来只有一句「需要数字」，
/// 既没说是**哪两个值**，也没说是在**比较**）。R6.3 对齐。
pub(crate) fn cmp_type_msg(a: &Val, b: &Val) -> String {
    format!("不能比较「{}」和「{}」", display(a), display(b))
}

pub(crate) fn to_num_pair(a: Val, b: Val) -> Result<(f64, f64), JishiError> {
    to_num_pair_op(a, b, "+")
}

/// 同上，但出错时的文案**带上运算符**。
///
/// 照 Python 的 `_type_error_zh` 认的那条
/// `unsupported operand type(s) for -: 'int' and 'str'` →
/// 「「整数」和「文本」不能做「-」运算」。宿主原来只有一句「需要数字」，
/// 既没说是哪两个值、也没说是在做哪个运算（R6.3 对齐）。
pub(crate) fn to_num_pair_op(a: Val, b: Val, op: &str) -> Result<(f64, f64), JishiError> {
    let la = a.clone();
    let lb = b.clone();
    let x = match a { Val::Int(i) => i as f64, Val::Float(f) => f,
        _ => return Err(err("类型错误", arith_type_msg(&la, &lb, op))) };
    let y = match b { Val::Int(i) => i as f64, Val::Float(f) => f,
        _ => return Err(err("类型错误", arith_type_msg(&la, &lb, op))) };
    Ok((x, y))
}

/// `「整数」和「文本」不能做「-」运算`（Python 侧 `_type_error_zh` 的那条）。
pub(crate) fn arith_type_msg(a: &Val, b: &Val, op: &str) -> String {
    format!("「{}」和「{}」不能做「{op}」运算", type_label(a), type_label(b))
}

// ---------------------------------------------------------------------------
// 精确小数（M29）
// ---------------------------------------------------------------------------

/// 把值转成精确小数（对齐 Python 侧 `_to_decimal`）。
///
/// 文本也认（`精确("1") + "2"` 在 Python 侧就是 3）；布尔不认——
/// 布尔不是数字，放进来只会让 `精确(真)` 这种写法悄悄成立。
fn to_dec(v: &Val) -> Result<Dec, JishiError> {
    match v {
        Val::Dec(d) => Ok((**d).clone()),
        Val::Bool(b) => Err(err("类型错误", format!(
            "布尔值「{}」不能当精确小数", if *b { "真" } else { "假" }))),
        Val::Int(i) => Ok(Dec { coef: BigInt::from_i128(*i), exp: 0 }),
        Val::Float(f) => Dec::from_f64(*f).ok_or_else(|| err("类型错误",
            format!("「{f}」不能变成精确小数（不是有限数）"))),
        Val::Str(s) => Dec::parse(s).ok_or_else(|| err("类型错误",
            format!("不能把「{s}」当作精确小数"))),
        other => Err(err("类型错误", format!(
            "不能把「{}」转成精确小数", type_label(other)))),
    }
}

fn has_dec(l: &Val, r: &Val) -> bool {
    matches!(l, Val::Dec(_)) || matches!(r, Val::Dec(_))
}

/// 至少一边是精确小数的二元运算（+ - * / // % **）。
fn dec_binop(op: i64, l: &Val, r: &Val) -> Result<Val, JishiError> {
    let a = to_dec(l)?;
    let b = to_dec(r)?;
    let zero = b.is_zero();
    let out = match op {
        0 => a.add(&b),
        1 => a.sub(&b),
        2 => a.mul(&b),
        3 | 4 | 5 => {
            if zero { return Err(err("除零错误", "")); }
            let (q, rem) = a.divmod_floor(&b).unwrap();
            if op == 4 { q } else if op == 5 { rem } else { a.div(&b).unwrap() }
        }
        6 => {
            // 只支持整数次幂。非整数指数是**近似**运算，各执行器实现细节
            // 一定会漂移，宁可不支持也不要「看起来能跑、结果对不上」。
            let n = b.to_i64().ok_or_else(|| err("类型错误",
                format!("「**」的指数要整数，但给的是 {}", b.to_string())))?;
            a.powi(n).ok_or_else(|| err("值错误", "精确小数的幂算不出来（结果太极端）"))?
        }
        _ => return Err(err("类型错误", "精确小数不支持这个运算")),
    };
    Ok(Val::Dec(Rc::new(out)))
}

/// 至少一边是精确小数的比较（== != < > <= >=）。
fn dec_compare(op: i64, l: &Val, r: &Val) -> Result<Val, JishiError> {
    let a = to_dec(l)?;
    let b = to_dec(r)?;
    let ord = a.cmp(&b);
    let out = match op {
        0 => ord == std::cmp::Ordering::Equal,
        1 => ord != std::cmp::Ordering::Equal,
        2 => ord == std::cmp::Ordering::Less,
        3 => ord == std::cmp::Ordering::Greater,
        4 => ord != std::cmp::Ordering::Greater,
        5 => ord != std::cmp::Ordering::Less,
        _ => return Err(err("运行期错误", "不支持的比较")),
    };
    Ok(Val::Bool(out))
}

/// 方法：舍入 / 绝对值 / 转文本（对齐 Python 侧 DECIMAL_METHODS）。
fn dec_call(name: &str, d: &Dec, args: &[Val]) -> Result<Val, JishiError> {
    match name {
        "舍入" => {
            need_args(name, args, 0, 1)?;
            let digits = match args.first() {
                None => 2i128,
                Some(v) => match v { Val::Int(i) => *i, _ => return Err(err("类型错误",
                    "「舍入」的位数要是整数")) },
            };
            // 金额场景用「四舍五入」（HalfUp），而不是默认的银行家舍入——
            // 后者更「标准」但不合用户直觉
            // 再按定点写法重新构造一遍：Python 侧最后一步是
            // `Decimal(format(rounded, "f"))`，舍到十位/百位时会得到
            // `1.2E+3`，那在金额场景里没人想看——换回 `1200`（M32）
            let r = d.quantize10(-(digits as i32), Round::HalfUp);
            let back = Dec::parse(&r.to_fixed_string()).unwrap_or(r);
            Ok(Val::Dec(Rc::new(back)))
        }
        "绝对值" => { need_args(name, args, 0, 0)?; Ok(Val::Dec(Rc::new(d.abs()))) }
        "转文本" => { need_args(name, args, 0, 0)?; Ok(Val::Str(d.to_string())) }
        _ => Err(err("类型错误", format!("「{name}」方法调用未实现"))),
    }
}


fn compare(op: i64, l: Val, r: Val) -> Result<Val, JishiError> {
    // 算术比较里只要有一边是精确小数就走十进制（`精确("0.3") == 0.3` 为真）。
    // 成员测试/同一性（6–9）**不走**：`精确("1") 在 [1]` 该按元素比，
    // 而不是让「精确」拒绝参加（对齐 Python 侧 apply_compare 的分工）。
    if matches!(op, 0..=5) && has_dec(&l, &r) {
        return dec_compare(op, &l, &r);
    }
    let out = match op {
        0 => l == r,
        1 => l != r,
        // 6–9 是 M23.2 的语义比较，Python 侧放在 CmpOp.IN/NOT_IN/IS/IS_NOT。
        // 这几个码 C VM 不认识、回落宿主，所以在这里实现它们是纯宿主侧工作。
        6 => contains(&r, &l)?,                                    // `l 在 r`
        7 => !contains(&r, &l)?,                                   // `l 不在 r`
        8 => identity_key(&l)? == identity_key(&r)?,               // `l 是 r`
        9 => identity_key(&l)? != identity_key(&r)?,               // `l 不是 r`
        _ => {
            // ✅ **整数对整数直接用 `i128` 比**（不走 f64）。两个理由：
            //
            // ① 正确性：原来的路径是「两边都 `as f64` 再比」，超过 2^53 就开始
            //    失真 —— `2 ** 62 > 2 ** 62 - 1` 在 Rust 上曾给出**假**，
            //    而另外四个执行器都精确比较（Python 任意精度 / C VM 是 int64 /
            //    Node 用 bigint）。整数比较是最不该出错的地方。
            // ② 性能：`当 i > 0` 这种热循环正是这一支，省掉两次 `clone` 与
            //    两次 f64 转换。
            //
            // ⚠️ `==` / `!=`（op 0/1）在上面走 `Val::eq`，本来就精确，不经过这里。
            if let (Val::Int(a), Val::Int(b)) = (&l, &r) {
                return Ok(Val::Bool(match op {
                    2 => a < b,
                    3 => a > b,
                    4 => a <= b,
                    5 => a >= b,
                    _ => return Err(err("运行期错误", "不支持的比较")),
                }));
            }
            // 混了浮点 / 精确小数（精确小数上面已分流）→ 仍走 f64。
            let (la, lb) = (l.clone(), r.clone());
            let (a, b) = to_num_pair(l, r)
                .map_err(|_| err("类型错误", cmp_type_msg(&la, &lb)))?;
            match op {
                2 => a < b,
                3 => a > b,
                4 => a <= b,
                5 => a >= b,
                _ => return Err(err("运行期错误", "不支持的比较")),
            }
        }
    };
    Ok(Val::Bool(out))
}

/// 成员测试（`item 在 container`）——与 Python 侧 `runtime.contains` 同一套语义：
/// 列表按值、字典按**键**、文本找子串（range 解出来的列表同理）。
fn contains(container: &Val, item: &Val) -> Result<bool, JishiError> {
    match container {
        Val::List(l) => Ok(l.borrow().iter().any(|x| x == item)),
        Val::Dict(d) => Ok(d.borrow().iter().any(|(k, _)| k == item)),
        Val::Str(s) => {
            let needle = match item { Val::Str(t) => t.clone(), other => display(other) };
            Ok(s.contains(&needle))
        }
        Val::Set(s) => Ok(s.borrow().contains(item)),
        _ => Err(err("类型错误",
            format!("「{}」不能在「{}」里找", display(item), display(container)))),
    }
}

/// 「是/不是」的同一性指纹（见 `identity_key`）。派生 `PartialEq` 即可：
/// 三个变体各自比各自的内容。
#[derive(PartialEq)]
enum IdKey {
    /// 空 / 真 / 假：单例，只和自己同一（`(是否为空, 是否真)`）
    Singleton(bool, bool),
    /// 数字 / 文本：按值比（`(类型名, 值的文本)`）
    Value(String, String),
    /// 列表 / 字典：按对象身份（同一份 `Rc` 的地址）
    Identity(usize),
}

/// 「是/不是」的同一性指纹——对齐 Python 侧 `_identity_snapshot`：
/// 空/真/假 是单例只和自己同一；数字/文本按**值**比（Python 侧也是这么定的，
/// 否则会有「有时真有时假」的随机感）；列表/字典按**对象身份**——
/// `Rc::as_ptr` 正好就是身份，两个内容相同的列表不是同一个东西。
///
/// 绑定方法（`a.方法`）仍**明确报错**：Python 里每次取属性都是一个新对象
/// （`a.方法 是 a.方法` 为假），而 Rust 侧要用「实例指针 + 方法名」判会得到
/// 真——那会给出与 Python 相反且反直觉的答案，宁可明确不支持。
fn identity_key(v: &Val) -> Result<IdKey, JishiError> {
    match v {
        // 类 / 函数 / 内建 / 异常类型 / 模块：以前一律「明确报错」，因为它们在
        // Rust 里是值类型。M32 给类与函数分配了创建序号，内建用函数指针、
        // 异常类型与模块用唯一名字——于是这几个也能像 Python 那样判同一性了。
        Val::Class(c) => Ok(IdKey::Identity(c.id)),
        Val::Func(f) => Ok(IdKey::Identity(f.id)),
        Val::Builtin(_, fp) => Ok(IdKey::Identity(*fp as usize)),
        Val::ExcType(n) => Ok(IdKey::Value("异常类型".into(), n.clone())),
        Val::Module(n, _) => Ok(IdKey::Value("模块".into(), n.clone())),
        Val::None_ => Ok(IdKey::Singleton(true, false)),
        Val::Bool(b) => Ok(IdKey::Singleton(false, *b)),
        Val::Int(i) => Ok(IdKey::Value("整数".into(), i.to_string())),
        Val::Float(f) => Ok(IdKey::Value("小数".into(), f.to_string())),
        Val::Str(s) => Ok(IdKey::Value("文本".into(), s.clone())),
        Val::Dec(d) => Ok(IdKey::Value("精确小数".into(), d.to_string())),
        Val::List(l) => Ok(IdKey::Identity(Rc::as_ptr(l) as usize)),
        Val::Dict(d) => Ok(IdKey::Identity(Rc::as_ptr(d) as usize)),
        Val::Set(s) => Ok(IdKey::Identity(Rc::as_ptr(s) as usize)),
        // 日期按 `Rc` 身份（Python 侧 datetime 走 `id()`）：
        // `d 是 d` 真、两次 `解析` 出来的相同日期互不相同
        Val::Date(d) => Ok(IdKey::Identity(Rc::as_ptr(d) as usize)),
        // 表格同样是引用身份（Python 的 `_Table` 没有自定义 `==`）
        Val::Table(t) => Ok(IdKey::Identity(Rc::as_ptr(t) as usize)),
        // 切片按**值**判（Python 的 `slice(1,2) == slice(1,2)` 为真）
        Val::Slice(s) => Ok(IdKey::Value(
            "切片".into(),
            format!("{:?}|{:?}|{:?}", s.start, s.stop, s.step),
        )),
        // 实例有了 `Rc` 身份——与 Python 侧 `_identity_snapshot` 对实例
        // 用 `id(value)` 一致（`a 是 a` 真，两个 `新建` 出来的互不相同）
        Val::Instance(i) => Ok(IdKey::Identity(Rc::as_ptr(i) as usize)),
        other => Err(err("运行期错误",
            format!("Rust 宿主暂不支持对「{}」用「是 / 不是」", display(other)))),
    }
}

impl PartialEq for Val {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Val::None_, Val::None_) => true,
            (Val::Bool(a), Val::Bool(b)) => a == b,
            // 布尔与数值：`真 == 1` / `假 == 0` / `真 == 1.0` 都是**真**。
            //
            // ⚠️ **只靠这一条就对不齐 —— 键的归一必须同时改**（见 `dict_key` /
            // `set_key`）：`==` 说「相等」而哈希键不同，字典 / 集合就会
            // 「查得到」变「查不到」。两处是一对。
            //
            // 🔴 这是 R6 主体做集合时**撞出来的既有分叉**：树遍历 / Python VM /
            // C VM / Node 四个一直给「真」（Python 里 `bool` 是 `int` 的子类，
            // 四侧都照这个语义），**只有 Rust 的 `==` 单独给「假」**——
            // 于是 `{真, 1}` 在 Rust 上是两个元素、`真 在 [1]` 是假。
            // 语料里没有这个形状，所以全语料体检一直绿的。
            (Val::Bool(a), Val::Int(b)) | (Val::Int(b), Val::Bool(a)) => {
                (*a as i128) == *b
            }
            (Val::Bool(a), Val::Float(b)) | (Val::Float(b), Val::Bool(a)) => {
                (if *a { 1.0 } else { 0.0 }) == *b
            }
            (Val::Int(a), Val::Int(b)) => a == b,
            (Val::Int(a), Val::Float(b)) | (Val::Float(b), Val::Int(a)) => *a as f64 == *b,
            (Val::Float(a), Val::Float(b)) => a == b,
            (Val::Str(a), Val::Str(b)) => a == b,
            // 精确小数按**数值**比（`精确("1.0") == 1` 为真，对齐 Python）
            (Val::Dec(a), Val::Dec(b)) => a.cmp(b) == std::cmp::Ordering::Equal,
            (Val::Date(a), Val::Date(b)) => a == b,
            // 表格没有自定义 `==`，按身份比（Python 同）
            (Val::Table(a), Val::Table(b)) => Rc::ptr_eq(a, b),
            (Val::Slice(a), Val::Slice(b)) => {
                a.start == b.start && a.stop == b.stop && a.step == b.step
            }
            (Val::Dec(a), b) => to_dec(b)
                .map(|d| a.cmp(&d) == std::cmp::Ordering::Equal).unwrap_or(false),
            (a, Val::Dec(b)) => to_dec(a)
                .map(|d| d.cmp(b) == std::cmp::Ordering::Equal).unwrap_or(false),
            // 容器的 `==` 是**按值**比较（Python 语义：`[1,2] == [1,2]` 为真）。
            // `Rc<RefCell<Vec<Val>>>` 的 PartialEq 正好逐元素比，递归到 `Val::eq`。
            (Val::List(a), Val::List(b)) => a == b,
            (Val::Dict(a), Val::Dict(b)) => a == b,
            // 集合相等与**顺序无关**（对齐 Python 侧 `JishiSet.__eq__`）
            (Val::Set(a), Val::Set(b)) => {
                let (x, y) = (a.borrow(), b.borrow());
                x.len() == y.len() && x.iter().all(|v| y.contains(v))
            }
            // 实例没有自定义 `==`，按身份比（Python 同）
            (Val::Instance(a), Val::Instance(b)) => Rc::ptr_eq(a, b),
            _ => false,
        }
    }
}

fn unary_neg(v: Val) -> Val {
    match v {
        Val::Int(i) => Val::Int(-i),
        Val::Float(f) => Val::Float(-f),
        Val::Dec(d) => Val::Dec(Rc::new(d.neg())),
        _ => Val::None_,
    }
}

// ---------------------------------------------------------------------------
// 内建函数
// ---------------------------------------------------------------------------

/// 位运算参数必须是整数（M36）。
///
/// 为什么不收布尔：Python 里 `True & 1` 给 1，但基石把布尔当**独立类型**，
/// 位运算混进逻辑值只会让人困惑 —— 明确报错，与 Python / JS 侧一致。
fn as_bit_int(v: &Val, func: &str) -> Result<i128, JishiError> {
    match v {
        Val::Int(i) => Ok(*i),
        _ => Err(err("类型错误", format!(
            "「{}」需要整数，但收到「{}」（{}）", func, display(v), type_label(v)))),
    }
}

/// 位移位数：非负整数，且不能大到把内存撑爆（上限与 Python / JS 侧一致）。
fn shift_count(v: &Val, func: &str) -> Result<i128, JishiError> {
    let n = as_bit_int(v, func)?;
    if n < 0 {
        return Err(err("类型错误",
                       format!("「{}」的位数不能是负数（收到 {}）", func, n)));
    }
    if n > 1_000_000 {
        return Err(err("类型错误",
                       format!("「{}」的位数太大（收到 {}，上限 1000000）", func, n)));
    }
    Ok(n)
}

fn new_builtins() -> HashMap<String, Val> {
    let mut m = HashMap::new();
    m.insert("打印".to_string(), Val::Builtin("打印", |args, vm| {
        // 走 format_value（不是 display）：顶层文本**不带引号**、空/真/假中文，
        // 而且自定义对象的「文本()」方法在这儿才跑得起来（需要 VM）。
        let mut parts = Vec::with_capacity(args.len());
        for a in args {
            parts.push(vm.format_value(a, true)?);
        }
        vm.output.push_str(&parts.join(" "));
        vm.output.push('\n');
        Ok(Val::None_)
    }));
    m.insert("输入".to_string(), Val::Builtin("输入", |args, vm| {
        need_builtin_args("输入", args, 0, 1)?;
        // 提示语写到输出流（不换行），与 Python 的 `input(prompt)` 一致
        if let Some(p) = args.first() {
            if !matches!(p, Val::None_) {
                let text = vm.format_value(p, true)?;
                if !text.is_empty() {
                    vm.output.push_str(&text);
                }
            }
        }
        let mut line = String::new();
        let n = std::io::stdin().read_line(&mut line).unwrap_or(0);
        if n == 0 && line.is_empty() {
            // 与 Python / Node 侧同一套说法（两边都是「输入结束」）
            return Err(err("输入结束", "输入已经结束，程序没有更多内容可读了"));
        }
        // 只去掉行尾换行（`\n` 或 `\r\n`）
        Ok(Val::Str(line.trim_end_matches(['\n', '\r']).to_string()))
    }));
    m.insert("整数".to_string(), Val::Builtin("整数", |args, _vm| {
        need_builtin_args("整数", args, 1, 1)?;
        // 归类与 Python 侧 `_b_int` 一致（M41）：文本转不动 → **值错误**；
        // 类型本身不对（列表/字典/集合）→ **类型错误**。
        match &args[0] {
            Val::Int(i) => Ok(Val::Int(*i)),
            Val::Float(f) => Ok(Val::Int(*f as i128)),
            Val::Bool(b) => Ok(Val::Int(if *b { 1 } else { 0 })),
            Val::Str(s) => s.parse::<i128>().map(Val::Int)
                .map_err(|_| err("值错误", format!("「{s}」不能转成整数"))),
            other => Err(err(
                "类型错误",
                format!("「{}」不能转成整数", display(other)),
            )),
        }
    }));
    m.insert("小数".to_string(), Val::Builtin("小数", |args, _vm| {
        need_builtin_args("小数", args, 1, 1)?;
        match &args[0] {
            Val::Int(i) => Ok(Val::Float(*i as f64)),
            Val::Float(f) => Ok(Val::Float(*f)),
            Val::Bool(b) => Ok(Val::Float(if *b { 1.0 } else { 0.0 })),
            Val::Str(s) => s.parse::<f64>().map(Val::Float)
                .map_err(|_| err("值错误", format!("「{s}」不能转成小数"))),
            other => Err(err(
                "类型错误",
                format!("「{}」不能转成小数", display(other)),
            )),
        }
    }));
    m.insert("文本".to_string(), Val::Builtin("文本", |args, vm| {
        need_builtin_args("文本", args, 1, 1)?;
        // 同样走 format_value：`文本(空)` 给「空」，`文本(对象)` 用「文本()」方法
        let mut out = String::new();
        for a in args {
            out.push_str(&vm.format_value(a, true)?);
        }
        Ok(Val::Str(out))
    }));
    // 文本插值脱糖的目标（M40）：**内部保留名**，用户写不出（Python 侧词法器
    // 不认 `$`），所以不会被 `导入 文本` 遮蔽。与 jishi/parser.py 的
    // INTERP_TO_TEXT 同名，语义与 `文本` 一致。
    m.insert("$文本".to_string(), Val::Builtin("$文本", |args, vm| {
        need_builtin_args("$文本", args, 1, 1)?;
        let mut out = String::new();
        for a in args {
            out.push_str(&vm.format_value(a, true)?);
        }
        Ok(Val::Str(out))
    }));
    m.insert("长度".to_string(), Val::Builtin("长度", |args, vm| {
        need_builtin_args("长度", args, 1, 1)?;
        match &args[0] {
            Val::Str(s) => Ok(Val::Int(s.chars().count() as i128)),
            Val::List(l) => Ok(Val::Int(l.borrow().len() as i128)),
            Val::Dict(d) => Ok(Val::Int(d.borrow().len() as i128)),
            Val::Set(s) => Ok(Val::Int(s.borrow().len() as i128)),
            // 定义了「迭代」的自定义对象也能求长度（M24.1）：既然能遍历，
            // 数一数有几个元素是自然的事，不必再要求作者额外写一个方法。
            Val::Instance(i) => {
                if i.borrow().class.methods.contains_key("迭代") {
                    let mut n = 0i128;
                    let it = vm.make_iter(args[0].clone())?;
                    let mut cur = it;
                    while iter_next(&mut cur)?.is_some() { n += 1; }
                    Ok(Val::Int(n))
                } else {
                    Err(err("类型错误", "没有长度"))
                }
            }
            _ => Err(err("类型错误", "没有长度")),
        }
    }));
    m.insert("范围".to_string(), Val::Builtin("范围", |args, _vm| {
        // M56：改用统一检查 —— 以前这里自己写了一句「范围需要 1 到 3 个参数」，
        // 没书名号、也少了「但传了 N 个」，与另两个宿主对不上。
        // ⚠️ 这个内建**不走**通用的 `need_builtin_args` 文案：Python 侧
        // `range` 那条单独写的是「最多接受 3 个参数」（R6.3 对齐）。
        if args.is_empty() || args.len() > 3 {
            return Err(err("类型错误", "「范围」最多接受 3 个参数"));
        }
        let (a, b, step) = match args.len() {
            1 => (0, as_int(&args[0])?, 1i128),
            2 => (as_int(&args[0])?, as_int(&args[1])?, 1i128),
            // 前面 need_builtin_args 已保证是 1~3 个，这里就是「3 个」
            _ => (as_int(&args[0])?, as_int(&args[1])?, as_int(&args[2])?),
        };
        let mut out = Vec::new();
        if step > 0 { let mut i = a; while i < b { out.push(Val::Int(i)); i += step; } }
        else if step < 0 { let mut i = a; while i > b { out.push(Val::Int(i)); i += step; } }
        Ok(list_new(out))
    }));
    // M41：`带下标` / `配对`（Python 的 enumerate / zip 同语义）。
    // 用 `seq_of` 而不是 `to_vec`——前者认得「定义了 `迭代` 的自定义对象」，
    // 与 Python 侧 `_as_iterable` 的口径一致。
    m.insert("带下标".to_string(), Val::Builtin("带下标", |args, vm| {
        if args.is_empty() || args.len() > 2 {
            return Err(err("类型错误", "「带下标」需要 1 到 2 个参数，例如 带下标(列表)"));
        }
        let seq = match seq_of(&args[..1], vm)? {
            Some(s) => s,
            None => match &args[0] {
                Val::Str(s) => s.chars().map(|c| Val::Str(c.to_string())).collect(),
                other => {
                    return Err(err(
                        "类型错误",
                        format!("「{}」不能当「带下标」的内容用", display(other)),
                    ));
                }
            },
        };
        let start = if args.len() == 2 { as_int(&args[1])? } else { 0 };
        let out: Vec<Val> = seq
            .into_iter()
            .enumerate()
            .map(|(i, v)| {
                let idx = Val::Int(start + i as i128);
                list_new(vec![idx, v])
            })
            .collect();
        Ok(list_new(out))
    }));
    m.insert("配对".to_string(), Val::Builtin("配对", |args, vm| {
        if args.is_empty() {
            return Err(err("类型错误", "「配对」至少要给一个可迭代对象"));
        }
        let mut seqs: Vec<Vec<Val>> = Vec::with_capacity(args.len());
        for a in args {
            let one = std::slice::from_ref(a);
            match seq_of(one, vm)? {
                Some(s) => seqs.push(s),
                None => match a {
                    Val::Str(s) => {
                        seqs.push(s.chars().map(|c| Val::Str(c.to_string())).collect());
                    }
                    other => {
                        return Err(err(
                            "类型错误",
                            format!("「{}」不能当「配对」的内容用", display(other)),
                        ));
                    }
                },
            }
        }
        let n = seqs.iter().map(|s| s.len()).min().unwrap_or(0);
        let out: Vec<Val> = (0..n)
            .map(|i| list_new(seqs.iter().map(|s| s[i].clone()).collect()))
            .collect();
        Ok(list_new(out))
    }));
    // 变参：至少 1 个（M55 补检查 —— 以前 0 个参数走各自的兜底，
    // 文案与另两个引擎对不上）
    m.insert("最大".to_string(), Val::Builtin("最大", |args, vm| {
        need_at_least_args("最大", args, 1)?;
        b_max(args, vm)
    }));
    m.insert("最小".to_string(), Val::Builtin("最小", |args, vm| {
        need_at_least_args("最小", args, 1)?;
        b_min(args, vm)
    }));
    m.insert("总和".to_string(), Val::Builtin("总和", |args, vm| {
        need_builtin_args("总和", args, 1, 1)?;
        b_sum(args, vm)
    }));
    m.insert("类型".to_string(), Val::Builtin("类型", |args, _vm| {
        need_builtin_args("类型", args, 1, 1)?;
        Ok(Val::Str(type_label(&args[0])))
    }));
    // 位运算内建（M36）：Python 侧**不做 `& | ^ << >>` 语法**（日常罕见，
    // 加运算符要动三执行器 + 两宿主 + 词法器），改成一小组内建。
    m.insert("位与".to_string(), Val::Builtin("位与", |args, _vm| {
        need_builtin_args("位与", args, 2, 2)?;
        Ok(Val::Int(as_bit_int(&args[0], "位与")? & as_bit_int(&args[1], "位与")?))
    }));
    m.insert("位或".to_string(), Val::Builtin("位或", |args, _vm| {
        need_builtin_args("位或", args, 2, 2)?;
        Ok(Val::Int(as_bit_int(&args[0], "位或")? | as_bit_int(&args[1], "位或")?))
    }));
    m.insert("位异或".to_string(), Val::Builtin("位异或", |args, _vm| {
        need_builtin_args("位异或", args, 2, 2)?;
        Ok(Val::Int(as_bit_int(&args[0], "位异或")? ^ as_bit_int(&args[1], "位异或")?))
    }));
    m.insert("位取反".to_string(), Val::Builtin("位取反", |args, _vm| {
        need_builtin_args("位取反", args, 1, 1)?;
        // `!a`（补码取反）= `-a - 1`，与 Python 的 `~a` 一致
        Ok(Val::Int(!as_bit_int(&args[0], "位取反")?))
    }));
    m.insert("左移".to_string(), Val::Builtin("左移", |args, _vm| {
        need_builtin_args("左移", args, 2, 2)?;
        let a = as_bit_int(&args[0], "左移")?;
        let n = shift_count(&args[1], "左移")?;
        if n >= 128 {
            // 基石在 Rust 宿主上整数到 i128 就到边界（与其它整数运算一致）：
            // 移出去就报错，**不给一个静默回绕的数**
            return Err(err("类型错误",
                           "「左移」的结果超出整数范围（Rust 宿主整数到 i128 就到边界）"));
        }
        a.checked_shl(n as u32).map(Val::Int).ok_or_else(|| {
            err("类型错误", "「左移」的结果超出整数范围（Rust 宿主整数到 i128 就到边界）")
        })
    }));
    m.insert("右移".to_string(), Val::Builtin("右移", |args, _vm| {
        need_builtin_args("右移", args, 2, 2)?;
        let a = as_bit_int(&args[0], "右移")?;
        let n = shift_count(&args[1], "右移")?;
        // 位数 ≥ 128 时 Python 给 0（或负数给 -1）——这里照做，别报错：
        // 「右移」的结果一定在范围内，没有边界问题
        if n >= 128 {
            return Ok(Val::Int(if a < 0 { -1 } else { 0 }));
        }
        Ok(Val::Int(a >> n))
    }));
    // 类型标注的参数校验（M24.3）：编译器把校验发射成函数自己的前导字节码，
    // 宿主实现这个内建就自动获得这项能力（M32 补齐，JS 侧是 M30 补的）。
    m.insert("检查实参".to_string(), Val::Builtin("检查实参", |args, _vm| {
        need_builtin_args("检查实参", args, 3, 3)?;
        check_args(&args[0], &args[1], &args[2])
    }));
    m.insert("反转".to_string(), Val::Builtin("反转", |args, _vm| {
        need_builtin_args("反转", args, 1, 1)?;
        match &args[0] {
            Val::Str(s) => Ok(Val::Str(s.chars().rev().collect())),
            Val::List(l) => { let mut x = l.borrow().clone(); x.reverse(); Ok(list_new(x)) }
            _ => Err(err("类型错误", "「反转」需要列表或文本")),
        }
    }));
    // -- 字符与码点（M55）--
    //
    // 口径：**「一个字符」= 一个 Unicode 码点**。Rust 的 `String` 是 UTF-8，
    // 所以不能按字节也不能按 `.len()` —— 一律走 `chars()`（迭代即码点）。
    m.insert("序数".to_string(), Val::Builtin("序数", |args, _vm| {
        need_builtin_args("序数", args, 1, 1)?;
        let s = match &args[0] {
            Val::Str(s) => s,
            other => return Err(err("类型错误", format!(
                "「序数」要一个字符，得到了「{}」", type_label(other)))),
        };
        let mut it = s.chars();
        let first = match it.next() {
            Some(c) => c,
            None => return Err(err("值错误", "「序数」要一个字符，但给的是空文本")),
        };
        if it.next().is_some() {
            return Err(err("值错误", format!(
                "「序数」要**正好一个**字符，但给的是 {} 个：「{s}」", s.chars().count())));
        }
        Ok(Val::Int(first as i128))
    }));
    m.insert("字符".to_string(), Val::Builtin("字符", |args, _vm| {
        need_builtin_args("字符", args, 1, 1)?;
        // 不收布尔（与位运算内建同一取舍）
        let n = match &args[0] {
            Val::Int(i) => *i,
            other => return Err(err("类型错误", format!(
                "「字符」要一个整数码点，得到了「{}」", type_label(other)))),
        };
        let cp = u32::try_from(n).ok()
            .and_then(char::from_u32)
            .ok_or_else(|| err("值错误", format!(
                "码点 {n} 超出范围，合法范围是 0 到 1114111")))?;
        Ok(Val::Str(cp.to_string()))
    }));
    // -- 精确小数（M29）--
    m.insert("精确".to_string(), Val::Builtin("精确", |args, _vm| {
        // M56：改用统一检查（以前少「，但传了 N 个」）
        need_builtin_args("精确", args, 1, 1)?;
        Ok(Val::Dec(Rc::new(to_dec(&args[0])?)))
    }));
    // -- 集合 / 文件 / 上下文（M29）--
    m.insert("集合".to_string(), Val::Builtin("集合", |args, _vm| {
        if args.len() > 1 {
            return Err(err("类型错误", format!(
                "「集合」最多接受 1 个参数，但传了 {} 个", args.len())));
        }
        if args.is_empty() { return set_new(Vec::new()); }
        let items = as_iterable(&args[0], "集合的初始内容")?;
        set_new(items)
    }));
    m.insert("打开".to_string(), Val::Builtin("打开", |args, _vm| {
        // M55：改用统一的检查 —— 文案与 Python / Node 侧逐字对齐
        need_builtin_args("打开", args, 1, 2)?;
        let path = match &args[0] {
            Val::Str(s) => s.clone(),
            other => return Err(err("类型错误", format!(
                "「打开」的第一个参数要是文件路径，得到了「{}」", type_label(other)))),
        };
        let mode = match args.get(1) {
            None => "读".to_string(),
            Some(Val::Str(s)) => s.clone(),
            Some(other) => return Err(err("类型错误", format!(
                "「打开」的第二个参数（模式）要是文本，得到了「{}」", type_label(other)))),
        };
        Ok(Val::File(open_file(&path, &mode)?))
    }));
    m.insert("$造模块".to_string(), Val::Builtin("$造模块", |args, _vm| {
        // 内部用（M52）：把「用基石写的标准库」的导出字典包成模块对象。
        // 与 Python 侧 `_b_make_module` 同义 —— `统计.求和(...)` 的语法
        // 因此一个字都不用改，`打印(统计)` 也还是 `<模块 统计>`。
        // 名字以 `$` 开头，用户写不出来（词法器不认这个字符）。
        if args.len() != 2 {
            return Err(err("类型错误", "$造模块 需要 2 个参数"));
        }
        let name = match &args[0] { Val::Str(s) => s.clone(), o => display(o) };
        match &args[1] {
            Val::Dict(d) => {
                // 本宿主的字典是**有序 Vec**（保插入顺序），模块表要 HashMap
                let mut table: HashMap<String, Val> = HashMap::new();
                for (k, v) in d.borrow().iter() {
                    let key = match k { Val::Str(s) => s.clone(), o => display(o) };
                    table.insert(key, v.clone());
                }
                Ok(Val::Module(name, table))
            }
            o => Err(err("类型错误",
                format!("$造模块 的第二个参数要传字典，得到了「{}」", display(o)))),
        }
    }));
    m.insert("进入上下文".to_string(), Val::Builtin("进入上下文", |args, vm| {
        // M56：改用统一检查（以前是「进入上下文 需要 1 个参数」—— 有空格、无书名号）
        need_builtin_args("进入上下文", args, 1, 1)?;
        let obj = args[0].clone();
        // 有「进入」就用它的返回值绑定名字（对应 Python 的 __enter__）；
        // 否则只要它能自己收拾（有「退出」或「关闭」）就绑定对象本身。
        if let Some(enter) = vm.lookup_method(&obj, "进入") {
            return vm.call_value(enter, Vec::new());
        }
        if vm.lookup_method(&obj, "退出").is_some()
            || vm.lookup_method(&obj, "关闭").is_some()
        {
            return Ok(obj);
        }
        Err(err("类型错误", format!(
            "「{}」不能当上下文管理器用；它既没有「进入」也没有「退出/关闭」方法",
            type_label(&obj))))
    }));
    m.insert("退出上下文".to_string(), Val::Builtin("退出上下文", |args, vm| {
        // M56：改用统一检查（同「进入上下文」）
        need_builtin_args("退出上下文", args, 1, 1)?;
        let obj = args[0].clone();
        // 优先「退出」，其次「关闭」。返回值忽略——不像 Python 那样能吞异常
        // （吞了会让「出问题快速找到」变难，有意不做）。
        for name in ["退出", "关闭"] {
            if let Some(f) = vm.lookup_method(&obj, name) {
                vm.call_value(f, Vec::new())?;
                return Ok(Val::None_);
            }
        }
        Ok(Val::None_)
    }));
    // 异常类型
    // ⚠️ 这份清单要与 Python 侧 `errors.py` 的异常表、Node 的 `EXCEPTION_TYPES`
    // 同宽 —— M55 补 `断言错误` 时发现它一直缺着（`捕获 断言错误` 在 Rust 上
    // 报「找不到名字」）。lang-spec 的 `exceptions` 节是权威清单。
    for t in ["异常", "运行期错误", "类型错误", "值错误", "索引错误", "键错误", "除零错误",
              "文件错误", "断言错误"] {
        m.insert(t.to_string(), Val::ExcType(t.to_string()));
    }
    // -- M55 补的两个内建（以前 Rust 侧根本没有，`--dump-builtins` 抓出来的）--
    m.insert("断言".to_string(), Val::Builtin("断言", |args, _vm| {
        need_builtin_args("断言", args, 1, 2)?;
        if truthy(args[0].clone()) {
            return Ok(Val::None_);
        }
        // 消息走「值→文本」的唯一实现（对齐 Python 侧 `jishi_repr(…, top=True)`）
        let msg = if args.len() > 1 { display(&args[1]) }
                  else { "条件不成立".to_string() };
        Err(err("断言错误", msg))
    }));
    m.insert("是实例".to_string(), Val::Builtin("是实例", |args, _vm| {
        need_builtin_args("是实例", args, 2, 2)?;
        let cls = match &args[1] {
            Val::Class(c) => c,
            other => return Err(err("类型错误", format!(
                "「是实例」的第二个参数要是一个类，但给的是「{}」", type_label(other)))),
        };
        Ok(Val::Bool(match &args[0] {
            // 沿**祖先链的序号**找（`base_ids` 在建类时算好）——与 Python 侧
            // `_instance_of` 同语义（那边是沿 `base` 指针走）
            Val::Instance(inst) => {
                let c = inst.borrow().class.clone();
                c.id == cls.id || c.base_ids.contains(&cls.id)
            }
            _ => false,
        }))
    }));
    // `超()`（M56 补）：编译器把 `超().方法(x)` 脱糖成 `超(自身, "定义类名")`，
    // 于是它是个**运行期内建** —— 两个宿主以前都没注册它，`超()` 在宿主上
    // 一直报「找不到名字「超」」。
    m.insert("超".to_string(), Val::Builtin("超", |args, _vm| {
        need_builtin_args("超", args, 2, 2)?;
        let inst = match &args[0] {
            Val::Instance(i) => i.clone(),
            other => return Err(err("类型错误", format!(
                "「超()」只能用在自己的方法里（第一参数该是实例，现在拿到的是「{}」）",
                type_label(other)))),
        };
        let want = display(&args[1]);
        // 在继承链上按名字找「定义类」（上限 64 防环，与 Python 侧一致）
        let (defining, base) = {
            let borrowed = inst.borrow();
            let mut cur: Option<&Class> = Some(&borrowed.class);
            let mut found: Option<(&Class, Option<Rc<Class>>)> = None;
            for _ in 0..64 {
                let Some(c) = cur else { break };
                if c.name == want { found = Some((c, c.base.clone())); break; }
                cur = c.base.as_deref();
            }
            match found {
                Some((c, b)) => (c.name.clone(), b),
                None => return Err(err("类型错误", format!(
                    "「超()」找不到定义类「{want}」——它不在「{}」的继承链上",
                    borrowed.class.name))),
            }
        };
        let base = base.ok_or_else(|| err("类型错误", format!(
            "类「{defining}」没有基类，用不了「超()」——它用来调基类的方法")))?;
        Ok(Val::Super(Rc::new(SuperVal { instance: inst, base, defining })))
    }));
    m
}

/// 整数溢出的统一出口（M32）。
///
/// `i128` 的边界约 1.7e38。超出时**明确报错**，而不是让 release 构建回绕成
/// 一个看似合法的数——那是最危险的静默算错。Python / JS 宿主是任意精度的，
/// 所以这条边界只属于本宿主。
pub(crate) fn big_ok(r: Option<i128>) -> Result<i128, JishiError> {
    r.ok_or_else(|| err("值错误",
        "整数运算结果太大，超出了 Rust 宿主能表示的范围（约 1.7e38）"))
}

/// 类型标注里认识的类型名 → 判定（对齐 Python 侧 `_TYPE_PREDICATES`）。
///
/// 返回 `None` 表示**不认识这个名字**——调用方应当跳过：作者自定义的标注
/// （`商品`、`订单`）只作文档，不该因为名字不在表里就误报。
fn matches_annotation(tname: &str, v: &Val) -> Option<bool> {
    Some(match tname {
        "整数" => matches!(v, Val::Int(_)),
        // 精确小数也是数字（Python 侧 `float` / `Decimal` 走同一条）
        "小数" => matches!(v, Val::Float(_) | Val::Dec(_)),
        "文本" => matches!(v, Val::Str(_)),
        "列表" => matches!(v, Val::List(_)),
        "字典" => matches!(v, Val::Dict(_)),
        "集合" => matches!(v, Val::Set(_)),
        "布尔" => matches!(v, Val::Bool(_)),
        "空" => matches!(v, Val::None_),
        "函数" => matches!(v, Val::Func(_) | Val::Class(_) | Val::Builtin(..)
                                | Val::ExcType(_) | Val::BoundMethod(_)),
        "任意" | "值" => true,
        _ => return None,               // 不认识的标注：只作文档
    })
}

/// 按类型标注校验实参。
///
/// `desc` 是扁平描述 `[参数名, 类型名, 参数名, 类型名, …]`，`values` 是实参
/// 列表（与 desc 里的参数一一对应）。报错文案与 Python / JS 侧逐字对齐。
fn check_args(desc: &Val, values: &Val, func_name: &Val) -> Result<Val, JishiError> {
    let (Val::List(d), Val::List(vs)) = (desc, values) else {
        return Ok(Val::None_);          // 形状不对就别拦（例如旧产物）
    };
    let d = d.borrow();
    let vs = vs.borrow();
    let fname = display(func_name);
    let mut i = 0;
    while i + 1 < d.len() {
        let idx = i / 2;
        if idx < vs.len() {
            let pname = display(&d[i]);
            let tname = display(&d[i + 1]);
            if let Some(ok) = matches_annotation(&tname, &vs[idx]) {
                if !ok {
                    return Err(err("类型错误", format!(
                        "函数「{fname}」的参数「{pname}」要求是「{tname}」，\
                         但传进来的是「{}」", type_label(&vs[idx]))));
                }
            }
        }
        i += 2;
    }
    Ok(Val::None_)
}

pub(crate) fn as_int(v: &Val) -> Result<i128, JishiError> {
    match v { Val::Int(i) => Ok(*i), _ => Err(err("类型错误", "需要整数")) }
}

/// 排序/最值时用的比较（没法比的键给「相等」，让调用方自己判可比性）。
pub(crate) fn cmp_vals(a: &Val, b: &Val) -> std::cmp::Ordering {
    a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal)
}

/// 单参数时，把这个参数当成「一整个序列」取出来；不是序列则 None。
///
/// 与 Python 侧 `_single_iterable_arg` 一致：文本刻意当**标量**处理
/// （`最大("abc")` 是那一个文本，不是三个字符）。
fn seq_of(args: &[Val], vm: &mut VM) -> Result<Option<Vec<Val>>, JishiError> {
    if args.len() != 1 { return Ok(None); }
    match &args[0] {
        Val::List(l) => Ok(Some(l.borrow().clone())),
        Val::Set(s) => Ok(Some(s.borrow().to_items())),
        Val::Dict(d) => Ok(Some(d.borrow().iter().map(|(k, _)| k.clone()).collect())),
        Val::Instance(i) => {
            if i.borrow().class.methods.contains_key("迭代") {
                let mut cur = vm.make_iter(args[0].clone())?;
                let mut out = Vec::new();
                while let Some(x) = iter_next(&mut cur)? { out.push(x); }
                Ok(Some(out))
            } else {
                Ok(None)
            }
        }
        _ => Ok(None),
    }
}

fn b_max(args: &[Val], vm: &mut VM) -> Result<Val, JishiError> {
    if let Some(seq) = seq_of(args, vm)? {
        // 照 Python 的 `_b_max`：**类型与文案都对齐**（Python 侧是
        // `RunTypeError` + 「「[]」不能求最大值，需要一个非空列表或若干数字」）。
        // ⚠️ 那条文案原来在 Python 侧是 `f"「{args}」"`（元组的 repr，单元素会
        // 多一个逗号：`「([],)」`）—— R6.3 两侧一起改成了 `_display(实参)`。
        return seq.into_iter().max_by(cmp_vals).ok_or_else(|| {
            err("类型错误", format!(
                "「{}」不能求最大值，需要一个非空列表或若干数字",
                display(&args[0])))
        });
    }
    args.iter().cloned().max_by(cmp_vals)
        .ok_or_else(|| err("类型错误", "「最大」至少要有一个参数"))
}

fn b_min(args: &[Val], vm: &mut VM) -> Result<Val, JishiError> {
    if let Some(seq) = seq_of(args, vm)? {
        return seq.into_iter().min_by(cmp_vals)
            .ok_or_else(|| err("值错误", "空序列求不了最小值"));
    }
    args.iter().cloned().min_by(cmp_vals)
        .ok_or_else(|| err("类型错误", "「最小」至少要有一个参数"))
}

/// 求和：全是整数就给整数，沾了小数就给小数，沾了精确小数就走十进制。
///
/// 精确小数那条要单独起算：`0 + Decimal` 在 Python 侧会 TypeError，
/// 而 `总和([精确("0.1"), …])` 不该因此失败（对齐 Python 侧 `_b_sum`）。
fn b_sum(args: &[Val], _vm: &mut VM) -> Result<Val, JishiError> {
    let v = args.first().ok_or_else(|| err("类型错误", "「总和」需要一个列表"))?;
    let items = as_iterable(v, "总和的参数")?;
    let has_dec = items.iter().any(|x| matches!(x, Val::Dec(_)));
    if has_dec {
        let mut acc = Dec::zero();
        for it in &items {
            acc = acc.add(&to_dec(it)?);
        }
        return Ok(Val::Dec(Rc::new(acc)));
    }
    let mut acc_int: i128 = 0;      // 大整数（M32）：整数求和不再回绕
    let mut acc_f: f64 = 0.0;
    let mut is_float = false;
    for it in &items {
        match it {
            Val::Int(i) => {
                if is_float { acc_f += *i as f64; }
                else { acc_int = big_ok(acc_int.checked_add(*i))?; }
            }
            Val::Float(f) => { if !is_float { acc_f = acc_int as f64; is_float = true; } acc_f += *f; }
            other => return Err(err("类型错误", format!(
                "「{}」不能求和，需要一个全是数字的列表", display(other)))),
        }
    }
    Ok(if is_float { Val::Float(acc_f) } else { Val::Int(acc_int) })
}

/// 标准库模块（M33）：具体实现在 `stdlib.rs`，这里只是一个转发。
///
/// 拆出去的理由：14 个模块、一百三十多个函数，全塞进 lib.rs 会把它撑到
/// 四千多行。零依赖约束下 `正则` / `加密` / `压缩` / `网络` 各自还有几百行，
/// 干脆按模块分文件（crypto.rs / regex.rs / zip.rs / http.rs / csv.rs）。
fn stdlib_module(name: &str) -> Result<Val, JishiError> {
    stdlib::module(name)
}

/// `--dump-stdlib`：这个宿主**实际带的标准库模块与函数**，打成 JSON（M51）。
///
/// 用途只有一个：给 `tests/test_m51_consistency.py` 当事实源。「Python 侧有、
/// 宿主没有」这种漂移以前是**静默**的 —— M50 第一批的宿主缺失就是手工测出来的。
pub fn dump_stdlib() -> String {
    stdlib::dump()
}

/// 目标不支持命名参数时**明确报错**（M51）。
///
/// 以前宿主对 kwargs 的态度是「收下、忘掉」：调用看着成功了，参数却没生效。
/// 那比报错难查一百倍 —— 宁可报错，也不要静默。
fn reject_kwargs(target: &str, knames: &[String]) -> Result<(), JishiError> {
    if knames.is_empty() {
        return Ok(());
    }
    Err(err("类型错误", format!(
        "{target}暂不支持命名参数（{}）—— 请改用位置参数", knames.join("、"))))
}

/// 命名参数 → 位置参数（M51）。
///
/// 以前 `Val::Builtin` 是 `bfn(&args, self)` —— **命名参数被静默丢掉**：
/// `加密.摘要("abc", 算法="md5")` 会悄悄用默认的 sha256，与 Python 侧结果不同
/// 还不报错。现在按「模块.函数」查 `stdlib_sigs`（由 `tools/gen_stdlib_sigs.py`
/// 从 Python 侧标准库源码生成，**零手抄、零漂移**）映射回位置参数；
/// 映射不了一律**明确报错**。
///
/// 异常类型统一用「类型错误」—— Python 侧也是 TypeError（`jishi/runtime.py`
/// 直接 `fn(*args, **kwargs)`），三执行器对拍才比得上。
///
/// 查不到 / 映射不了的四种情形与 Node 侧 `R.mapKwargs` 逐条对齐：
/// 名字对不上、被给两次、缺必填、**跳过了前面的可选参数**（宿主按位置调用，
/// 中间留空就得补 `空` 顶上，而 `空` 未必等价于「不给」，所以宁可报错也不猜）。
fn map_kwargs(qualified: &str, args: &[Val], knames: &[String], kwvals: &[Val])
    -> Result<Vec<Val>, JishiError> {
    if knames.is_empty() {
        return Ok(args.to_vec());
    }
    let (required, params, vararg) = match stdlib_sigs::sigof(qualified) {
        Some(s) => s,
        None => return Err(err("类型错误", format!(
            "宿主上「{qualified}」暂不支持命名参数（{}）—— 请改用位置参数",
            knames.join("、")))),
    };
    // ⚠️ **变参函数没有上限**（R6.4 补）：`压缩.打包(zip路径, *文件或目录)` 的
    // 参数名表里只有 `zip路径`，但合法调用可以给很多个 —— 不认这一条就会把
    // 正常的调用拦下来（比不检查更糟）。
    if vararg.is_none() && args.len() > params.len() {
        return Err(err("类型错误", format!(
            "函数「{qualified}」最多接受 {} 个参数，但传了 {} 个",
            params.len(), args.len())));
    }
    let mut slots: Vec<Option<Val>> = vec![None; params.len()];
    for (i, v) in args.iter().enumerate() {
        if i >= slots.len() {
            break;                       // 变参多出来的部分：下面拼到尾部
        }
        slots[i] = Some(v.clone());
    }
    for (k, v) in knames.iter().zip(kwvals.iter()) {
        let i = match params.iter().position(|p| p == k) {
            Some(i) => i,
            None => return Err(err("类型错误", format!(
                "函数「{qualified}」没有名为「{k}」的参数（可用：{}）",
                if params.is_empty() { "无".to_string() } else { params.join("、") }))),
        };
        if slots[i].is_some() {
            return Err(err("类型错误",
                format!("函数「{qualified}」的参数「{k}」被给了两次")));
        }
        slots[i] = Some(v.clone());
    }
    for i in 0..required {
        if slots[i].is_none() {
            return Err(err("类型错误", format!(
                "函数「{qualified}」缺少必填参数「{}」", params[i])));
        }
    }
    // 尾部的空缺截掉 —— 让宿主实现自己的默认值生效
    let mut n = params.len();
    while n > 0 && slots[n - 1].is_none() { n -= 1; }
    for i in 0..n {
        if slots[i].is_none() {
            return Err(err("类型错误", format!(
                "函数「{qualified}」的命名参数跳过了「{}」（它没有给值），\
                 而宿主是按位置调用的、中间不能留空 —— 请把「{}」也补上，\
                 或全部改用位置参数", params[i], params[i])));
        }
    }
    // 变参多出来的位置参数（`压缩.打包("a.zip", "f1", "f2")`）拼在**尾部** ——
    // 命名参数映射只覆盖前 `params.len()` 个位置槽。
    let mut out: Vec<Val> = slots[..n].iter().map(|v| v.clone().unwrap()).collect();
    if vararg.is_some() && args.len() > params.len() {
        out.extend(args[params.len()..].iter().cloned());
    }
    Ok(out)
}

/// 设置脚本收到的命令行参数（`jishi-rs 字节码.json 参数1 参数2`）。
///
/// 与 Python 侧 `系统._设参数`、Node 侧 `process.argv.slice(3)` 同一语义：
/// 不含字节码文件名本身。存成全局是因为 `Val::Builtin` 只能放**无捕获的
/// 函数指针**，闭包捕获不了参数。
pub fn set_args(args: Vec<String>) {
    stdlib::set_args(args);
}

// ---------------------------------------------------------------------------
// 属性 / 下标 / 调用
// ---------------------------------------------------------------------------

fn get_attr(obj: Val, attr: &str, line: i64, col: i64) -> Result<Val, JishiError> {
    match obj {
        Val::Module(_, attrs) => attrs.get(attr).cloned().ok_or_else(|| err("异常", format!("模块里没有「{attr}」"))),
        Val::Instance(inst_rc) => {
            let inst = inst_rc.borrow();
            if let Some(v) = inst.fields.get(attr) { return Ok(v.clone()); }
            // 类变量（M23.3）：实例上没有就看类的（构造时已把基类的并进来）
            if let Some(v) = inst.class.class_vars.borrow().get(attr) {
                return Ok(v.clone());
            }
            if let Some(f) = inst.class.methods.get(attr) {
                if let Val::Func(fu) = f {
                    // 绑定时把实例一起带上——调用时它会被放进第一个形参（自身）
                    return Ok(Val::BoundMethod(Box::new(BoundMethod::User {
                        func: fu.clone(), instance: inst_rc.clone(),
                        name: attr.to_string() })));
                }
            }
            let cls = inst.class.name.clone();
            Err(err("类型错误", format!("「{cls}」没有属性「{attr}」")))
        }
        Val::Class(c) => {
            if let Some(v) = c.class_vars.borrow().get(attr) { return Ok(v.clone()); }
            c.methods.get(attr).cloned().ok_or_else(|| err("类型错误", format!("类没有方法「{attr}」")))
        }
        // `超()`（M56）：从**定义类的基类**起找，方法绑定**原实例**。
        // 与 Python 侧 `get_attr` 同语义（那边看 `obj.defining.base`）。
        Val::Super(sp) => {
            if let Some(Val::Func(fu)) = sp.base.methods.get(attr) {
                return Ok(Val::BoundMethod(Box::new(BoundMethod::User {
                    func: fu.clone(), instance: sp.instance.clone(),
                    name: attr.to_string() })));
            }
            if let Some(v) = sp.base.class_vars.borrow().get(attr) { return Ok(v.clone()); }
            Err(err("类型错误", format!("基类「{}」里没有「{attr}」", sp.base.name)))
        }
        // 被捕获的异常（M56）：`e.类型` / `e.消息` 可读 —— 与 Python 侧
        // `JishiError` 的属性、Node 侧 `getAttr` 的 `JishiError` 分支一致。
        Val::Exc(e) => match attr {
            "类型" => Ok(Val::Str(e.type_name.clone())),
            "消息" => Ok(Val::Str(e.message.clone())),
            other => Err(err("类型错误", format!("「异常」没有属性「{other}」"))),
        },
        Val::Str(s) => str_method(attr, Val::Str(s), line, col),
        Val::List(l) => list_method(attr, Val::List(l), line, col),
        Val::Dict(d) => dict_method(attr, Val::Dict(d), line, col),
        Val::Set(s) => set_method(attr, Val::Set(s), line, col),
        Val::Dec(d) => dec_method(attr, d, line, col),
        Val::File(f) => file_method(attr, Val::File(f), line, col),
        // ⚠️ 显示**类型名**而不是值：Python 侧是 `type_name(obj)`，所以
        // `1.某某` 报的是「「整数」没有属性「某某」」；宿主原来打的是值
        // （「「1」没有属性…」），两个引擎连报错都不像（R6.2 对齐）。
        _ => Err(err("类型错误",
                     format!("「{}」没有属性「{attr}」", type_label(&obj)))),
    }
}

fn set_attr(obj: Val, attr: &str, val: Val) -> Result<(), JishiError> {
    match obj {
        Val::Module(_, mut attrs) => { attrs.insert(attr.to_string(), val); Ok(()) }
        // 改的是**共享**的字段表（`Rc<RefCell<…>>`），
        // 否则 `自身.字段 = 值` 只改到一份拷贝，调用方看不见（M29）
        Val::Instance(inst) => { inst.borrow_mut().fields.insert(attr.to_string(), val); Ok(()) }
        // 类变量（M23.3）：`类名.计数 = 5`，也承接编译器发射的「类体里 x = 1」
        // （那走的就是 DUP_TOP + SET_ATTR）。写进共享表，实例才看得见（M34）。
        Val::Class(c) => {
            c.class_vars.borrow_mut().insert(attr.to_string(), val);
            Ok(())
        }
        _ => Err(err("类型错误", "不能给这个对象赋值属性")),
    }
}

/// **负下标折算**（R6.5）：`列[-1]` 是最后一个元素 —— 与 Python 一致。
///
/// ⚠️ 计数用的是**元素的个数**：文本要按**码点**（`chars().count()`），
/// 不是字节数、也不是 UTF-16 码元数（`"😀"[-1]` 要拿到完整的 😀）。
///
/// 折算后仍越界就返回 `None`（调用方照常报「下标越界」）——
/// `列[-9]` 在长度 3 的列表上该报越界，而不是绕回开头。
/// 三个 Python 执行器底下就是 Python 的 list/str，行为天然一致。
fn norm_index(i: i128, len: usize) -> Option<usize> {
    let n = len as i128;
    let j = if i < 0 { n + i } else { i };
    if (0..n).contains(&j) {
        Some(j as usize)
    } else {
        None
    }
}

fn get_item(obj: Val, idx: Val) -> Result<Val, JishiError> {
    // 切片（M33）：列表按元素切、文本按字符切（与 Python 的 str 索引一致）
    if let Val::Slice(sl) = &idx {
        return match &obj {
            Val::List(l) => {
                let items = l.borrow();
                let (a, b, st) = slice_indices(sl, items.len())?;
                Ok(list_new(
                    slice_positions(a, b, st)
                        .into_iter()
                        .filter_map(|i| items.get(i as usize).cloned())
                        .collect(),
                ))
            }
            Val::Str(text) => {
                let chars: Vec<char> = text.chars().collect();
                let (a, b, st) = slice_indices(sl, chars.len())?;
                Ok(Val::Str(
                    slice_positions(a, b, st)
                        .into_iter()
                        .filter_map(|i| chars.get(i as usize))
                        .collect(),
                ))
            }
            other => Err(err(
                "类型错误",
                format!("「{}」不能切片，切片需要列表或文本", type_label(other)),
            )),
        };
    }
    match (obj, idx) {
        (Val::List(l), Val::Int(i)) => {
            let l = l.borrow();
            match norm_index(i, l.len()) {
                Some(j) => Ok(l[j].clone()),
                None => Err(err("索引错误", INDEX_MSG)),
            }
        }
        (Val::Dict(d), k) => dict_get(&d.borrow(), &k).ok_or_else(|| err("键错误", missing_key_msg(&k))),
        (Val::Str(s), Val::Int(i)) => {
            let n = s.chars().count();
            match norm_index(i, n) {
                Some(j) => Ok(Val::Str(s.chars().nth(j).unwrap().to_string())),
                None => Err(err("索引错误", STR_INDEX_MSG)),
            }
        }
        // 照 Python 的 `_type_error_zh`：`'int' object is not subscriptable`
        // 翻成「「整数」不能取下标」——宿主原来只有一句「不能取下标」，
        // 没说清是**哪个**东西不能取下标（R6.2 对齐）。
        (o, _) => Err(err("类型错误",
                          format!("「{}」不能取下标", type_label(&o)))),
    }
}

fn set_item(obj: Val, idx: Val, val: Val) -> Result<(), JishiError> {
    // 切片赋值（M33）：步长为 1 时可以变长（替换整个区间），
    // 步长不为 1 时长度必须精确匹配——与 Python 一致
    if let Val::Slice(sl) = &idx {
        let l = match &obj {
            Val::List(l) => l.clone(),
            other => {
                return Err(err(
                    "类型错误",
                    format!("「{}」不能按下标赋值", type_label(other)),
                ))
            }
        };
        let vals = match &val {
            Val::List(v) => v.borrow().clone(),
            other => {
                return Err(err(
                    "类型错误",
                    format!("切片赋值的值要是序列，得到了「{}」", type_label(other)),
                ))
            }
        };
        let (a, b, st) = slice_indices(sl, l.borrow().len())?;
        let mut l = l.borrow_mut();
        if st == 1 {
            let a = a.max(0) as usize;
            let b = b.max(0) as usize;
            let kill = b.saturating_sub(a).min(l.len().saturating_sub(a));
            for _ in 0..kill {
                l.remove(a);
            }
            for (k, v) in vals.into_iter().enumerate() {
                l.insert(a + k, v);
            }
            return Ok(());
        }
        let idxs = slice_positions(a, b, st);
        if idxs.len() != vals.len() {
            return Err(err(
                "数值错误",
                format!(
                    "切片赋值长度不匹配：目标有 {} 个位置，却给了 {} 个值",
                    idxs.len(),
                    vals.len()
                ),
            ));
        }
        for (i, v) in idxs.into_iter().zip(vals) {
            if let Some(slot) = l.get_mut(i as usize) {
                *slot = v;
            }
        }
        return Ok(());
    }
    match (obj, idx) {
        // 注意是 `borrow_mut`：改的是共享容器本身，调用者（原变量）能看见
        (Val::List(l), Val::Int(i)) => {
            let mut l = l.borrow_mut();
            // 负下标先折算（R6.5）：`列[-1] = 9` 改最后一个元素（Python 语义）
            match norm_index(i, l.len()) {
                Some(j) => {
                    l[j] = val;
                    Ok(())
                }
                None => Err(err("索引错误", INDEX_MSG)),
            }
        }
        (Val::Dict(d), k) => {
            dict_set(&mut d.borrow_mut(), k, val);
            Ok(())
        }
        // ⚠️ 要说清**是哪个东西**不能按下标赋值（R6.5）：原来只有一句
        // 「不能按下标赋值」，与 Python / Node 的「「文本」不能按下标赋值」
        // 对不上（R6.2 在「不能取下标」上修过同一类问题，这处当时漏了）。
        (o, _) => Err(err(
            "类型错误",
            format!("「{}」不能按下标赋值", type_label(&o)),
        )),
    }
}


fn need_args(name: &str, args: &[Val], lo: usize, hi: usize) -> Result<(), JishiError> {
    // 文案与 Python / Node 侧逐字对齐（M55：以前少了「但传了 N 个」，
    // 而且 lo == hi 时也说「1 到 1 个」，读起来像范围）。
    if args.len() < lo || args.len() > hi {
        Err(err("类型错误", format!(
            "方法「{name}」需要 {}，但传了 {} 个", need_span(lo, hi), args.len())))
    } else { Ok(()) }
}

/// 内建的参数个数检查。
///
/// ⚠️ 与 `need_args`（对象方法）分开，因为**称呼不同**：内建说「「长度」」、
/// 方法说「方法「追加」」。这与 Node 侧 `needBuiltinArgs` / `needArgs` 那一对
/// 是同一设计（M55）。文案：`「X」需要 1 个参数，但传了 0 个`。
fn need_builtin_args(name: &str, args: &[Val], lo: usize, hi: usize)
    -> Result<(), JishiError> {
    if args.len() < lo || args.len() > hi {
        Err(err("类型错误", format!(
            "「{name}」需要 {}，但传了 {} 个", need_span(lo, hi), args.len())))
    } else { Ok(()) }
}

/// 只要下限的检查（`最大` / `最小` 是变参的）。
fn need_at_least_args(name: &str, args: &[Val], lo: usize) -> Result<(), JishiError> {
    if args.len() < lo {
        Err(err("类型错误", format!(
            "「{name}」至少要 {lo} 个参数，但传了 {} 个", args.len())))
    } else { Ok(()) }
}

/// `1 个参数` / `1 到 2 个参数`
fn need_span(lo: usize, hi: usize) -> String {
    if lo == hi { format!("{lo} 个参数") } else { format!("{lo} 到 {hi} 个参数") }
}

// 简化：字符串/列表/字典方法直接返回 BoundMethod
//
// ⚠️ **这 6 张 `*_METHOD_NAMES` 是「对象方法」在本宿主的单一事实源**（M54）：
// ① 分派时用它挡不认识的名字；② `dump_methods()` 把它报给
// `tests/test_m51_consistency.py` 做漂移检测。**两者读同一张表，所以不会各报各的**
// —— 这是「宿主自报家底」能当真的前提（M51 定的规矩）。
// 加/删方法只改这里一处，别在下面的 `match` 里偷偷多写一个分支。
const LIST_METHOD_NAMES: &[&str] = &[
    "追加", "插入", "移除", "弹出", "排序", "反转", "清空", "索引", "计数", "包含",
    "映射", "过滤", "排序按", "归约",
];
const DICT_METHOD_NAMES: &[&str] = &[
    "获取", "键", "值", "包含", "更新", "合并", "弹出", "清空",
];
const STR_METHOD_NAMES: &[&str] = &[
    "拆分", "替换", "查找", "大写", "小写", "去空白", "开头是", "结尾是", "包含",
    "转整数", "转小数",
];
const SET_METHOD_NAMES: &[&str] = &[
    "添加", "移除", "丢弃", "包含", "并集", "交集", "差集", "对称差", "清空",
    "转列表", "复制",
];
const FILE_METHOD_NAMES: &[&str] = &[
    "读", "读行", "读所有行", "写", "写行", "关闭", "刷新", "位置", "定位",
];
const DEC_METHOD_NAMES: &[&str] = &["舍入", "绝对值", "转文本"];

/// 认名字就包成 `BoundMethod`，不认识就**在取属性时**报错。
///
/// ⚠️ 为什么必须在这里挡：直接把任何名字包成 `BoundMethod`，`列表.不存在()` 就
/// 变成「运行到调用时才报错」，**位置和措辞都差**（M30 的老坑，`set_method` 的注释
/// 里记着）。以前只有 集合/文件/小数 三个有这道门，列表/字典/文本 是**到调用时**
/// 才由 `call_builtin_method` 的兜底报「方法调用未实现」—— M54 统一了。
fn gated_method(names: &[&str], kind: &str, name: &str, receiver: Val,
                line: i64, col: i64)
    -> Result<Val, JishiError> {
    if !names.contains(&name) {
        // 文案与提示都照 Python 的 `runtime.get_attr`：
        // 类型名要带「」（`「列表」没有方法「排续」`），而且**要告诉用户有哪些**——
        // 拼错方法名是最常见的错，提示里直接列出可用方法最省事（R6.3 对齐）。
        let mut avail: Vec<&str> = names.to_vec();
        avail.sort_unstable();
        return Err(err_with_hint(
            "类型错误",
            format!("「{kind}」没有方法「{name}」"),
            format!("可用的方法：{}", avail.join("、"))));
    }
    let bm = BoundMethod::Builtin {
        name: name.to_string(), receiver: Box::new(receiver), line, col };
    Ok(Val::BoundMethod(Box::new(bm)))
}

fn str_method(name: &str, receiver: Val, line: i64, col: i64) -> Result<Val, JishiError> {
    gated_method(STR_METHOD_NAMES, "文本", name, receiver, line, col)
}
fn list_method(name: &str, receiver: Val, line: i64, col: i64) -> Result<Val, JishiError> {
    gated_method(LIST_METHOD_NAMES, "列表", name, receiver, line, col)
}
fn dict_method(name: &str, receiver: Val, line: i64, col: i64) -> Result<Val, JishiError> {
    gated_method(DICT_METHOD_NAMES, "字典", name, receiver, line, col)
}
fn set_method(name: &str, receiver: Val, line: i64, col: i64) -> Result<Val, JishiError> {
    gated_method(SET_METHOD_NAMES, "集合", name, receiver, line, col)
}
fn file_method(name: &str, receiver: Val, line: i64, col: i64) -> Result<Val, JishiError> {
    gated_method(FILE_METHOD_NAMES, "文件", name, receiver, line, col)
}
fn dec_method(name: &str, d: Rc<Dec>, line: i64, col: i64) -> Result<Val, JishiError> {
    gated_method(DEC_METHOD_NAMES, "精确小数", name, Val::Dec(d), line, col)
}

/// `--dump-methods`：本宿主**实际带的「对象方法」**，打成 JSON（M54）。
///
/// 数据源就是上面那 6 张 `*_METHOD_NAMES` —— **与分派同源**，不是另抄的清单。
/// 所以这份「自报家底」报的是真的能力，不会糊弄检测。
pub fn dump_methods() -> String {
    let mut parts: Vec<String> = Vec::new();
    for (kind, names) in [
        ("列表", LIST_METHOD_NAMES),
        ("字典", DICT_METHOD_NAMES),
        ("文本", STR_METHOD_NAMES),
        ("集合", SET_METHOD_NAMES),
        ("文件", FILE_METHOD_NAMES),
        ("小数", DEC_METHOD_NAMES),
    ] {
        let mut sorted: Vec<&str> = names.to_vec();
        sorted.sort_unstable();
        let items: Vec<String> = sorted.iter()
            .map(|n| format!("\"{n}\""))
            .collect();
        parts.push(format!("\"{kind}\":[{}]", items.join(",")));
    }
    format!("{{{}}}", parts.join(","))
}

/// 内部用内建（不该出现在用户可见清单里）。
///
/// ⚠️ Python 侧靠**说明以「内部用」开头**判定（`runtime.is_internal_builtin`），
/// 而宿主侧的内建**没有 doc 字段**，所以这里**显式列名**。
/// 漏列一个就会被 `tests/test_m51_consistency.py` 的内建漂移检测抓住
/// （报成「Python 侧没有、Rust 却有」）—— 这就是这份清单敢手写的原因。
const INTERNAL_BUILTIN_NAMES: &[&str] = &["检查实参"];

/// `--dump-builtins`：本宿主**实际带的「内建函数」**，打成 JSON（M55）。
///
/// 与 `--dump-stdlib` / `--dump-methods` 同一个模式（宿主自报家底）。
/// 补这个是因为 M55 实测发现：**内建也有漂移，而且一直没有检测** ——
/// 加一个内建要同时动 Python / Node / Rust 三处，漏一处就是
/// 「在某个引擎上「没有定义过这个名字」」，而 M51/M54 的检测只覆盖
/// 标准库函数与对象方法，**内建一条都没测**（M50 那批宿主缺失就是这么漏的）。
///
/// 过滤规则与 Python 侧 `is_internal_builtin` + `build_lang_spec` 对齐：
/// 去掉 `$` 开头的（用户写不出来）、去掉内部用的、只留真正的内建函数
/// （异常类型与 `空` 不是「函数」，不在内建函数清单里）。
pub fn dump_builtins() -> String {
    let table = new_builtins();
    let mut names: Vec<String> = table.iter()
        .filter(|(n, v)| !n.starts_with('$')
            && !INTERNAL_BUILTIN_NAMES.contains(&n.as_str())
            && matches!(v, Val::Builtin(_, _)))
        .map(|(n, _)| n.clone())
        .collect();
    names.sort();
    let items: Vec<String> = names.iter().map(|n| format!("\"{n}\"")).collect();
    format!("[{}]", items.join(","))
}

/// 检查一个参数确实可调用（`映射`/`过滤`/`排序按`/`归约` 的报错要说清是哪个方法）。
fn ensure_callable(v: &Val, name: &str) -> Result<Val, JishiError> {
    let ok = matches!(v, Val::Func(_) | Val::Builtin(_, _) | Val::BoundMethod(_));
    if !ok {
        return Err(err("类型错误", format!(
            "「{name}」需要一个函数作为参数，但给的是「{}」", type_label(v))));
    }
    Ok(v.clone())
}

/// 把一个值当成可迭代对象取元素（对齐 Python 侧 `_as_iterable`）。
fn as_iterable(v: &Val, what: &str) -> Result<Vec<Val>, JishiError> {
    match v {
        Val::List(l) => Ok(l.borrow().clone()),
        Val::Set(s) => Ok(s.borrow().to_items()),
        Val::Str(s) => Ok(s.chars().map(|c| Val::Str(c.to_string())).collect()),
        Val::Dict(d) => Ok(d.borrow().iter().map(|(k, _)| k.clone()).collect()),
        other => Err(err("类型错误", format!(
            "「{}」不能当{what}用，需要列表、文本、集合或字典", display(other)))),
    }
}

fn unpack_values(v: Val, n: usize, star: i64) -> Result<Vec<Val>, JishiError> {
    // star >= 0 时是星号解包（M25）：该位置收「其余」成列表
    // 注意这里取的是**快照**（`.borrow().clone()`）：解包是只读操作，
    // 不该把原列表搬空。
    let seq: Vec<Val> = match v {
        Val::List(l) => l.borrow().clone(),
        Val::Str(s) => s.chars().map(|c| Val::Str(c.to_string())).collect(),
        // 字典解出来的是键（与 Python 侧一致）
        Val::Dict(d) => d.borrow().iter().map(|(k, _)| k.clone()).collect(),
        other => return Err(err_with_hint(
            "类型错误",
            format!("解包赋值需要列表，得到了「{}」", type_label(&other)),
            "列表、文本、集合都能解包（字典解出来的是键）".to_string())),
    };
    if star < 0 {
        if seq.len() != n {
            return Err(err_with_hint(
                "值错误",
                format!("解包需要 {n} 个值，实际有 {} 个", seq.len()),
                "想「收剩下的」可以写「*余」，例如 令 甲, *余 = 列表".to_string()));
        }
        return Ok(seq);
    }
    let star_pos = star as usize;
    let nhead = star_pos;
    let ntail = n - 1 - star_pos;
    if seq.len() < nhead + ntail {
        return Err(err("值错误",
            format!("解包至少需要 {} 个值，实际有 {} 个",
                    nhead + ntail, seq.len())));
    }
    let mut out: Vec<Val> = Vec::with_capacity(n);
    out.extend_from_slice(&seq[..nhead]);
    let mid_end = seq.len() - ntail;
    out.push(list_new(seq[nhead..mid_end].to_vec()));
    if ntail > 0 {
        out.extend_from_slice(&seq[mid_end..]);
    }
    Ok(out)
}

fn make_exception(v: Val) -> JishiError {
    match v {
        Val::ExcType(t) => err(&t, ""),
        // 重抛一个**捕获到的**异常（`抛出 e` / `捕获 … 再 抛出`）：消息要保住，
        // 否则一路丢成「错误（类型错误）：」（M56）
        Val::Exc(e) => err(&e.type_name, &e.message),
        Val::Str(s) => err("异常", s),
        other => err("异常", display(&other)),
    }
}

fn exception_matches(exc: &Val, cond: &Val) -> bool {
    // 右边（`捕获 X`）永远是异常**类型**
    let Val::ExcType(c) = cond else { return false };
    // 左边可能是类型本身，也可能是**捕获住的异常对象**（M56 起压的是后者）
    let t: &str = match exc {
        Val::ExcType(t) => t,
        Val::Exc(e) => &e.type_name,
        _ => return false,
    };
    if c == "异常" { return true; }
    let mut cur = t;
    loop {
        if cur == c { return true; }
        match EXC_PARENT.iter().find(|(child, _)| *child == cur) {
            Some((_, parent)) => cur = parent,
            None => return false,
        }
    }
}

/// 异常类型的**父子表**（子, 父）。R6.2 补全。
///
/// ⚠️ 这张表以前只写了三支（除零 / 类型 / 值错误），于是
/// **`捕获 运行期错误` 接不住 `键错误` / `索引错误`** —— 而语言规格 §8.9
/// 明写「基类能抓子类」，Python 侧又是拿真类继承（`isinstance`）判的，
/// 所以只有 Rust VM 一个引擎表现不同。
///
/// 📌 **这张表要跟 `jishi/errors.py` 的类继承逐项对齐**（Python 侧是
/// `RunError` 一家：除零 / 类型 / 值 / 索引 / 键 / 文件 / 断言 / 不可调用
/// 全是它的子类）。**加异常类型时两个地方都要动**，
/// `tests/test_m64_rust_vm_iter.py` 有一条对拍盯着它。
// ---------------------------------------------------------------------------
// 运行期错误的**类型名与文案**（R6.2）
// ---------------------------------------------------------------------------
//
// ⚠️ 这些字符串在 Python 侧是**错误类**，在宿主里没有类可用，只能写字面量。
// 于是它们成了一份「隐式的拷贝」—— 漂了就是「同一个程序在两个引擎里
// `e.类型` 不一样」，而 `捕获 X` 的匹配也跟着变。R6.2 实测：19 个常见运行期
// 错误形状里 **18 个**在四个引擎之间不一致（见 `build/_errshape.py` 与
// `tests/test_m65_runtime_errors.py`）。
//
// 📌 **加/改运行期错误时，Python 侧与这里要一起动。**

/// 下标越界的文案（Python 侧 `_INDEX_ZH` 那张表：列表 / 文本分开说）。
pub(crate) const INDEX_MSG: &str = "列表下标越界";
pub(crate) const STR_INDEX_MSG: &str = "文本下标越界";

/// `字典里没有键「x」` —— 与 Python 侧 `_key_error_zh` 逐字对齐。
///
/// ⚠️ 不能用 `display(&k)` 之外的**默认字符串化**（比如 debug 格式）：文本键
/// 会多一层引号，那就成了「另一门语言的 repr」问题（R3 的教训）。
pub(crate) fn missing_key_msg(k: &Val) -> String {
    match k {
        Val::Str(x) => format!("字典里没有键「{x}」"),
        Val::Bool(true) => "字典里没有键「真」".to_string(),
        Val::Bool(false) => "字典里没有键「假」".to_string(),
        Val::None_ => "字典里没有键「空」".to_string(),
        Val::Int(i) => format!("字典里没有键「{i}」"),
        // 浮点也走 `display`（与 `打印` 同一个写法）—— 不自己拼格式，
        // 免得又出现一份「浮点怎么写」的口径
        Val::Float(_) => format!("字典里没有键「{}」", display(k)),
        _ => "字典里没有这个键".to_string(),
    }
}

/// `「列表」只能和「列表」相加，不能和「整数」相加` —— 与 Python 侧
/// `_type_error_zh` 认出的那条 `can only concatenate X (not "Y") to X` 对齐。
///
/// 📌 这是**认得出的**那一种；认不出的另一半 Python 会保留英文原文，
/// 宿主这边给不出原文，只能也走这条 —— 探针里只钉可复现的那几个形状。
pub(crate) fn add_type_msg(a: &Val, b: &Val) -> String {
    let ta = type_label(a);
    let tb = type_label(b);
    // ⚠️ **两种形态都要认**（Python 侧由 `_type_error_zh` 认两套模式）：
    // * 左边是文本/列表 ⇒ Python 走的是 `can only concatenate X (not "Y") to X`
    //   → 「「列表」只能和「列表」相加，不能和「文本」相加」；
    // * 其它 ⇒ Python 走的是 `unsupported operand type(s) for +: 'int' and 'str'`
    //   → 「「整数」和「文本」不能做「+」运算」。
    // 第一版只写了前者，于是 `1 + "a"` 的文案两边不一样（R6.3 抓到）。
    match a {
        Val::Str(_) | Val::List(_) =>
            format!("「{ta}」只能和「{ta}」相加，不能和「{tb}」相加"),
        _ => format!("「{ta}」和「{tb}」不能做「+」运算"),
    }
}

/// 「找不到这个名字」（Python 侧 `SemanticNameError.title`，E0301）。
pub(crate) const T_NAME: &str = "找不到这个名字";
/// 「找不到这个函数」（Python 侧 `SemanticFuncError.title`，E0302）。
pub(crate) const T_FUNC: &str = "找不到这个函数";
/// 「这个东西不能调用」（Python 侧 `RunNotCallableError.title`，E2006）。
pub(crate) const T_NOT_CALLABLE: &str = "这个东西不能调用";

/// 「赋值之前被用到了」的统一文案（Python 侧 `runtime.unbound_local`）。
pub(crate) fn unbound_msg(name: &str) -> String {
    format!("名字「{name}」在赋值之前被用到了")
}

/// 通用提示语（Python 侧同一处的兜底 hint）—— **要带名字**，
/// 逐字对齐 `runtime.unbound_local` 的 f-string。
pub(crate) fn unbound_hint(name: &str) -> String {
    format!("「{name}」在这个函数里被赋过值，所以它是局部变量；\
函数一进来它还没值。想读外层同名的那个，就换个名字。")
}

pub(crate) const EXC_PARENT: &[(&str, &str)] = &[
    ("除零错误", "运行期错误"),
    ("类型错误", "运行期错误"),
    ("值错误", "运行期错误"),
    ("索引错误", "运行期错误"),
    ("键错误", "运行期错误"),
    ("文件错误", "运行期错误"),
    ("断言错误", "运行期错误"),
    ("运行期错误", "异常"),
];

// ---------------------------------------------------------------------------
// 迭代器
// ---------------------------------------------------------------------------

/// 把一个**快照**包成迭代器：**反转**后存进列表。
///
/// ⚠️ **为什么要反转**（R6.1 修，R6.0 量出来的）：迭代器的列表是「每次取一个、
/// 取完就弹」的，而**从头部弹（`Vec::remove(0)`）是 O(n)** —— 整趟遍历就是
/// **O(n²)**。实测：n = 2500/5000/10000/20000 → 35 / 151 / 636 / 2499 ms，
/// **规模翻倍、耗时 ×4**，2 万次空循环要 2.5 秒（对照：同一份基准里 `当` 循环
/// 是 7.1x，说明不是「循环都慢」，就是这一处）。
///
/// 反转之后 `pop()` 从头到尾**每步 O(1)**，而弹出的顺序**与原顺序完全一致**。
/// 安全性：这个列表是 `GET_ITER` 造出来的**私有快照**，除了 `FOR_ITER` 与
/// 循环的清理路径（`POP_TOP`）**没有任何人看它** —— 所以「里面是反的」
/// 不可能被观察到。
fn reversed_iter(items: Vec<Val>) -> Val {
    let mut v = items;
    v.reverse();
    list_new(v)
}

fn make_iterator(obj: Val) -> Result<Val, JishiError> {
    // 迭代器 = 「快照列表 + 从**尾部**弹」。
    //
    // 关键：**必须做快照拷贝**。以前列表是值类型，`Val::List(l)` 天然就是
    // 独立副本，所以「遍历不消耗原列表」是白捡的；换成共享容器后如果直接把
    // `l` 递出去，`遍历 x 在 a` 会把 `a` 逐元素搬空——那是错的。
    //
    // ⚠️ 顺序：`iter_next` 从**尾部**弹，所以这里必须**反转**（见 `reversed_iter`）。
    match obj {
        Val::List(l) => Ok(reversed_iter(l.borrow().clone())),
        Val::Set(s) => Ok(reversed_iter(s.borrow().to_items())),
        Val::Str(s) => Ok(reversed_iter(
            s.chars().map(|c| Val::Str(c.to_string())).collect())),
        Val::Dict(d) => Ok(reversed_iter(
            d.borrow().iter().map(|(k, _)| k.clone()).collect())),
        // 文件是**惰性**迭代：真句柄不能快照（那等于整读进内存），
        // 所以迭代器就是文件自己，每次 next 读一行。
        Val::File(_) => Ok(obj),
        other => Err(err("类型错误",
            format!("「{}」不能遍历，遍历需要列表、文本、集合、字典或文件",
                    display(&other)))),
    }
}

/// 取下一个元素：列表迭代器弹**尾**（快照是反转的，所以顺序正确、每步 O(1)），
/// 文件迭代器读一行。
fn iter_next(it: &mut Val) -> Result<Option<Val>, JishiError> {
    match it {
        Val::List(l) => {
            // ⚠️ `pop()` 而不是 `remove(0)` —— 后者每次搬移整个 Vec（O(n)），
            // 整趟遍历 O(n²)。见 `reversed_iter` 的说明与实测数据。
            Ok(l.borrow_mut().pop())
        }
        Val::File(f) => {
            let mut h = f.borrow_mut();
            h.check_read("遍历")?;
            Ok(h.read_line()?.map(Val::Str))
        }
        _ => Err(err("类型错误", "迭代器错误"))
    }
}

// ---------------------------------------------------------------------------
// 参数绑定
// ---------------------------------------------------------------------------

fn bind_args(code: &Code, args: &[Val], knames: &[String], kwvals: &[Val], defaults: Option<&[Val]>) -> Result<(Vec<Val>, Vec<bool>, Vec<Val>), JishiError> {
    let nparams = code.params.len();
    // 形参种类（M25）：0=普通 1=*参数 2=**选项
    let star_pos = code.param_kind.iter().position(|k| *k == 1);
    let dstar_pos = code.param_kind.iter().position(|k| *k == 2);
    // 能吃普通位置实参的个数 = *参数 之前的那些
    let npos = star_pos.or(dstar_pos).unwrap_or(nparams);

    if args.len() > npos && star_pos.is_none() {
        return Err(err("类型错误",
            format!("函数「{}」需要 {} 个参数，但传了 {} 个",
                    code.name, npos, args.len() + knames.len())));
    }

    let mut bound: HashMap<usize, Val> = HashMap::new();
    for (i, a) in args.iter().enumerate() {
        if i < npos { bound.insert(i, a.clone()); }
    }
    if let Some(sp) = star_pos {
        bound.insert(sp, list_new(args[npos.min(args.len())..].to_vec()));
    }
    // 未匹配的关键字收进 **选项
    let mut extra: Vec<(Val, Val)> = Vec::new();
    for (k, v) in knames.iter().zip(kwvals.iter()) {
        if let Some(pi) = code.params[..npos].iter().position(|p| p == k) {
            bound.insert(pi, v.clone());
        } else if let Some(dp) = dstar_pos {
            extra.push((Val::Str(k.clone()), v.clone()));
        } else {
            // 照 Python 的 `vm._bind_args`：要说清是**哪个函数**、以及
            // **它有哪些参数**（只报「没有参数」等于让用户去翻定义）。
            return Err(err_with_hint(
                "类型错误",
                format!("函数「{}」没有叫「{k}」的参数", code.name),
                format!("它的参数是：{}", code.params.join("、"))));
        }
    }
    if let Some(dp) = dstar_pos {
        bound.insert(dp, dict_new(extra));
    }
    if let Some(d) = defaults {
        for (i, dv) in d.iter().enumerate() {
            if i < npos && !bound.contains_key(&i) && !matches!(dv, Val::None_) {
                bound.insert(i, dv.clone());
            }
        }
    }
    let missing: Vec<&str> = code.params[..npos].iter().enumerate()
        .filter(|(i, _)| !bound.contains_key(i)).map(|(_, p)| p.as_str()).collect();
    if !missing.is_empty() {
        return Err(err("类型错误", format!("函数「{}」缺少参数：{}", code.name, missing.join("、"))));
    }
    let mut locals = vec![Val::None_; code.nlocals];
    // 形参落到的槽算「赋过值」——其余槽保持未赋值（读就报「找不到名字」，M54）
    let mut locals_set = vec![false; code.nlocals];
    let mut values = Vec::new();
    for i in 0..nparams {
        let v = bound[&i].clone();
        values.push(v.clone());
        let slot = code.param_local[i];
        if slot >= 0 {
            locals[slot as usize] = v;
            locals_set[slot as usize] = true;
        }
    }
    Ok((locals, locals_set, values))
}

fn store_cells(code: &Code, cells: &mut [JCell], values: &[Val]) {
    for (i, v) in values.iter().enumerate() {
        let ci = code.param_cell[i];
        if ci >= 0 {
            // 改**单元里的值**（不是换掉单元）：形参若是单元变量（被闭包捕获），
            // 换掉单元就等于闭包看不见这次赋值了（M54）。
            let mut c = cells[ci as usize].borrow_mut();
            c.value = v.clone();
            c.set = true;
        }
    }
}

/// 单元号 → 名字（布局是 `[cellvars… , freevars…]`，与 Python 侧
/// `VM._cell_name` / Node 侧 `_cellName` 逐条对齐）。
fn cell_name(code: &Code, idx: usize) -> String {
    if idx < code.cellvars.len() {
        return code.cellvars[idx].clone();
    }
    let j = idx - code.cellvars.len();
    if j < code.freevars.len() {
        return code.freevars[j].clone();
    }
    format!("?单元{idx}")
}

/// 值的全序比较（排序、`最大`/`最小`、`<` 这类都用它）。
///
/// **列表按字典序**逐项比（Python 的 `[1,2] < [1,3]` 就是这个规则）——
/// 这一支以前是漏的：`partial_cmp` 对列表返回 `None`，而 `列表.排序`
/// 把 `None` 当成「相等」，于是**排序静默失效**（`排名.排序()` 后顺序不变，
/// 谁也没发现，直到拿真实项目跑端到端才撞出来，M33）。
fn try_cmp_vals(a: &Val, b: &Val) -> Option<std::cmp::Ordering> {
    use std::cmp::Ordering;
    match (a, b) {
        (Val::None_, Val::None_) => Some(Ordering::Equal),
        (Val::Bool(x), Val::Bool(y)) => Some(x.cmp(y)),
        // 布尔在 Python 里就是整数（`真 == 1`），排序也要按数值
        (Val::Bool(x), Val::Int(y)) | (Val::Int(y), Val::Bool(x)) => {
            Some((*x as i128).cmp(y))
        }
        (Val::Int(x), Val::Int(y)) => Some(x.cmp(y)),
        (Val::Int(x), Val::Float(y)) => (*x as f64).partial_cmp(y),
        (Val::Float(x), Val::Int(y)) => x.partial_cmp(&(*y as f64)),
        (Val::Float(x), Val::Float(y)) => x.partial_cmp(y),
        (Val::Str(x), Val::Str(y)) => Some(x.cmp(y)),
        // 精确小数参加排序/最值：先转十进制再比（对齐 Python 侧
        // `JishiDecimal.__lt__` 等，让 `最大`/`列表.排序` 能用）
        (Val::Dec(x), Val::Dec(y)) => Some(x.cmp(y)),
        (Val::Dec(x), b) => to_dec(b).ok().map(|d| x.cmp(&d)),
        (a, Val::Dec(y)) => to_dec(a).ok().map(|d| d.cmp(y)),
        (Val::Date(x), Val::Date(y)) => Some(x.to_display().cmp(&y.to_display())),
        (Val::List(x), Val::List(y)) => {
            let (x, y) = (x.borrow(), y.borrow());
            for (p, q) in x.iter().zip(y.iter()) {
                match try_cmp_vals(p, q)? {
                    Ordering::Equal => continue,
                    ord => return Some(ord),
                }
            }
            Some(x.len().cmp(&y.len()))
        }
        // 其它组合：能判相等就给 Equal，否则「不可比」（Python 此时会抛
        // TypeError，调用方按原来的方式报错）
        _ => {
            if a == b {
                Some(Ordering::Equal)
            } else {
                None
            }
        }
    }
}

impl PartialOrd for Val {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        try_cmp_vals(self, other)
    }
}

// ---------------------------------------------------------------------------
// 加载 JSON 字节码
// ---------------------------------------------------------------------------

pub fn load_module(json_text: &str) -> Result<VM, String> {
    let body = json::parse(json_text)?;
    if body.get("format").and_then(|v| v.as_str()) != Some("jishi-bytecode") {
        return Err("不是基石字节码格式".to_string());
    }
    // 字节码格式版本：**四处同步**（`jishi/serialize.py` / `core/src/serialize.rs`
    // / `node/index.js` / 这里），改一处就要改四处。
    // 3 = R6.6 起（Code 增加 local_hints）；2 = M25 起（Code 增加 param_kind）。
    // 版本不同宁可直接拒绝，也不要按旧格式错误解析（那会静默算错）。
    if body.get("version").and_then(|v| v.as_i64()) != Some(crate::BYTECODE_VERSION) {
        return Err(format!(
            "字节码版本不兼容（本引擎需要 {}）", crate::BYTECODE_VERSION));
    }
    let payload = body.get("payload").ok_or("缺少 payload")?;

    // 常量池
    let mut consts = Vec::new();
    for c in payload.get("consts").and_then(|v| v.as_array()).ok_or("常量池格式错误")? {
        let t = c.get("t").and_then(|v| v.as_str()).unwrap_or("");
        let v = match t {
            "none" => Val::None_,
            "true" => Val::Bool(true),
            "false" => Val::Bool(false),
            "int" => {
                // 两种编码都要认：小整数是 JSON 数字，超出宿主安全范围的
                // 大整数是**十进制字符串**（M32，见 jishi/serialize.py）。
                // 解不出来就报错——以前 `unwrap_or(0)` 会把大整数静默变成 0。
                let raw = c.get("v").ok_or("整数常量缺 v 字段")?;
                let parsed = match raw {
                    Json::Str(t) => t.parse::<i128>().ok(),
                    other => other.as_i128(),
                };
                Val::Int(parsed.ok_or_else(|| {
                    format!("整数常量超出范围（Rust 宿主支持到 i128）：{raw:?}")
                })?)
            }
            "float" => Val::Float(c.get("v").and_then(|x| x.as_f64()).unwrap_or(0.0)),
            "str" => Val::Str(c.get("v").and_then(|x| x.as_str()).unwrap_or("").to_string()),
            _ => return Err(format!("未知常量类型 {t}")),
        };
        consts.push(v);
    }

    let names: Vec<String> = payload.get("names").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default();
    let kw_names: Vec<Vec<String>> = payload.get("kw_names").and_then(|v| v.as_array()).map(|a| a.iter().map(|v| v.as_array().map(|x| x.iter().filter_map(|y| y.as_str().map(String::from)).collect()).unwrap_or_default()).collect()).unwrap_or_default();

    // 代码对象
    let mut codes = Vec::new();
    for cd in payload.get("codes").and_then(|v| v.as_array()).ok_or("codes 缺失")? {
        let flat: Vec<i64> = cd.get("instrs").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|v| v.as_i64()).collect()).unwrap_or_default();
        let mut instrs = Vec::new();
        for chunk in flat.chunks(6) {
            if chunk.len() == 6 {
                instrs.push(Instr { op: chunk[0], a: chunk[1], b: chunk[2], c: chunk[3], line: chunk[4], col: chunk[5] });
            }
        }
        codes.push(Code {
            name: cd.get("name").and_then(|v| v.as_str()).unwrap_or("<模块>").to_string(),
            params: cd.get("params").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default(),
            param_local: cd.get("param_local").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|v| v.as_i64()).collect()).unwrap_or_default(),
            param_kind: cd.get("param_kind").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|v| v.as_i64()).collect()).unwrap_or_default(),
            param_cell: cd.get("param_cell").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|v| v.as_i64()).collect()).unwrap_or_default(),
            instrs,
            nlocals: cd.get("nlocals").and_then(|v| v.as_i64()).unwrap_or(0) as usize,
            local_names: cd.get("local_names").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default(),
            cellvars: cd.get("cellvars").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default(),
            freevars: cd.get("freevars").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default(),
            // v3：`local_hints` 是 **JSON 对象**（键是槽位号的字符串形式）
            local_hints: cd.get("local_hints").and_then(|v| v.as_obj())
                .map(|o| o.iter().filter_map(|(k, v)| {
                    Some((k.parse::<i64>().ok()?, v.as_str()?.to_string()))
                }).collect())
                .unwrap_or_default(),
        });
    }

    let main = payload.get("main").and_then(|v| v.as_i64()).unwrap_or(0) as usize;
    let filename = payload.get("filename").and_then(|v| v.as_str())
        .unwrap_or("<输入>").to_string();
    Ok(VM::new(Module { consts, names, kw_names, codes, main, filename }))
}

pub fn get_output(vm: &VM) -> &str {
    &vm.output
}

/// 源文件名（R6.3）—— 报错渲染要打 `  ┌─ 文件:行:列`。
pub fn module_filename(vm: &VM) -> &str {
    &vm.module.filename
}

/// 源文件第 `line` 行的内容（R6.3，1 起）。读不到就给 `None`。
pub fn source_line_of(vm: &VM, line: i64) -> Option<String> {
    vm.module.source_line(line)
}

/// 把错误渲染成**与 Python 侧 `JishiError.render()` 同形**的多行文本（R6.3）。
///
/// ⚠️ 为什么要一模一样（而不是「大致像」）：报错是**用户可见的行为**，
/// 四个执行器里三个（树遍历 / Python VM / C VM）共用 Python 的 `render()`，
/// 只有宿主是自己拼的。宿主是 R7/R8 之后**唯一的形态**，所以它必须自己长出
/// 完整的那一份 —— 否则「单二进制的报错」会比今天的体验**退步**。
///
/// 与 Python 侧的逐项对齐：
/// * 标题行：有空消息才带括号（`错误 E2001：不能除以零`）；
/// * `  ┌─ 文件:行:列` + `  │` + `行号 │ 源码` + `    │   ^` + `  │`；
/// * 调用链 `name ← 第 N 行`（最多 20 帧）；
/// * 提示语 `  提示：…`。
/// ⚠️ **`^` 的宽度固定 1**：Python 侧有时带 `underline`（多字符区间），
/// 那是**词法/语法错**才有的（解析器手里有 token 的跨度）；宿主收到的只有
/// `line/col`。运行期错误在 Python 侧走的也正是 `underline = None` 那条路
/// （`start = end = col`），所以这一处**不差分毫**。
pub fn render_error(vm: &VM, e: &JishiError) -> String {
    let mut out = String::new();
    let title = error_title(&e.type_name);
    match &e.code {
        Some(code) => {
            out.push_str(&format!("错误 {code}：{title}"));
            if !e.message.is_empty() && e.message != title {
                out.push_str(&format!("（{}）", e.message));
            }
        }
        None => out.push_str(&format!("错误（{}）：{}", e.type_name, e.message)),
    }
    if let Some(line) = e.line {
        let col = e.col.unwrap_or(1);
        out.push_str(&format!("\n  ┌─ {}:{}:{}", vm.module.filename, line, col));
        out.push_str("\n  │");
        if let Some(src) = vm.module.source_line(line) {
            let src = src.trim_end_matches(['\n', '\r']);
            out.push_str(&format!("\n{line:>3} │ {src}"));
            let pad = " ".repeat((col.max(1) - 1) as usize);
            out.push_str(&format!("\n    │ {pad}^"));
        }
        out.push_str("\n  │");
    }
    if !e.trace.is_empty() {
        out.push_str("\n调用链（从外到内）：");
        for fr in e.trace.iter().rev().take(20).rev() {
            let where_ = match fr.line {
                Some(l) => format!("第 {l} 行"),
                None => "未知位置".to_string(),
            };
            out.push_str(&format!("\n  {} ← {}", fr.func, where_));
        }
    }
    if let Some(hint) = &e.hint {
        out.push_str(&format!("\n  提示：{hint}"));
    }
    out.push('\n');
    out
}
