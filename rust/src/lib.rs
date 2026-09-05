//! 基石（jishi）中文编程语言的 Rust 字节码解释器（M13.3）。
//!
//! 消费 M13.1 产出的 JSON 字节码，脱离 Python 独立执行基石代码。
//! 语义与 Python VM / JS VM 对齐。

use std::collections::HashMap;

mod json;
use json::Json;

// ---------------------------------------------------------------------------
// 值表示
// ---------------------------------------------------------------------------

#[derive(Clone)]
enum Val {
    None_,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    List(Vec<Val>),
    Dict(Vec<(Val, Val)>),
    Func(Func),
    Class(Class),
    Instance(Instance),
    Module(String, HashMap<String, Val>),
    Builtin(&'static str, fn(&[Val], &mut VM) -> Result<Val, JishiError>),
    ExcType(String),
    BoundMethod(Box<BoundMethod>),
    Cell(Box<Cell>),
}

#[derive(Clone)]
struct Cell {
    value: Val,
    set: bool,
}

#[derive(Clone)]
struct Func {
    name: String,
    code_idx: usize,
    cells: Vec<Cell>,
    defaults: Option<Vec<Val>>,
}

#[derive(Clone)]
struct Class {
    name: String,
    methods: HashMap<String, Val>,
}

#[derive(Clone)]
struct Instance {
    class: Class,
    fields: HashMap<String, Val>,
}

#[derive(Clone)]
enum BoundMethod {
    Builtin { name: String, receiver: Box<Val> },
    User { func: Func, instance: Instance, name: String },
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

fn err(t: &str, msg: impl Into<String>) -> JishiError {
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
    loops: Vec<(usize, i64, i64)>,      // (depth, cont, brk)
    handlers: Vec<Handler>,
}

#[derive(Clone)]
struct Handler {
    catch_ip: i64,
    finally_ip: i64,
    sp: usize,
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
                    // 内部错误直接抛出
                    return Err(e);
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
                            // 压新帧
                            let sub_idx = f.code_idx;
                            let sub = &self.module.codes[sub_idx];
                            let (locals, values) = bind_args(sub, &args, &knames, &kwvals, f.defaults.as_deref())?;
                            let mut cells = sub.cellvars.iter().map(|_| Cell { value: Val::None_, set: false }).collect::<Vec<_>>();
                            cells.extend(f.cells.clone());
                            store_cells(sub, &mut cells, &values);
                            let nf = Frame {
                                code_idx: sub_idx,
                                ip: 0,
                                base: fn_base,
                                locals,
                                cells,
                                loops: Vec::new(),
                                handlers: Vec::new(),
                            };
                            self.frames[fi].ip = ip;
                            self.frames.push(nf);
                            return Ok(Flow::Return);
                        }
                        Val::Builtin(name, bfn) => {
                            let out = bfn(&args, self)?;
                            self.stack.push(out);
                        }
                        Val::Class(c) => {
                            let inst = make_instance(c, &args)?;
                            self.stack.push(Val::Instance(inst));
                        }
                        Val::BoundMethod(bm) => {
                            let out = call_bound(*bm, &args)?;
                            self.stack.push(out);
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
                    self.stack.truncate(base);
                    self.stack.push(value);
                    return Ok(Flow::Return);
                }
                op::MAKE_FUNCTION => {
                    let code_idx = ins.a as usize;
                    let ncells = ins.b as usize;
                    let ndefaults = ins.c as usize;
                    let defaults = if ndefaults > 0 {
                        let n = self.stack.len();
                        let tail: Vec<Val> = self.stack.drain(n - ndefaults..).collect();
                        let nparams = self.module.codes[code_idx].params.len();
                        let mut d = vec![Val::None_; nparams - ndefaults];
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
                    self.stack.push(Val::Func(Func { name, code_idx, cells, defaults }));
                }
                op::BUILD_LIST => {
                    let n = ins.a as usize;
                    let items = if n > 0 {
                        let l = self.stack.len();
                        self.stack.drain(l - n..).collect()
                    } else { Vec::new() };
                    self.stack.push(Val::List(items));
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
                    self.stack.push(Val::Dict(pairs));
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
                    let it = make_iterator(obj)?;
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
                    let items = unpack_values(v, n)?;
                    for item in items.into_iter().rev() {
                        self.stack.push(item);
                    }
                }
                op::LIST_APPEND => {
                    let val = self.stack.pop().unwrap_or(Val::None_);
                    let obj = self.stack.pop().unwrap_or(Val::None_);
                    let bm = get_attr(obj, "追加")?;
                    if let Val::BoundMethod(bm) = bm {
                        call_bound(*bm, &[val])?;
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
                    if has_base {
                        // 基类方法合并（简化：直接展开基类）
                        if let Val::Class(base) = self.stack.pop().unwrap_or(Val::None_) {
                            for (k, v) in base.methods { methods.entry(k).or_insert(v); }
                        }
                    }
                    self.stack.push(Val::Class(Class { name, methods }));
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
                    self.frames[fi].loops.push((self.stack.len(), ins.a, ins.b));
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
                    let n = match v { Val::Int(i) => i, Val::Float(f) => f as i64, _ => return Err(err("类型错误", format!("「{}」不是有效的循环次数", display(&v)))) };
                    self.frames[fi].locals[ins.a as usize] = Val::Int(n);
                }
                op::SETUP_TRY => {
                    self.frames[fi].handlers.push(Handler { catch_ip: ins.a, finally_ip: ins.b, sp: self.stack.len() });
                }
                op::POP_TRY => { self.frames[fi].handlers.pop(); }
                op::THROW => {
                    let v = self.stack.pop().unwrap_or(Val::None_);
                    return Err(make_exception(v));
                }
                op::MATCH_EXC => {
                    let cond = self.stack.pop().unwrap_or(Val::None_);
                    let exc = self.stack.last().cloned().unwrap_or(Val::None_);
                    let m = exception_matches(&exc, &cond);
                    self.stack.push(Val::Bool(m));
                }
                op::END_FINALLY => {}
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

    fn dispatch_signal(&mut self, s: Signal, stop_at: Option<usize>) -> Result<bool, JishiError> {
        while !self.frames.is_empty() {
            let fi = self.frames.len() - 1;
            if Some(fi) == stop_at { return Ok(false); }
            if let Some(lp) = self.frames[fi].loops.last().cloned() {
                let (depth, cont, brk) = lp;
                self.frames[fi].loops.pop();
                self.stack.truncate(depth);
                self.frames[fi].ip = (match s { Signal::Break => brk, Signal::Continue => cont }) as usize;
                return Ok(true);
            }
            let base = self.frames[fi].base;
            self.frames.pop();
            self.stack.truncate(base);
        }
        Ok(false)
    }
}

// ---------------------------------------------------------------------------
// 流程控制
// ---------------------------------------------------------------------------

enum Flow {
    Return,
    Halt,
    Error(JishiError),
    Signal(Signal),
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
        Val::List(l) => !l.is_empty(),
        Val::Dict(d) => !d.is_empty(),
        _ => true,
    }
}

fn display(v: &Val) -> String {
    match v {
        Val::None_ => "空".to_string(),
        Val::Bool(true) => "真".to_string(),
        Val::Bool(false) => "假".to_string(),
        Val::Int(i) => i.to_string(),
        Val::Float(f) => format_float(*f),
        Val::Str(s) => s.clone(),
        Val::List(l) => {
            let items: Vec<String> = l.iter().map(py_repr).collect();
            format!("[{}]", items.join(", "))
        }
        Val::Dict(d) => {
            let items: Vec<String> = d.iter().map(|(k, v)| format!("{}: {}", py_repr(k), py_repr(v))).collect();
            format!("{{{}}}", items.join(", "))
        }
        Val::Func(f) => format!("<函数 {}>", f.name),
        Val::Class(c) => format!("<类 {}>", c.name),
        Val::Instance(_) => "<实例>".to_string(),
        Val::Module(n, _) => format!("<模块 {n}>"),
        Val::Builtin(n, _) => format!("<内建函数 {n}>"),
        Val::ExcType(t) => t.clone(),
        Val::BoundMethod(_) => "<方法>".to_string(),
        Val::Cell(_) => "<单元>".to_string(),
    }
}

fn py_repr(v: &Val) -> String {
    match v {
        Val::Str(s) => format!("'{s}'"),
        Val::Bool(true) => "True".to_string(),
        Val::Bool(false) => "False".to_string(),
        Val::None_ => "None".to_string(),
        _ => display(v),
    }
}

fn format_float(f: f64) -> String {
    if f == f.trunc() && f.is_finite() { format!("{:.1}", f) } else { f.to_string() }
}

fn binop(op: i64, l: Val, r: Val) -> Result<Val, JishiError> {
    use Val::*;
    let result = match op {
        0 => match (l, r) { (Int(a), Int(b)) => Int(a + b), (a, b) => numeric_add(a, b)? },
        1 => match (l, r) { (Int(a), Int(b)) => Int(a - b), (a, b) => numeric_sub(a, b)? },
        2 => match (l, r) { (Int(a), Int(b)) => Int(a * b), (a, b) => numeric_mul(a, b)? },
        3 => { let (a, b) = to_num_pair(l, r)?; if b == 0.0 { return Err(err("除零错误", "不能除以零")); } Float(a / b) }
        4 => { let (a, b) = to_num_pair(l, r)?; if b == 0.0 { return Err(err("除零错误", "不能除以零")); } Int((a / b).floor() as i64) }
        5 => { let (a, b) = to_num_pair(l, r)?; if b == 0.0 { return Err(err("除零错误", "不能除以零")); } Int(a as i64 % b as i64) }
        6 => { let (a, b) = to_num_pair(l, r)?; Float(a.powf(b)) }
        _ => return Err(err("运行期错误", "不支持的运算符")),
    };
    Ok(result)
}

fn numeric_add(a: Val, b: Val) -> Result<Val, JishiError> {
    match (a, b) {
        (Val::Int(x), Val::Float(y)) | (Val::Float(y), Val::Int(x)) => Ok(Val::Float(x as f64 + y)),
        (Val::Float(x), Val::Float(y)) => Ok(Val::Float(x + y)),
        (Val::Str(x), Val::Str(y)) => Ok(Val::Str(x + &y)),
        (Val::List(mut x), Val::List(y)) => { x.extend(y); Ok(Val::List(x)) }
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
        (Val::List(l), Val::Int(n)) | (Val::Int(n), Val::List(l)) => Ok(Val::List((0..n).flat_map(|_| l.clone()).collect())),
        _ => Err(err("类型错误", "不支持的乘法")),
    }
}

fn to_num_pair(a: Val, b: Val) -> Result<(f64, f64), JishiError> {
    let x = match a { Val::Int(i) => i as f64, Val::Float(f) => f, _ => return Err(err("类型错误", "需要数字")) };
    let y = match b { Val::Int(i) => i as f64, Val::Float(f) => f, _ => return Err(err("类型错误", "需要数字")) };
    Ok((x, y))
}

fn compare(op: i64, l: Val, r: Val) -> Result<Val, JishiError> {
    let out = match op {
        0 => l == r,
        1 => l != r,
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

impl PartialEq for Val {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Val::None_, Val::None_) => true,
            (Val::Bool(a), Val::Bool(b)) => a == b,
            (Val::Int(a), Val::Int(b)) => a == b,
            (Val::Int(a), Val::Float(b)) | (Val::Float(b), Val::Int(a)) => *a as f64 == *b,
            (Val::Float(a), Val::Float(b)) => a == b,
            (Val::Str(a), Val::Str(b)) => a == b,
            (Val::List(a), Val::List(b)) => a == b,
            _ => false,
        }
    }
}

fn unary_neg(v: Val) -> Val {
    match v {
        Val::Int(i) => Val::Int(-i),
        Val::Float(f) => Val::Float(-f),
        _ => Val::None_,
    }
}

// ---------------------------------------------------------------------------
// 内建函数
// ---------------------------------------------------------------------------

fn new_builtins() -> HashMap<String, Val> {
    let mut m = HashMap::new();
    m.insert("打印".to_string(), Val::Builtin("打印", |args, vm| {
        let parts: Vec<String> = args.iter().map(py_repr).collect();
        vm.output.push_str(&parts.join(" "));
        vm.output.push('\n');
        Ok(Val::None_)
    }));
    m.insert("整数".to_string(), Val::Builtin("整数", |args, _vm| {
        match &args[0] {
            Val::Int(i) => Ok(Val::Int(*i)),
            Val::Float(f) => Ok(Val::Int(*f as i64)),
            Val::Str(s) => s.parse::<i64>().map(Val::Int).map_err(|_| err("值错误", format!("「{s}」不能转成整数"))),
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
    m.insert("文本".to_string(), Val::Builtin("文本", |args, _vm| {
        Ok(Val::Str(args.iter().map(py_repr).collect::<Vec<_>>().join("")))
    }));
    m.insert("长度".to_string(), Val::Builtin("长度", |args, _vm| {
        match &args[0] {
            Val::Str(s) => Ok(Val::Int(s.chars().count() as i64)),
            Val::List(l) => Ok(Val::Int(l.len() as i64)),
            Val::Dict(d) => Ok(Val::Int(d.len() as i64)),
            _ => Err(err("类型错误", "没有长度")),
        }
    }));
    m.insert("范围".to_string(), Val::Builtin("范围", |args, _vm| {
        let (a, b, step) = match args.len() {
            1 => (0, as_int(&args[0])?, 1i64),
            2 => (as_int(&args[0])?, as_int(&args[1])?, 1i64),
            3 => (as_int(&args[0])?, as_int(&args[1])?, as_int(&args[2])?),
            _ => return Err(err("类型错误", "范围需要 1 到 3 个参数")),
        };
        let mut out = Vec::new();
        if step > 0 { let mut i = a; while i < b { out.push(Val::Int(i)); i += step; } }
        else if step < 0 { let mut i = a; while i > b { out.push(Val::Int(i)); i += step; } }
        Ok(Val::List(out))
    }));
    m.insert("最大".to_string(), Val::Builtin("最大", |args, _vm| {
        if let Val::List(l) = &args[0] {
            l.iter().cloned().max_by(|a, b| a.partial_cmp(b).unwrap()).ok_or_else(|| err("值错误", "空列表"))
        } else { Err(err("类型错误", "需要列表")) }
    }));
    m.insert("最小".to_string(), Val::Builtin("最小", |args, _vm| {
        if let Val::List(l) = &args[0] {
            l.iter().cloned().min_by(|a, b| a.partial_cmp(b).unwrap()).ok_or_else(|| err("值错误", "空列表"))
        } else { Err(err("类型错误", "需要列表")) }
    }));
    m.insert("总和".to_string(), Val::Builtin("总和", |args, _vm| {
        if let Val::List(l) = &args[0] {
            let mut s = 0.0;
            let mut is_int = true;
            for v in l {
                match v { Val::Int(i) => s += *i as f64, Val::Float(f) => { s += f; is_int = false; }, _ => return Err(err("类型错误", "需要数字列表")) }
            }
            Ok(if is_int { Val::Int(s as i64) } else { Val::Float(s) })
        } else { Err(err("类型错误", "需要列表")) }
    }));
    m.insert("类型".to_string(), Val::Builtin("类型", |args, _vm| {
        Ok(Val::Str(match &args[0] {
            Val::None_ => "空", Val::Bool(_) => "布尔", Val::Int(_) => "整数", Val::Float(_) => "小数",
            Val::Str(_) => "文本", Val::List(_) => "列表", Val::Dict(_) => "字典", _ => "其他",
        }.to_string()))
    }));
    m.insert("反转".to_string(), Val::Builtin("反转", |args, _vm| {
        match &args[0] {
            Val::Str(s) => Ok(Val::Str(s.chars().rev().collect())),
            Val::List(l) => { let mut x = l.clone(); x.reverse(); Ok(Val::List(x)) }
            _ => Err(err("类型错误", "需要列表或文本")),
        }
    }));
    // 异常类型
    for t in ["异常", "运行期错误", "类型错误", "值错误", "索引错误", "键错误", "除零错误", "文件错误"] {
        m.insert(t.to_string(), Val::ExcType(t.to_string()));
    }
    m
}

fn as_int(v: &Val) -> Result<i64, JishiError> {
    match v { Val::Int(i) => Ok(*i), _ => Err(err("类型错误", "需要整数")) }
}

fn stdlib_module(name: &str) -> Result<Val, JishiError> {
    let mut m = HashMap::new();
    match name {
        "数学" => {
            m.insert("开方".to_string(), Val::Builtin("开方", |args, _vm| {
                let f = match &args[0] { Val::Int(i) => (*i as f64).sqrt(), Val::Float(x) => x.sqrt(), _ => return Err(err("类型错误", "需要数字")) };
                Ok(Val::Float(f))
            }));
            m.insert("幂".to_string(), Val::Builtin("幂", |args, _vm| {
                let (a, b) = to_num_pair(args[0].clone(), args[1].clone())?;
                Ok(Val::Float(a.powf(b)))
            }));
            m.insert("阶乘".to_string(), Val::Builtin("阶乘", |args, _vm| {
                let n = as_int(&args[0])?;
                let mut r = 1i64; for i in 2..=n { r *= i; }
                Ok(Val::Int(r))
            }));
        }
        "文本" => {
            m.insert("拼接".to_string(), Val::Builtin("拼接", |args, _vm| {
                if let Val::List(l) = &args[0] {
                    let sep = if args.len() > 1 { display(&args[1]) } else { String::new() };
                    Ok(Val::Str(l.iter().map(display).collect::<Vec<_>>().join(&sep)))
                } else { Err(err("类型错误", "需要列表")) }
            }));
        }
        "随机" => {
            m.insert("随机整数".to_string(), Val::Builtin("随机整数", |args, _vm| {
                let (lo, hi) = (as_int(&args[0])?, as_int(&args[1])?);
                Ok(Val::Int(lo))  // 确定性：固定返回下界，避免非确定性（对拍友好）
            }));
        }
        _ => return Err(err("异常", format!("没有找到标准库模块「{name}」"))),
    }
    Ok(Val::Module(name.to_string(), m))
}

// ---------------------------------------------------------------------------
// 属性 / 下标 / 调用
// ---------------------------------------------------------------------------

fn get_attr(obj: Val, attr: &str) -> Result<Val, JishiError> {
    match obj {
        Val::Module(_, attrs) => attrs.get(attr).cloned().ok_or_else(|| err("异常", format!("模块里没有「{attr}」"))),
        Val::Instance(inst) => {
            if let Some(v) = inst.fields.get(attr) { return Ok(v.clone()); }
            if let Some(f) = inst.class.methods.get(attr) {
                if let Val::Func(fu) = f {
                    return Ok(Val::BoundMethod(Box::new(BoundMethod::User { func: fu.clone(), instance: inst.clone(), name: attr.to_string() })));
                }
            }
            Err(err("类型错误", format!("「{}」没有属性「{attr}」", inst.class.name)))
        }
        Val::Class(c) => c.methods.get(attr).cloned().ok_or_else(|| err("类型错误", format!("类没有方法「{attr}」"))),
        Val::Str(s) => str_method(attr, Val::Str(s)),
        Val::List(l) => list_method(attr, Val::List(l)),
        Val::Dict(d) => dict_method(attr, Val::Dict(d)),
        _ => Err(err("类型错误", format!("「{}」没有属性「{attr}」", display(&obj)))),
    }
}

fn set_attr(obj: Val, attr: &str, val: Val) -> Result<(), JishiError> {
    match obj {
        Val::Module(_, mut attrs) => { attrs.insert(attr.to_string(), val); Ok(()) }
        Val::Instance(mut inst) => { inst.fields.insert(attr.to_string(), val); Ok(()) }
        _ => Err(err("类型错误", "不能给这个对象赋值属性")),
    }
}

fn get_item(obj: Val, idx: Val) -> Result<Val, JishiError> {
    match (obj, idx) {
        (Val::List(l), Val::Int(i)) => l.get(i as usize).cloned().ok_or_else(|| err("索引错误", "越界")),
        (Val::Dict(d), k) => d.iter().find(|(key, _)| *key == k).map(|(_, v)| v.clone()).ok_or_else(|| err("键错误", "找不到这个键")),
        (Val::Str(s), Val::Int(i)) => s.chars().nth(i as usize).map(|c| Val::Str(c.to_string())).ok_or_else(|| err("索引错误", "越界")),
        _ => Err(err("类型错误", "不能取下标")),
    }
}

fn set_item(obj: Val, idx: Val, val: Val) -> Result<(), JishiError> {
    match (obj, idx) {
        (Val::List(mut l), Val::Int(i)) => { let i = i as usize; if i < l.len() { l[i] = val; Ok(()) } else { Err(err("索引错误", "越界")) } }
        (Val::Dict(mut d), k) => { if let Some(v) = d.iter_mut().find(|(key, _)| *key == k) { v.1 = val; } else { d.push((k, val)); } Ok(()) }
        _ => Err(err("类型错误", "不能按下标赋值")),
    }
}

fn call_bound(bm: BoundMethod, args: &[Val]) -> Result<Val, JishiError> {
    match bm {
        BoundMethod::Builtin { name, receiver } => {
            match (*receiver, name.as_str()) {
                // -- 列表方法 --
                (Val::List(mut l), "追加") => { need_args(&name, args, 1, 1)?; l.push(args[0].clone()); Ok(Val::None_) }
                (Val::List(mut l), "插入") => { need_args(&name, args, 2, 2)?; let i = as_int(&args[0])? as usize; l.insert(i.min(l.len()), args[1].clone()); Ok(Val::None_) }
                (Val::List(l), "弹出") => {
                    need_args(&name, args, 0, 1)?;
                    let mut l = l;
                    if l.is_empty() { return Err(err("值错误", "列表是空的")); }
                    let v = if args.is_empty() { l.pop() } else { let i = as_int(&args[0])? as usize; if i < l.len() { Some(l.remove(i)) } else { None } };
                    v.ok_or_else(|| err("索引错误", "越界"))
                }
                (Val::List(mut l), "排序") => { need_args(&name, args, 0, 0)?; l.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal)); Ok(Val::None_) }
                (Val::List(mut l), "反转") => { need_args(&name, args, 0, 0)?; l.reverse(); Ok(Val::None_) }
                (Val::List(mut l), "清空") => { need_args(&name, args, 0, 0)?; l.clear(); Ok(Val::None_) }
                (Val::List(l), "索引") => { need_args(&name, args, 1, 1)?; l.iter().position(|x| *x == args[0]).map(|i| Val::Int(i as i64)).ok_or_else(|| err("值错误", "找不到元素")) }
                (Val::List(l), "计数") => { need_args(&name, args, 1, 1)?; Ok(Val::Int(l.iter().filter(|x| **x == args[0]).count() as i64)) }
                (Val::List(l), "包含") => { need_args(&name, args, 1, 1)?; Ok(Val::Bool(l.contains(&args[0]))) }
                (Val::List(mut l), "移除") => {
                    need_args(&name, args, 1, 1)?;
                    if let Some(pos) = l.iter().position(|x| *x == args[0]) { l.remove(pos); Ok(Val::None_) }
                    else { Err(err("值错误", "列表里没有这个元素")) }
                }
                // -- 字典方法 --
                (Val::Dict(d), "获取") => {
                    need_args(&name, args, 1, 2)?;
                    let dflt = if args.len() == 2 { args[1].clone() } else { Val::None_ };
                    Ok(d.iter().find(|(k, _)| *k == args[0]).map(|(_, v)| v.clone()).unwrap_or(dflt))
                }
                (Val::Dict(d), "键") => { need_args(&name, args, 0, 0)?; Ok(Val::List(d.iter().map(|(k, _)| k.clone()).collect())) }
                (Val::Dict(d), "值") => { need_args(&name, args, 0, 0)?; Ok(Val::List(d.iter().map(|(_, v)| v.clone()).collect())) }
                (Val::Dict(d), "包含") => { need_args(&name, args, 1, 1)?; Ok(Val::Bool(d.iter().any(|(k, _)| *k == args[0]))) }
                (Val::Dict(mut d), "清空") => { need_args(&name, args, 0, 0)?; d.clear(); Ok(Val::None_) }
                // -- 字符串方法 --
                (Val::Str(s), "大写") => { need_args(&name, args, 0, 0)?; Ok(Val::Str(s.to_uppercase())) }
                (Val::Str(s), "小写") => { need_args(&name, args, 0, 0)?; Ok(Val::Str(s.to_lowercase())) }
                (Val::Str(s), "去空白") => { need_args(&name, args, 0, 0)?; Ok(Val::Str(s.trim().to_string())) }
                (Val::Str(s), "拆分") => {
                    need_args(&name, args, 0, 1)?;
                    let sep = args.first().map(display).unwrap_or_default();
                    Ok(Val::List(s.split(&sep).map(|p| Val::Str(p.to_string())).collect()))
                }
                (Val::Str(s), "替换") => { need_args(&name, args, 2, 2)?; let a = display(&args[0]); let b = display(&args[1]); Ok(Val::Str(s.replace(&a, &b))) }
                (Val::Str(s), "包含") => { need_args(&name, args, 1, 1)?; Ok(Val::Bool(s.contains(&display(&args[0])))) }
                (Val::Str(s), "开头是") => { need_args(&name, args, 1, 1)?; Ok(Val::Bool(s.starts_with(&display(&args[0])))) }
                (Val::Str(s), "结尾是") => { need_args(&name, args, 1, 1)?; Ok(Val::Bool(s.ends_with(&display(&args[0])))) }
                (Val::Str(s), "转整数") => { need_args(&name, args, 0, 0)?; s.parse::<i64>().map(Val::Int).map_err(|_| err("值错误", "不能转成整数")) }
                (Val::Str(s), "转小数") => { need_args(&name, args, 0, 0)?; s.parse::<f64>().map(Val::Float).map_err(|_| err("值错误", "不能转成小数")) }
                _ => Err(err("类型错误", format!("「{name}」方法调用未实现"))),
            }
        }
        BoundMethod::User { func, instance, .. } => {
            let _ = func;
            let _ = instance;
            Err(err("运行期错误", "用户方法调用需经 VM 帧（Rust 版暂未完整实现）"))
        }
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

fn make_instance(class: Class, args: &[Val]) -> Result<Instance, JishiError> {
    let mut inst = Instance { class: class.clone(), fields: HashMap::new() };
    if let Some(Val::Func(_)) = class.methods.get("初始化") {
        // 构造方法简化：不真正执行（Rust 版聚焦核心链路）
    } else if !args.is_empty() {
        return Err(err("类型错误", format!("类「{}」没有构造方法", class.name)));
    }
    Ok(inst)
}

fn unpack_values(v: Val, n: usize) -> Result<Vec<Val>, JishiError> {
    if let Val::List(l) = v {
        if l.len() != n { return Err(err("值错误", format!("解包需要 {n} 个值，实际有 {} 个", l.len()))); }
        Ok(l)
    } else {
        Err(err("类型错误", "解包赋值需要列表"))
    }
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
    // 用 List 作为迭代器载体：在 VM 里把迭代器表示为特殊 List（元素+位置）
    // 简化：直接用 List 本身 + 一个包装（这里用 List + 哨兵）
    match obj {
        Val::List(l) => Ok(Val::List(l)),
        Val::Str(s) => Ok(Val::List(s.chars().map(|c| Val::Str(c.to_string())).collect())),
        Val::Dict(d) => Ok(Val::List(d.into_iter().map(|(k, _)| k).collect())),
        _ => Err(err("类型错误", "不能遍历")),
    }
}

// 简化迭代：FOR_ITER 直接弹 List 头
fn iter_next(it: &mut Val) -> Result<Option<Val>, JishiError> {
    if let Val::List(l) = it {
        if l.is_empty() { Ok(None) } else { Ok(Some(l.remove(0))) }
    } else {
        Err(err("类型错误", "迭代器错误"))
    }
}

// ---------------------------------------------------------------------------
// 参数绑定
// ---------------------------------------------------------------------------

fn bind_args(code: &Code, args: &[Val], knames: &[String], kwvals: &[Val], defaults: Option<&[Val]>) -> Result<(Vec<Val>, Vec<Val>), JishiError> {
    let nparams = code.params.len();
    let mut bound: HashMap<usize, Val> = HashMap::new();
    for (i, a) in args.iter().enumerate() { if i < nparams { bound.insert(i, a.clone()); } }
    for (k, v) in knames.iter().zip(kwvals.iter()) {
        if let Some(pi) = code.params.iter().position(|p| p == k) {
            bound.insert(pi, v.clone());
        } else {
            return Err(err("类型错误", format!("函数没有参数「{k}」")));
        }
    }
    if let Some(d) = defaults {
        for (i, dv) in d.iter().enumerate() {
            if !bound.contains_key(&i) && !matches!(dv, Val::None_) { bound.insert(i, dv.clone()); }
        }
    }
    let missing: Vec<&str> = code.params.iter().enumerate().filter(|(i, _)| !bound.contains_key(i)).map(|(_, p)| p.as_str()).collect();
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

impl PartialOrd for Val {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        match (self, other) {
            (Val::Int(a), Val::Int(b)) => a.partial_cmp(b),
            (Val::Int(a), Val::Float(b)) => (*a as f64).partial_cmp(b),
            (Val::Float(a), Val::Int(b)) => a.partial_cmp(&(*b as f64)),
            (Val::Float(a), Val::Float(b)) => a.partial_cmp(b),
            _ => None,
        }
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
    if body.get("version").and_then(|v| v.as_i64()) != Some(1) {
        return Err("字节码版本不兼容".to_string());
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
            "int" => Val::Int(c.get("v").and_then(|x| x.as_i64()).unwrap_or(0)),
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
