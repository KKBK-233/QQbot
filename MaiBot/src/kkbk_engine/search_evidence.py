"""收集本轮真正取得的搜索资料；预算耗尽时由宿主交付阶段结果。"""

import re


class SearchEvidence:
    """只记录真实工具结果，不用模型自述判定是否搜到资料。"""

    def __init__(self):
        self.attempts = 0
        self.sources = {}

    def add(self, tool_name: str, success: bool, text: str) -> None:
        """保留来源及有界摘录；搜索摘要与页面正文不视为事实核实结论。"""
        if tool_name not in {"web_search", "read_webpage"}:
            return
        self.attempts += 1
        if not success or not text.strip():
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

    def partial_reply(self, max_chars: int) -> str:
        """优先交付摘录和完整来源，避免收尾模型把已有结果说成零结果。"""
        header = "先把已取得的资料发出来（阶段结果，摘要和页面摘录仍需核实）："
        footer = "\n\n本轮检索额度已到，先交付以上资料；其余线索及交叉核实尚未完成。"
        if not self.sources:
            return "本轮检索暂未取得可用资料，尚不能下结论；待核实的线索仍未完成。"[:max_chars]
        # 先为每条来源预留篇幅，至少交付一条正文摘录，不让长 URL 挤掉所有内容。
        candidates = list(self.sources.values())[:6]
        usable = max_chars - len(header) - len(footer)
        chosen = []
        for excerpt, url, tool_name in candidates:
            minimum = len(url) + 70
            if minimum <= usable:
                chosen.append((excerpt, url, tool_name))
                usable -= minimum
        if not chosen:
            excerpt = next(iter(self.sources.values()))[0]
            return (header + "\n" + excerpt[:max(0, max_chars-len(header)-len(footer)-1)] + footer)[:max_chars]
        extra = usable // len(chosen)
        parts = [header]
        for index, (excerpt, url, tool_name) in enumerate(chosen, 1):
            label = "搜索摘要" if tool_name == "web_search" else "页面摘录"
            shown = excerpt[:min(220, 44 + extra)]
            if len(shown) < len(excerpt):
                shown += "…"
            parts.append(f"{index}. {label}：{shown}" + ("\n来源：" + url if url else ""))
        return ("\n\n".join(parts) + footer)[:max_chars]
