# -*- coding: utf-8 -*-
"""语言元数据的**共享提炼层**（M38 从 `lsp.py` 挪出来）。

`ai.build_lang_spec()` 是语言元数据的唯一来源（关键字 / 内建 / 标准库 / 方法 /
异常）。LSP 与 REPL 的补全都需要它，但都需要一点「提炼」——按用途分成几个字典。
这份提炼只该有一处：以前它在 `lsp.py` 里，M38 做 REPL 的 Tab 补全时如果照抄
一遍，就会出现两个地方各自解释同一份 spec（项目里已经为这类漂移付过代价，
见 AGENTS §2「不要在工具里另抄一份语言元数据」）。
"""

from __future__ import annotations


class LangData:
    """从 ``ai.build_lang_spec()`` 提炼补全/悬停需要的数据。"""

    def __init__(self) -> None:
        from .ai import build_lang_spec

        spec = build_lang_spec()
        self.spec = spec
        self.keywords: dict[str, str] = dict(spec.get("keywords") or {})
        self.builtins: dict[str, str] = {
            b["name"]: b.get("doc", "")
            for b in (spec.get("builtins") or [])
        }
        self.stdlib: dict[str, list[dict]] = {
            m["module"]: list(m.get("functions") or [])
            for m in (spec.get("stdlib") or [])
        }
        self.methods: dict[str, list[str]] = dict(spec.get("methods") or {})
        self.exceptions: set[str] = set(spec.get("exceptions") or {})

    def all_methods(self) -> list[str]:
        out: list[str] = []
        for names in self.methods.values():
            for n in names:
                if n not in out:
                    out.append(n)
        return out

    def top_level_names(self) -> list[str]:
        """顶层候选：关键字 + 内建 + 标准库模块名。

        REPL 与编辑器的补全都从这里起步——**不要各自去拼一遍**，
        否则「新加了内建，一个地方有一处没有」这类漂移就会重演。
        """
        names = list(self.keywords)
        names += list(self.builtins)
        names += list(self.stdlib)
        out: list[str] = []
        seen: set[str] = set()
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out
