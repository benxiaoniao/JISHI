# C VM 嵌入示例（M9.4）

演示第三方 C 宿主如何通过 `jsvm.h` 头文件链接 `jsvm.dll`，直嵌基石脚本引擎
（无需 libpython，比子进程 Python 快一个数量级）。

## hello.c —— 最小嵌入

加载一段最小字节码 `LOAD_CONST 42; STORE_LAST; HALT`，运行后取 `last_value`。

```bash
# 编译（MinGW；把 jsvm.dll 放到运行目录）
gcc hello.c -I ../../cvm/include ../../cvm/bin/jsvm.dll -o hello.exe
cp ../../cvm/bin/jsvm.dll .
./hello.exe
# 输出：OK: last_value = 42 (ABI v1)
```

## 公开 C ABI（cvm/include/jsvm.h）

- `JSVM_ABI_VERSION = 1`：结构布局/签名兼容版本，跨版本只追加不改动。
- `JSVM_API` 导出宏：构建库时 `-DJSVM_BUILD`（dllexport），
  宿主编译时默认（dllimport）。
- 核心结构：
  - `JVal`：16 字节带标签值（`tag` 见 `opcodes.h` 的 `JS_TAG_*`），
    inline 构造器 `jsvm_int` / `jsvm_float` / `jsvm_bool` / `jsvm_null` /
    `jsvm_host` / `jsvm_cell` / `jsvm_func`。
  - `JsHost`：宿主回调表（语义/异常/报错全部桥回宿主）。
  - `JsVm`：不完整类型（内部布局对宿主隐藏）。

## 生命周期

```
jsvm_create() → jsvm_set_host(vm, &host) → jsvm_load_module(vm, ...)
  → jsvm_run(vm) → jsvm_get_last(vm, &out) → jsvm_destroy(vm)
```

字节码扁平格式见 `cvm/bytecode.md`；完整序列化由 `jishi/cvm_bind.py`
（Python 侧）完成，第三方语言宿主可参照实现自己的字节码序列化器。

## 说明

- 字节码由 `jishi/compiler.py` 编译（Python 侧）；跨语言宿主需自行实现
  「编译产物 → 扁平数组」的序列化，或引入一个字节码中间格式（后续工作）。
- 复杂语义（打印/文本/列表/字典/导入/报错文案）经 `JsHost` 回调桥回宿主，
  C 侧只保留算术/比较/跳转/栈操作/调用返回等热路径。
