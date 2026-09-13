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
mod zip;
use json::Json;

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
/// 基石字典（共享可变，保持插入顺序）
pub(crate) type JDict = Rc<RefCell<Vec<(Val, Val)>>>;
/// 基石集合（共享可变，**按插入顺序**去重——对齐 Python 侧 JishiSet：
/// 刻意不用哈希集合，迭代顺序依赖哈希种子会让同一程序两次运行输出不同）
pub(crate) type JSet = Rc<RefCell<Vec<Val>>>;
/// 基石实例（共享可变）。必须是共享的：`自身.字段 = 值` 走 SET_ATTR，
/// 拿到的是 `Val` 的一份拷贝——不共享的话就等于改副本，**不报错、静默失效**。
type JInst = Rc<RefCell<Instance>>;

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
    /// 循环信号（`中断`/`继续`）在「穿过 finally」时的临时载体。
    ///
    /// 与 Python 侧把 `RunBreak`/`RunContinue` 当异常对象在块栈里传递同一手法：
    /// 循环信号要先经过 finally 代码，再到外层循环，中间得能放在值栈上。
    /// 1 = 中断，2 = 继续。
    LoopSignal(i64),
    BoundMethod(Box<BoundMethod>),
    Cell(Box<Cell>),
}

/// 造一个列表值（把裸 Vec 包成共享容器）。
pub(crate) fn list_new(items: Vec<Val>) -> Val {
    Val::List(Rc::new(RefCell::new(items)))
}

/// 造一个字典值。
pub(crate) fn dict_new(pairs: Vec<(Val, Val)>) -> Val {
    Val::Dict(Rc::new(RefCell::new(pairs)))
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
    let mut out: Vec<Val> = Vec::new();
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
pub(crate) fn set_push(items: &mut Vec<Val>, v: Val) -> Result<(), JishiError> {
    match &v {
        Val::List(_) | Val::Dict(_) | Val::Set(_) | Val::Dec(_) => {
            return Err(err("类型错误",
                format!("「{}」不能放进集合，集合的元素要是不可变的（数字、文本…）",
                        display(&v))))
        }
        Val::Func(_) | Val::Class(_) | Val::Builtin(_, _)
        | Val::BoundMethod(_) | Val::Cell(_) => {
            return Err(err("类型错误",
                format!("「{}」不能放进集合（Rust 宿主没有可用的身份）", display(&v))))
        }
        _ => {}
    }
    if !items.iter().any(|x| *x == v) {
        items.push(v);
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
    cells: Vec<Cell>,
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
    /// 身份序号（M32）：见 `NEXT_OBJ_ID`
    id: usize,
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
                return Ok(Some(strip_cr(decode_utf8(&line)?)));
            }
            if self.fill()? == 0 {
                if self.rpos < self.rbuf.len() {
                    let rest = self.rbuf[self.rpos..].to_vec();
                    self.rpos = self.rbuf.len();
                    return Ok(Some(strip_cr(decode_utf8(&rest)?)));
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
        decode_utf8(&out)
    }

    fn read_n(&mut self, n: i64) -> Result<String, JishiError> {
        if n < 0 { return self.read_all(); }
        let mut out: Vec<u8> = Vec::new();
        while (out.len() as i64) < n {
            let avail = self.rbuf.len() - self.rpos;
            if avail > 0 {
                let take = std::cmp::min(avail, (n - out.len() as i64) as usize);
                out.extend_from_slice(&self.rbuf[self.rpos..self.rpos + take]);
                self.rpos += take;
            } else if self.fill()? == 0 {
                break;
            }
        }
        decode_utf8(&out)
    }

    fn write_text(&mut self, s: &str) -> Result<(), JishiError> {
        std::io::Write::write_all(&mut self.file, s.as_bytes())
            .map_err(|e| io_fail(&e, &self.path))
    }

    fn flush(&mut self) -> Result<(), JishiError> {
        std::io::Write::flush(&mut self.file).map_err(|e| io_fail(&e, &self.path))
    }

    /// 当前位置（把「已预读但还没消费」的部分减掉）。
    fn tell(&mut self) -> Result<i64, JishiError> {
        let pos = std::io::Seek::stream_position(&mut self.file)
            .map_err(|e| io_fail(&e, &self.path))?;
        Ok(pos as i64 - (self.rbuf.len() - self.rpos) as i64)
    }

    fn seek(&mut self, pos: i64) -> Result<(), JishiError> {
        // 定位会作废预读缓冲，否则读出来的东西对不上位置
        self.rbuf.clear();
        self.rpos = 0;
        std::io::Seek::seek(&mut self.file, std::io::SeekFrom::Start(pos.max(0) as u64))
            .map_err(|e| io_fail(&e, &self.path))?;
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
    Builtin { name: String, receiver: Box<Val> },
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
    cellvars: Vec<String>,
    freevars: Vec<String>,
}

#[derive(Clone)]
struct Module {
    consts: Vec<Val>,
    names: Vec<String>,
    kw_names: Vec<Vec<String>>,
    codes: Vec<Code>,
    main: usize,
}

// ---------------------------------------------------------------------------
// 异常
// ---------------------------------------------------------------------------

#[derive(Debug)]
pub struct JishiError {
    pub type_name: String,
    pub message: String,
}

type JsResult<T> = Result<T, JishiError>;

pub(crate) fn err(t: &str, msg: impl Into<String>) -> JishiError {
    JishiError { type_name: t.to_string(), message: msg.into() }
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
    locals: Vec<Val>,
    cells: Vec<Cell>,
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
    globals: HashMap<String, Val>,
    stack: Vec<Val>,
    frames: Vec<Frame>,
    last_value: Val,
    output: String,
}

impl VM {
    pub fn new(module: Module) -> Self {
        VM {
            module,
            globals: new_builtins(),
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
            locals: vec![Val::None_; nlocals],
            cells: vec![Cell { value: Val::None_, set: false }; ncells],
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
                Ok(Flow::Error(e)) => {
                    // 异常分发
                    if !self.dispatch_exception(&e, stop_at)? {
                        return Err(e);
                    }
                }
                Ok(Flow::Signal(s)) => {
                    if !self.dispatch_signal(s, stop_at)? {
                        return Err(err("运行期错误", "中断/继续没有对应的循环"));
                    }
                }
                Err(e) => {
                    // 出错（除零、类型不符、`抛出`…）也要走异常分发去找捕获/最终。
                    // 以前这里直接 `return Err(e)`，于是 try/捕获/最终 对「内置错误»
                    // 完全失效——只有 Flow::Error 才分发，而那个变体根本没人构造。
                    if !self.dispatch_exception(&e, stop_at)? {
                        return Err(e);
                    }
                }
            }
        }
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
            let ins = &instrs[ip];
            ip += 1;

            match ins.op {
                op::LOAD_CONST => {
                    let c = self.module.consts[ins.a as usize].clone();
                    self.stack.push(c);
                }
                op::LOAD_GLOBAL => {
                    let name = &self.module.names[ins.a as usize];
                    match self.globals.get(name) {
                        Some(v) => self.stack.push(v.clone()),
                        None => return Err(err("异常", format!("找不到名字「{name}」"))),
                    }
                }
                op::STORE_GLOBAL => {
                    let name = self.module.names[ins.a as usize].clone();
                    let v = self.stack.pop().unwrap_or(Val::None_);
                    self.globals.insert(name, v);
                }
                op::LOAD_FAST => {
                    let v = self.frames[fi].locals[ins.a as usize].clone();
                    self.stack.push(v);
                }
                op::STORE_FAST => {
                    let v = self.stack.pop().unwrap_or(Val::None_);
                    self.frames[fi].locals[ins.a as usize] = v;
                }
                op::LOAD_DEREF => {
                    let v = self.frames[fi].cells[ins.a as usize].value.clone();
                    self.stack.push(v);
                }
                op::STORE_DEREF => {
                    let v = self.stack.pop().unwrap_or(Val::None_);
                    self.frames[fi].cells[ins.a as usize] = Cell { value: v, set: true };
                }
                op::LOAD_CELL => {
                    let cell = self.frames[fi].cells[ins.a as usize].clone();
                    self.stack.push(Val::Cell(Box::new(cell)));
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
                                                 &kwvals, fn_base)?;
                            return Ok(Flow::Call);
                        }
                        Val::Builtin(name, bfn) => {
                            let out = bfn(&args, self)?;
                            self.stack.push(out);
                        }
                        Val::Class(c) => {
                            let inst = self.make_instance(c, &args)?;
                            self.stack.push(Val::Instance(inst));
                        }
                        Val::BoundMethod(bm) => {
                            match *bm {
                                BoundMethod::User { func, instance, .. } => {
                                    // 用户方法：把实例放进第一个形参（自身），再压新帧。
                                    // 与「函数」调用共用一条压帧路径——方法不过是个
                                    // 首参被预先绑定的函数。
                                    let mut all = Vec::with_capacity(args.len() + 1);
                                    all.push(Val::Instance(instance));
                                    all.extend(args.into_iter());
                                    self.push_func_frame(Some((fi, ip)), func, all,
                                                         &[], &[], fn_base)?;
                                    return Ok(Flow::Call);
                                }
                                builtin => {
                                    let out = self.call_bound(builtin, &args)?;
                                    self.stack.push(out);
                                }
                            }
                        }
                        Val::ExcType(t) => {
                            let msg = args.first().map(display).unwrap_or_default();
                            return Err(err(&t, msg));
                        }
                        other => return Err(err("运行期错误", format!("「{}」不能调用", display(&other)))),
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
                            Val::Cell(b) => (*b).clone(),
                            _ => Cell { value: Val::None_, set: false },
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
                    let out = get_attr(obj, &name)?;
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
                    let bm = get_attr(obj, "追加")?;
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
                    if has_base {
                        // 基类方法合并（简化：直接展开基类）
                        if let Val::Class(base) = self.stack.pop().unwrap_or(Val::None_) {
                            for (k, v) in base.methods { methods.entry(k).or_insert(v); }
                            for (k, v) in base.class_vars.borrow().iter() {
                                class_vars.insert(k.clone(), v.clone());
                            }
                        }
                    }
                    self.stack.push(Val::Class(Class {
                        name,
                        methods,
                        class_vars: Rc::new(RefCell::new(class_vars)),
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
                // 把异常作为特殊值压栈（这里简化为放一个字符串标记 + 异常类型）
                // 实际 `捕获 X 为 e` 时 e.类型/e.消息 由 get_attr 处理，
                // 这里压一个 ExcVal
                self.stack.push(Val::ExcType(e.type_name.clone()));
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
    fn push_func_frame(&mut self, set_ip: Option<(usize, usize)>, func: Func,
                       args: Vec<Val>, knames: &[String], kwvals: &[Val],
                       base: usize) -> Result<(), JishiError> {
        let sub_idx = func.code_idx;
        let (locals, values) = {
            let sub = &self.module.codes[sub_idx];
            bind_args(sub, &args, knames, kwvals, func.defaults.as_deref())?
        };
        let mut cells = {
            let sub = &self.module.codes[sub_idx];
            sub.cellvars.iter()
                .map(|_| Cell { value: Val::None_, set: false })
                .collect::<Vec<_>>()
        };
        cells.extend(func.cells.clone());
        {
            let sub = &self.module.codes[sub_idx];
            store_cells(sub, &mut cells, &values);
        }
        let nf = Frame {
            code_idx: sub_idx,
            ip: 0,
            base,
            locals,
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
        self.push_func_frame(None, func, all, &[], &[], base)?;
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
        get_attr(obj, attr)
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
            BoundMethod::Builtin { name, receiver } => {
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
                let i = as_int(&args[0])? as usize;
                let mut l = l.borrow_mut();
                let at = i.min(l.len());      // 先算好下标，再插入（避免同时可变+不可变借用）
                l.insert(at, args[1].clone());
                Ok(Val::None_)
            }
            (Val::List(l), "弹出") => {
                need_args(&name, args, 0, 1)?;
                let mut l = l.borrow_mut();
                if l.is_empty() { return Err(err("值错误", "列表是空的")); }
                let v = if args.is_empty() { l.pop() } else { let i = as_int(&args[0])? as usize; if i < l.len() { Some(l.remove(i)) } else { None } };
                v.ok_or_else(|| err("索引错误", "越界"))
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
                else { Err(err("值错误", "列表里没有这个元素")) }
            }
            // -- 字典方法 --
            (Val::Dict(d), "获取") => {
                need_args(&name, args, 1, 2)?;
                let dflt = if args.len() == 2 { args[1].clone() } else { Val::None_ };
                Ok(d.borrow().iter().find(|(k, _)| *k == args[0]).map(|(_, v)| v.clone()).unwrap_or(dflt))
            }
            (Val::Dict(d), "键") => { need_args(&name, args, 0, 0)?; Ok(list_new(d.borrow().iter().map(|(k, _)| k.clone()).collect())) }
            (Val::Dict(d), "值") => { need_args(&name, args, 0, 0)?; Ok(list_new(d.borrow().iter().map(|(_, v)| v.clone()).collect())) }
            (Val::Dict(d), "包含") => { need_args(&name, args, 1, 1)?; Ok(Val::Bool(d.borrow().iter().any(|(k, _)| *k == args[0]))) }
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
            (Val::Str(s), "替换") => { need_args(&name, args, 2, 2)?; let a = display(&args[0]); let b = display(&args[1]); Ok(Val::Str(s.replace(&a, &b))) }
            (Val::Str(s), "包含") => { need_args(&name, args, 1, 1)?; Ok(Val::Bool(s.contains(&display(&args[0])))) }
            (Val::Str(s), "开头是") => { need_args(&name, args, 1, 1)?; Ok(Val::Bool(s.starts_with(&display(&args[0])))) }
            (Val::Str(s), "结尾是") => { need_args(&name, args, 1, 1)?; Ok(Val::Bool(s.ends_with(&display(&args[0])))) }
            (Val::Str(s), "转整数") => { need_args(&name, args, 0, 0)?; s.parse::<i128>().map(Val::Int).map_err(|_| err("值错误", "不能转成整数")) }
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
            (Val::Set(s), "添加") => { need_args(&name, args, 1, 1)?; set_push(&mut s.borrow_mut(), args[0].clone())?; Ok(Val::None_) }
            (Val::Set(s), "移除") => {
                need_args(&name, args, 1, 1)?;
                let mut s = s.borrow_mut();
                match s.iter().position(|x| *x == args[0]) {
                    Some(i) => { s.remove(i); Ok(Val::None_) }
                    None => Err(err("值错误",
                        format!("集合里没有「{}」，没法移除", display(&args[0])))),
                }
            }
            (Val::Set(s), "丢弃") => {
                need_args(&name, args, 1, 1)?;
                let mut s = s.borrow_mut();
                if let Some(i) = s.iter().position(|x| *x == args[0]) { s.remove(i); }
                Ok(Val::None_)
            }
            (Val::Set(s), "包含") => { need_args(&name, args, 1, 1)?; Ok(Val::Bool(s.borrow().iter().any(|x| *x == args[0]))) }
            (Val::Set(s), "并集") => { need_args(&name, args, 1, 1)?; let other = as_iterable(&args[0], "「并集」的另一个集合")?; let mut v = s.borrow().clone(); v.extend(other); set_new(v) }
            (Val::Set(s), "交集") => { need_args(&name, args, 1, 1)?; let other = as_iterable(&args[0], "「交集」的另一个集合")?; let v: Vec<Val> = s.borrow().iter().filter(|x| other.iter().any(|y| y == *x)).cloned().collect(); set_new(v) }
            (Val::Set(s), "差集") => { need_args(&name, args, 1, 1)?; let other = as_iterable(&args[0], "「差集」的另一个集合")?; let v: Vec<Val> = s.borrow().iter().filter(|x| !other.iter().any(|y| y == *x)).cloned().collect(); set_new(v) }
            (Val::Set(s), "对称差") => {
                need_args(&name, args, 1, 1)?;
                let other = as_iterable(&args[0], "「对称差」的另一个集合")?;
                let a: Vec<Val> = s.borrow().iter().filter(|x| !other.iter().any(|y| y == *x)).cloned().collect();
                let b: Vec<Val> = other.iter().filter(|x| !s.borrow().iter().any(|y| y == *x)).cloned().collect();
                let mut v = a; v.extend(b); set_new(v)
            }
            (Val::Set(s), "清空") => { need_args(&name, args, 0, 0)?; s.borrow_mut().clear(); Ok(Val::None_) }
            (Val::Set(s), "转列表") => { need_args(&name, args, 0, 0)?; Ok(list_new(s.borrow().clone())) }
            (Val::Set(s), "复制") => { need_args(&name, args, 0, 0)?; set_new(s.borrow().clone()) }
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
                let items = s.borrow().clone();
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
                "退出" | "关闭" => match get_attr(obj.clone(), name) {
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

fn truthy(v: Val) -> bool {
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
        3 => { let (a, b) = to_num_pair(l, r)?; if b == 0.0 { return Err(err("除零错误", "不能除以零")); } Float(a / b) }
        4 => { let (a, b) = to_num_pair(l, r)?; if b == 0.0 { return Err(err("除零错误", "不能除以零")); } Int((a / b).floor() as i128) }
        5 => { let (a, b) = to_num_pair(l, r)?; if b == 0.0 { return Err(err("除零错误", "不能除以零")); } Int(a as i128 % b as i128) }
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
        _ => Err(err("类型错误", "不支持的加法")),
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

pub(crate) fn to_num_pair(a: Val, b: Val) -> Result<(f64, f64), JishiError> {
    let x = match a { Val::Int(i) => i as f64, Val::Float(f) => f, _ => return Err(err("类型错误", "需要数字")) };
    let y = match b { Val::Int(i) => i as f64, Val::Float(f) => f, _ => return Err(err("类型错误", "需要数字")) };
    Ok((x, y))
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
            if zero { return Err(err("除零错误", "不能除以零")); }
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
            let (a, b) = to_num_pair(l, r)?;
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
        Val::Set(s) => Ok(s.borrow().iter().any(|x| x == item)),
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
                x.len() == y.len() && x.iter().all(|v| y.iter().any(|w| w == v))
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
        match &args[0] {
            Val::Int(i) => Ok(Val::Int(*i)),
            Val::Float(f) => Ok(Val::Int(*f as i128)),
            Val::Str(s) => s.parse::<i128>().map(Val::Int).map_err(|_| err("值错误", format!("「{s}」不能转成整数"))),
            _ => Err(err("值错误", "不能转成整数")),
        }
    }));
    m.insert("小数".to_string(), Val::Builtin("小数", |args, _vm| {
        match &args[0] {
            Val::Int(i) => Ok(Val::Float(*i as f64)),
            Val::Float(f) => Ok(Val::Float(*f)),
            Val::Str(s) => s.parse::<f64>().map(Val::Float).map_err(|_| err("值错误", format!("「{s}」不能转成小数"))),
            _ => Err(err("值错误", "不能转成小数")),
        }
    }));
    m.insert("文本".to_string(), Val::Builtin("文本", |args, vm| {
        // 同样走 format_value：`文本(空)` 给「空」，`文本(对象)` 用「文本()」方法
        let mut out = String::new();
        for a in args {
            out.push_str(&vm.format_value(a, true)?);
        }
        Ok(Val::Str(out))
    }));
    m.insert("长度".to_string(), Val::Builtin("长度", |args, vm| {
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
        let (a, b, step) = match args.len() {
            1 => (0, as_int(&args[0])?, 1i128),
            2 => (as_int(&args[0])?, as_int(&args[1])?, 1i128),
            3 => (as_int(&args[0])?, as_int(&args[1])?, as_int(&args[2])?),
            _ => return Err(err("类型错误", "范围需要 1 到 3 个参数")),
        };
        let mut out = Vec::new();
        if step > 0 { let mut i = a; while i < b { out.push(Val::Int(i)); i += step; } }
        else if step < 0 { let mut i = a; while i > b { out.push(Val::Int(i)); i += step; } }
        Ok(list_new(out))
    }));
    m.insert("最大".to_string(), Val::Builtin("最大", b_max));
    m.insert("最小".to_string(), Val::Builtin("最小", b_min));
    m.insert("总和".to_string(), Val::Builtin("总和", b_sum));
    m.insert("类型".to_string(), Val::Builtin("类型", |args, _vm| {
        Ok(Val::Str(type_label(&args[0])))
    }));
    // 类型标注的参数校验（M24.3）：编译器把校验发射成函数自己的前导字节码，
    // 宿主实现这个内建就自动获得这项能力（M32 补齐，JS 侧是 M30 补的）。
    m.insert("检查实参".to_string(), Val::Builtin("检查实参", |args, _vm| {
        check_args(&args[0], &args[1], &args[2])
    }));
    m.insert("反转".to_string(), Val::Builtin("反转", |args, _vm| {
        match &args[0] {
            Val::Str(s) => Ok(Val::Str(s.chars().rev().collect())),
            Val::List(l) => { let mut x = l.borrow().clone(); x.reverse(); Ok(list_new(x)) }
            _ => Err(err("类型错误", "「反转」需要列表或文本")),
        }
    }));
    // -- 精确小数（M29）--
    m.insert("精确".to_string(), Val::Builtin("精确", |args, _vm| {
        if args.len() != 1 {
            return Err(err("类型错误", "「精确」需要 1 个参数"));
        }
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
        if args.is_empty() || args.len() > 2 {
            return Err(err("类型错误", "「打开」需要 1 到 2 个参数"));
        }
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
    m.insert("进入上下文".to_string(), Val::Builtin("进入上下文", |args, vm| {
        if args.len() != 1 {
            return Err(err("类型错误", "进入上下文 需要 1 个参数"));
        }
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
        if args.len() != 1 {
            return Err(err("类型错误", "退出上下文 需要 1 个参数"));
        }
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
    for t in ["异常", "运行期错误", "类型错误", "值错误", "索引错误", "键错误", "除零错误", "文件错误"] {
        m.insert(t.to_string(), Val::ExcType(t.to_string()));
    }
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
fn cmp_vals(a: &Val, b: &Val) -> std::cmp::Ordering {
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
        Val::Set(s) => Ok(Some(s.borrow().clone())),
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
        return seq.into_iter().max_by(cmp_vals)
            .ok_or_else(|| err("值错误", "空序列求不了最大值"));
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

fn get_attr(obj: Val, attr: &str) -> Result<Val, JishiError> {
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
        Val::Str(s) => str_method(attr, Val::Str(s)),
        Val::List(l) => list_method(attr, Val::List(l)),
        Val::Dict(d) => dict_method(attr, Val::Dict(d)),
        Val::Set(s) => set_method(attr, Val::Set(s)),
        Val::Dec(d) => dec_method(attr, d),
        Val::File(f) => file_method(attr, Val::File(f)),
        _ => Err(err("类型错误", format!("「{}」没有属性「{attr}」", display(&obj)))),
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
        (Val::List(l), Val::Int(i)) => l.borrow().get(i as usize).cloned().ok_or_else(|| err("索引错误", "越界")),
        (Val::Dict(d), k) => d.borrow().iter().find(|(key, _)| *key == k).map(|(_, v)| v.clone()).ok_or_else(|| err("键错误", "找不到这个键")),
        (Val::Str(s), Val::Int(i)) => s.chars().nth(i as usize).map(|c| Val::Str(c.to_string())).ok_or_else(|| err("索引错误", "越界")),
        _ => Err(err("类型错误", "不能取下标")),
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
            let i = i as usize;
            if i < l.len() { l[i] = val; Ok(()) } else { Err(err("索引错误", "越界")) }
        }
        (Val::Dict(d), k) => {
            let mut d = d.borrow_mut();
            if let Some(v) = d.iter_mut().find(|(key, _)| *key == k) { v.1 = val; } else { d.push((k, val)); }
            Ok(())
        }
        _ => Err(err("类型错误", "不能按下标赋值")),
    }
}


fn need_args(name: &str, args: &[Val], lo: usize, hi: usize) -> Result<(), JishiError> {
    if args.len() < lo || args.len() > hi {
        Err(err("类型错误", format!("方法「{name}」需要 {lo} 到 {hi} 个参数")))
    } else { Ok(()) }
}

// 简化：字符串/列表/字典方法直接返回 BoundMethod
fn str_method(name: &str, receiver: Val) -> Result<Val, JishiError> {
    let bm = BoundMethod::Builtin { name: name.to_string(), receiver: Box::new(receiver) };
    Ok(Val::BoundMethod(Box::new(bm)))
}
fn list_method(name: &str, receiver: Val) -> Result<Val, JishiError> {
    let bm = BoundMethod::Builtin { name: name.to_string(), receiver: Box::new(receiver) };
    Ok(Val::BoundMethod(Box::new(bm)))
}
fn dict_method(name: &str, receiver: Val) -> Result<Val, JishiError> {
    let bm = BoundMethod::Builtin { name: name.to_string(), receiver: Box::new(receiver) };
    Ok(Val::BoundMethod(Box::new(bm)))
}
fn set_method(name: &str, receiver: Val) -> Result<Val, JishiError> {
    // 只认自己有的方法名，其余交给 get_attr 报「没有属性」——
    // 直接把任何名字包成 BoundMethod 会让 `集合.不存在的方法()` 变成
    // 运行到调用时才报错，位置和措辞都差
    const NAMES: &[&str] = &["添加", "移除", "丢弃", "包含", "并集", "交集",
                             "差集", "对称差", "清空", "转列表", "复制"];
    if !NAMES.contains(&name) {
        return Err(err("类型错误", format!("集合没有方法「{name}」")));
    }
    let bm = BoundMethod::Builtin { name: name.to_string(), receiver: Box::new(receiver) };
    Ok(Val::BoundMethod(Box::new(bm)))
}
fn file_method(name: &str, receiver: Val) -> Result<Val, JishiError> {
    const NAMES: &[&str] = &["读", "读行", "读所有行", "写", "写行",
                             "关闭", "刷新", "位置", "定位"];
    if !NAMES.contains(&name) {
        return Err(err("类型错误", format!("文件没有方法「{name}」")));
    }
    let bm = BoundMethod::Builtin { name: name.to_string(), receiver: Box::new(receiver) };
    Ok(Val::BoundMethod(Box::new(bm)))
}
fn dec_method(name: &str, d: Rc<Dec>) -> Result<Val, JishiError> {
    const NAMES: &[&str] = &["舍入", "绝对值", "转文本"];
    if !NAMES.contains(&name) {
        return Err(err("类型错误", format!("精确小数没有方法「{name}」")));
    }
    let bm = BoundMethod::Builtin { name: name.to_string(), receiver: Box::new(Val::Dec(d)) };
    Ok(Val::BoundMethod(Box::new(bm)))
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
        Val::Set(s) => Ok(s.borrow().clone()),
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
        _ => return Err(err("类型错误", "解包赋值需要列表")),
    };
    if star < 0 {
        if seq.len() != n {
            return Err(err("值错误",
                format!("解包需要 {n} 个值，实际有 {} 个", seq.len())));
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
        Val::Str(s) => err("异常", s),
        other => err("异常", display(&other)),
    }
}

fn exception_matches(exc: &Val, cond: &Val) -> bool {
    match (exc, cond) {
        (Val::ExcType(t), Val::ExcType(c)) => {
            if c == "异常" { return true; }
            let hierarchy: Vec<(&str, &str)> = vec![("除零错误", "运行期错误"), ("类型错误", "运行期错误"), ("值错误", "运行期错误"), ("运行期错误", "异常")];
            let mut cur = t.as_str();
            loop {
                if cur == c { return true; }
                match hierarchy.iter().find(|(child, _)| *child == cur) {
                    Some((_, parent)) => cur = parent,
                    None => return false,
                }
            }
        }
        _ => false,
    }
}

// ---------------------------------------------------------------------------
// 迭代器
// ---------------------------------------------------------------------------

fn make_iterator(obj: Val) -> Result<Val, JishiError> {
    // 迭代器就是「一个会被逐个弹掉的列表」，FOR_ITER 每次 remove(0)。
    //
    // 关键：**必须做快照拷贝**。以前列表是值类型，`Val::List(l)` 天然就是
    // 独立副本，所以「遍历不消耗原列表」是白捡的；换成共享容器后如果直接把
    // `l` 递出去，`遍历 x 在 a` 会把 `a` 逐元素搬空——那是错的。
    match obj {
        Val::List(l) => Ok(list_new(l.borrow().clone())),
        Val::Set(s) => Ok(list_new(s.borrow().clone())),
        Val::Str(s) => Ok(list_new(s.chars().map(|c| Val::Str(c.to_string())).collect())),
        Val::Dict(d) => Ok(list_new(d.borrow().iter().map(|(k, _)| k.clone()).collect())),
        // 文件是**惰性**迭代：真句柄不能快照（那等于整读进内存），
        // 所以迭代器就是文件自己，每次 next 读一行。
        Val::File(_) => Ok(obj),
        other => Err(err("类型错误",
            format!("「{}」不能遍历，遍历需要列表、文本、集合、字典或文件",
                    display(&other)))),
    }
}

/// 取下一个元素：列表迭代器弹头，文件迭代器读一行。
fn iter_next(it: &mut Val) -> Result<Option<Val>, JishiError> {
    match it {
        Val::List(l) => {
            let mut l = l.borrow_mut();
            if l.is_empty() { Ok(None) } else { Ok(Some(l.remove(0))) }
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

fn bind_args(code: &Code, args: &[Val], knames: &[String], kwvals: &[Val], defaults: Option<&[Val]>) -> Result<(Vec<Val>, Vec<Val>), JishiError> {
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
            return Err(err("类型错误", format!("函数没有参数「{k}」")));
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
    let mut values = Vec::new();
    for i in 0..nparams {
        let v = bound[&i].clone();
        values.push(v.clone());
        let slot = code.param_local[i];
        if slot >= 0 { locals[slot as usize] = v; }
    }
    Ok((locals, values))
}

fn store_cells(code: &Code, cells: &mut [Cell], values: &[Val]) {
    for (i, v) in values.iter().enumerate() {
        let ci = code.param_cell[i];
        if ci >= 0 { cells[ci as usize] = Cell { value: v.clone(), set: true }; }
    }
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
    // 字节码格式版本：2 = M25 起（Code 增加 param_kind，UNPACK 的 b 表示星号位置）。
    // 与 Python 侧 serialize.FORMAT_VERSION、Node 侧 index.js 的检查保持一致；
    // 版本不同宁可直接拒绝，也不要按旧格式错误解析（那会静默算错）。
    if body.get("version").and_then(|v| v.as_i64()) != Some(2) {
        return Err("字节码版本不兼容（本引擎需要 2）".to_string());
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
                instrs.push(Instr { op: chunk[0], a: chunk[1], b: chunk[2], c: chunk[3] });
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
            cellvars: cd.get("cellvars").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default(),
            freevars: cd.get("freevars").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default(),
        });
    }

    let main = payload.get("main").and_then(|v| v.as_i64()).unwrap_or(0) as usize;
    Ok(VM::new(Module { consts, names, kw_names, codes, main }))
}

pub fn get_output(vm: &VM) -> &str {
    &vm.output
}
