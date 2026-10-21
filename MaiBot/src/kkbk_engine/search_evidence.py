"""收集本轮真正取得的搜索资料；阶段结束时由宿主交付已有来源和未完成项。"""

import re


class SearchEvidence:
    """只记录真实工具结果，不用模型自述判定是否搜到资料。"""

    _SEARCH_TOOLS = frozenset({"web_search", "read_webpage"})
    _MAX_ISSUES = 6
    _MAX_SOURCES_IN_REPLY = 6
    _MAX_EXCERPT_IN_REPLY = 220
    _MIN_EXCERPT_IN_REPLY = 16
    _HEADER = "已取得的资料（阶段结果，仍需核实）："
    _FOOTER_PREFIX = "\n\n本轮检索阶段已结束，先交付以上资料；"
    _END = "其余线索及交叉核实尚未完成。"
    _ISSUE_LEAD = "以下调用未完成：\n"
    _ISSUE_OMITTED = "（其余失败/未执行说明已省略）"

    def __init__(self):
        self.attempts = 0
        self.sources = {}
        self.issues = []

    def add(self, tool_name: str, success: bool, text: str, *, arguments: dict | None = None,
            not_executed: bool = False) -> None:
        """保留来源及有界摘录；失败与未执行分开记录，不推测远端站点状态。"""
        if tool_name not in self._SEARCH_TOOLS:
            return
        self.attempts += 1
        text = text or ""
        if not success:
            # 失败原因原样保留，避免把远端拒绝、页面读取失败说成“没有结果”。
            if len(self.issues) < self._MAX_ISSUES:
                detail = " ".join(text.split())[:240]
                self.issues.append((tool_name, dict(arguments or {}),
                                    "not_executed" if not_executed else "failed", detail))
            return
        if not text.strip():
            return
        if tool_name == "web_search":
            blocks = re.findall(r"(?ms)^\d+\.\s+(.*?)\n来源[:：]\s*(https?://[^\s]+)", text)
        else:
            source = re.search(r"(?m)^来源[:：]\s*(https?://[^\s]+)", text)
            title = (text[:source.start()].splitlines() or ["已读取页面"])[-1] if source else ""
            blocks = [(title + "\n" + text[source.end():], source.group(1))] if source else []
        if not blocks:
            blocks = [(text, "")]
        for excerpt, url in blocks:
            if len(self.sources) >= 12:
                break
            excerpt = " ".join(excerpt.split()).replace("@", "＠")
            if not excerpt:
                continue
            key = url or excerpt[:120]
            if key not in self.sources:
                self.sources[key] = (excerpt[:1600], url, tool_name)

    def _issue_lines(self) -> list[str]:
        """把失败/未执行调用转成可读行，标明来源而不推断具体站点原因。"""
        lines = []
        for tool_name, arguments, status, detail in self.issues:
            target = arguments.get("query") or arguments.get("url") or ""
            target = " ".join(str(target).split())[:120].replace("@", "＠")
            label = tool_name + (f"（{target}）" if target else "")
            detail = detail.replace("@", "＠")
            if status == "not_executed":
                reason = detail or "宿主本地冷却等待预算不足，未向远端发出请求"
            else:
                reason = "工具返回失败" + (f"：{detail}" if detail else "（没有提供原因）")
            lines.append(f"{label}：{reason}")
        return lines

    def _source_block(self, index: int, excerpt: str, url: str, tool_name: str,
                      shown_len: int) -> str:
        """按给定正文长度渲染一条来源，来源链接始终完整保留。"""
        label = "搜索摘要" if tool_name == "web_search" else "页面摘录"
        shown = excerpt[:shown_len]
        if shown_len < len(excerpt):
            shown += "…"
        block = f"{index}. {label}：{shown}"
        if url:
            block += "\n来源：" + url
        return block

    def _issue_section(self, issue_lines: list[str], budget: int) -> str:
        """只在给定预算内拼失败说明；放不下时裁剪并明确提示已有省略。"""
        if not issue_lines or budget <= 0:
            return ""
        full = self._ISSUE_LEAD + "\n".join(issue_lines) + "\n"
        if len(full) <= budget:
            return full
        # 先为省略提示留出空间，再逐条放入完整行或裁剪后的首行。
        if budget <= len(self._ISSUE_LEAD) + len(self._ISSUE_OMITTED) + 2:
            return ""
        parts = []
        used = len(self._ISSUE_LEAD)
        reserve = len(self._ISSUE_OMITTED) + 2
        for line in issue_lines:
            room = budget - used - reserve - 1
            if room < 1:
                break
            if len(line) <= room:
                parts.append(line)
                used += len(line) + 1
            else:
                # 至少保留一小段可读原因，避免只剩工具名和 URL。
                if room >= 16:
                    clipped = line[:room - 1] + "…"
                    parts.append(clipped)
                    used += len(clipped) + 1
                break
        if not parts:
            return ""
        parts.append(self._ISSUE_OMITTED)
        section = self._ISSUE_LEAD + "\n".join(parts) + "\n"
        # 仅对本段做最后保护，避免计数误差影响总长度；来源正文已在外部预留。
        return section[:budget]

    def _fallback_source_reply(self, max_chars: int,
                               candidates: list[tuple[str, str, str]]) -> str:
        """预算极小时优先保留一条真实来源链接和摘录，不返回只有标题的“成功”。"""
        if max_chars <= 0 or not candidates:
            return ""
        # 有链接的来源优先，避免在缩小时只留下没有出处的摘要。
        excerpt, url, tool_name = next((item for item in candidates if item[1]), candidates[0])
        label = "搜索摘要" if tool_name == "web_search" else "页面摘录"
        header = self._HEADER + "\n"
        title = f"1. {label}："
        url_part = "\n来源：" + url if url else ""
        # 依次尝试“标题+摘录+链接”，空间不足时去掉标题或链接，始终给真实摘录留位置。
        layouts = []
        if url:
            layouts.extend(((header + title, url_part), (title, url_part), ("", url_part)))
        layouts.extend(((header + title, ""), (title, ""), ("", "")))
        minimum = min(len(excerpt), 8)
        for prefix, suffix in layouts:
            available = max_chars - len(prefix) - len(suffix)
            if available >= minimum:
                shown = excerpt[:available]
                if len(shown) < len(excerpt):
                    shown = excerpt[:max(0, available - 1)] + "…"
                return prefix + shown + suffix
        # 预算连最小摘录都放不下时，直接交付真实摘录开头，不再显示成功标题。
        return excerpt[:max_chars]

    def _sources_reply(self, max_chars: int, candidates: list[tuple[str, str, str]],
                       issue_lines: list[str]) -> str:
        """先按预算放入来源链接和摘录，再把剩余空间分配给失败/未执行说明。"""
        if max_chars <= 0:
            return ""
        tail_len = len(self._FOOTER_PREFIX) + len(self._END)
        # 第一轮只选能完整容纳“来源链接 + 最小摘录”的条目，保证不会出现负预算。
        chosen = []
        used = len(self._HEADER) + 1 + tail_len
        for excerpt, url, tool_name in candidates:
            index = len(chosen) + 1
            minimum = min(len(excerpt), self._MIN_EXCERPT_IN_REPLY)
            block = self._source_block(index, excerpt, url, tool_name, minimum)
            extra = len(block) + (2 if chosen else 0)
            if used + extra <= max_chars:
                chosen.append((excerpt, url, tool_name))
                used += extra
        if not chosen:
            return self._fallback_source_reply(max_chars, candidates)
        # 第二轮把剩余空间公平地分给各条摘录；有失败说明时预留一小段预算。
        fixed = len(self._HEADER) + 1 + tail_len + 2 * (len(chosen) - 1)
        lengths = []
        for index, (excerpt, url, tool_name) in enumerate(chosen, 1):
            minimum = min(len(excerpt), self._MIN_EXCERPT_IN_REPLY)
            lengths.append(minimum)
            fixed += len(self._source_block(index, excerpt, url, tool_name, minimum))
        available = max(0, max_chars - fixed)
        issue_reserve = min(120, available) if issue_lines else 0
        excerpt_budget = available - issue_reserve
        spent = 0
        while spent < excerpt_budget:
            progressed = False
            for index, (excerpt, _url, _tool_name) in enumerate(chosen):
                if lengths[index] >= min(len(excerpt), self._MAX_EXCERPT_IN_REPLY):
                    continue
                old_cost = lengths[index] + (1 if lengths[index] < len(excerpt) else 0)
                new_len = lengths[index] + 1
                new_cost = new_len + (1 if new_len < len(excerpt) else 0)
                change = new_cost - old_cost
                if change == 0 or spent + change <= excerpt_budget:
                    lengths[index] = new_len
                    spent += change
                    progressed = True
            if not progressed:
                break
        blocks = []
        for index, (excerpt, url, tool_name) in enumerate(chosen, 1):
            blocks.append(self._source_block(index, excerpt, url, tool_name, lengths[index - 1]))
        body = self._HEADER + "\n" + "\n\n".join(blocks)
        base = body + self._FOOTER_PREFIX
        issue_budget = max_chars - len(base) - len(self._END)
        section = self._issue_section(issue_lines, issue_budget)
        return base + section + self._END

    def _no_source_reply(self, max_chars: int, issue_lines: list[str]) -> str:
        """没有成功来源时只陈述失败/未执行，不虚构检索结论。"""
        if max_chars <= 0:
            return ""
        if not issue_lines:
            text = "本轮检索暂未取得可用资料，尚不能下结论；待核实的线索仍未完成。"
            return text[:max_chars]
        header = ("本轮检索未取得可用来源；以下调用失败或未执行，"
                  "不能据此断言检索没有命中或站点限流：\n")
        tail = "\n待核实的线索仍未完成。"
        issue_budget = max_chars - len(header) - len(tail)
        if issue_budget > 0:
            section = self._issue_section(issue_lines, issue_budget)
            if section:
                return header + section + tail
        # 预算小到放不下完整提示时，至少保留失败/未执行线索本身。
        compact = "本轮检索未取得可用来源；"
        if len(compact) >= max_chars:
            return compact[:max_chars]
        remaining = max_chars - len(compact)
        return compact + issue_lines[0][:remaining]

    def partial_reply(self, max_chars: int) -> str:
        """优先交付真实来源与摘录，再在剩余预算内说明失败或未执行。"""
        max_chars = max(0, int(max_chars))
        issue_lines = self._issue_lines()
        candidates = list(self.sources.values())[:self._MAX_SOURCES_IN_REPLY]
        if not candidates:
            return self._no_source_reply(max_chars, issue_lines)
        return self._sources_reply(max_chars, candidates, issue_lines)
