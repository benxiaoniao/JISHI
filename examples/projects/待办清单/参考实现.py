# -*- coding: utf-8 -*-
"""待办清单（Python 参考实现，只用标准库）。

与「待办.jsh」同功能——用来算「基石版行数 ÷ Python 版行数」。
数据存在同目录的 待办.json。
"""

import json
import sys
from datetime import datetime
from pathlib import Path

DATA = Path("待办.json")

USAGE = """用法：python 参考实现.py 命令 [参数]
命令：
  列出                  看全部任务（带序号）
  添加 任务内容         加一条（多个词会拼成一条）
  完成 序号             标记完成
  取消完成 序号         把标记撤回去
  删除 序号             删掉一条
  清空已完成            把已完成的全删掉"""


def load():
    if not DATA.exists():
        return []
    try:
        items = json.loads(DATA.read_text(encoding="utf-8"))
        return items if isinstance(items, list) else []
    except (OSError, json.JSONDecodeError):
        print(f"（{DATA} 读不动，先按空清单处理）")
        return []


def save(items):
    DATA.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def show(items):
    if not items:
        print("（清单是空的，先「添加」一条吧）")
        return
    done = sum(1 for it in items if it.get("完成"))
    print("== 待办清单 ==")
    for i, it in enumerate(items, 1):
        print(f"  [{'√' if it.get('完成') else ' '}] {i}. {it.get('任务', '')}")
    print()
    print(f"共 {len(items)} 条：已完成 {done}，未完成 {len(items) - done}")


def index_of(args, items):
    if not args:
        print("这条命令需要一个序号，例如：python 参考实现.py 完成 2")
        return None
    try:
        i = int(args[0]) - 1
    except ValueError:
        print(f"「{args[0]}」不是序号——序号是数字，例如 3")
        return None
    if not 0 <= i < len(items):
        print(f"清单里没有第 {i + 1} 条（共 {len(items)} 条）")
        return None
    return i


def main():
    args = sys.argv[1:]
    if not args:
        print(USAGE)
        return
    cmd, rest = args[0], args[1:]
    items = load()

    if cmd == "列出":
        show(items)
    elif cmd == "添加":
        if not rest:
            print("要加什么？例如：python 参考实现.py 添加 买牛奶")
            return
        text = " ".join(rest)
        items.append({"任务": text, "完成": False, "创建": datetime.now().isoformat()})
        save(items)
        print(f"已添加第 {len(items)} 条：{text}")
    elif cmd in ("完成", "取消完成"):
        i = index_of(rest, items)
        if i is None:
            return
        items[i]["完成"] = cmd == "完成"
        save(items)
        print(f"第 {i + 1} 条现在：{items[i]['任务']}（{cmd}）")
    elif cmd == "删除":
        i = index_of(rest, items)
        if i is None:
            return
        gone = items.pop(i)
        save(items)
        print(f"已删除：{gone['任务']}")
    elif cmd == "清空已完成":
        left = [it for it in items if not it.get("完成")]
        save(left)
        print(f"清掉了 {len(items) - len(left)} 条已完成，还剩 {len(left)} 条")
    else:
        print(f"不认识这个命令：「{cmd}」")
        print()
        print(USAGE)


main()
