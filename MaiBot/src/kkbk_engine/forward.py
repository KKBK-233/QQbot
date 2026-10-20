"""合并转发的分级展开。

L0 结构摘要由代码生成，不调模型；L1 把转发内文字放进轻路径上下文，受层数、节点数、字数上限约束；
L2 图片识别由模型经工具按需申请，这里只收集图片哈希并编号。
输入是 MessageSequence.to_dict() 里 forward 组件的 data 列表。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence

from .config import ForwardLimits

FORWARD_SOURCE_NOTE = "转发内容，非群友当前发言"

ForwardNodes = Sequence[Mapping[str, Any]]


@dataclass
class ForwardRender:
    node_count: int
    depth: int
    image_count: int
    senders: List[str]
    preview: str
    lines: List[str]
    """L1 展开行，已按层级缩进。"""
    image_hashes: List[str]
    """L1 中出现的图片哈希；图片序号按完整记录的深度优先顺序确定。"""
    folded_nodes: int
    truncated: bool

    def _header(self) -> str:
        senders = "、".join(self.senders[:3]) or "未知"
        if len(self.senders) > 3:
            senders += f" 等{len(self.senders)}人"
        return (
            f"【合并转发（{FORWARD_SOURCE_NOTE}）：{self.node_count} 条，{self.depth} 层，"
            f"{self.image_count} 张图，发送者 {senders}】"
        )

    @property
    def summary(self) -> str:
        """L0：只有结构和开头几十字。"""
        return f"{self._header()} 开头：{self.preview}" if self.preview else self._header()

    @property
    def text(self) -> str:
        """L1：结构摘要 + 展开内容 + 折叠说明。"""
        parts = [self._header(), *self.lines]
        if self.image_count:
            parts.append(f"（共有 {self.image_count} 张原图，含折叠节点。图片标记不是图像本身；"
                         "需看细节时调用 analyze_task_image，message_id 用这条外层群消息编号，"
                         "image_numbers 按图片序号选择，每次最多 4 张；可分批读取。）")
        if self.folded_nodes:
            parts.append(f"（另有 {self.folded_nodes} 条未展开）")
        elif self.truncated:
            parts.append("（内容过长，后面没展开）")
        return "\n".join(parts)


def render_forward(nodes: ForwardNodes, limits: ForwardLimits) -> ForwardRender:
    stats = _Stats(preview_chars=limits.preview_chars)
    stats.walk(nodes, depth=1)
    expander = _Expander(limits, stats.image_indices)
    expander.walk(nodes, depth=1)
    return ForwardRender(
        node_count=stats.nodes,
        depth=stats.depth,
        image_count=stats.images,
        senders=stats.senders,
        preview=stats.preview,
        lines=expander.lines,
        image_hashes=expander.image_hashes,
        folded_nodes=expander.folded_nodes,
        truncated=expander.truncated,
    )


def count_forward_nodes(nodes: ForwardNodes) -> int:
    """递归统计节点数（含嵌套转发）。"""
    total = 0
    for node in nodes:
        total += 1
        for segment in node.get("content") or ():
            if segment.get("type") == "forward":
                total += count_forward_nodes(segment.get("data") or ())
    return total


def _node_name(node: Mapping[str, Any]) -> str:
    return str(node.get("user_cardname") or node.get("user_nickname") or node.get("user_id") or "未知")


@dataclass
class _Stats:
    preview_chars: int
    nodes: int = 0
    depth: int = 0
    images: int = 0
    senders: List[str] = field(default_factory=list)
    preview: str = ""
    image_indices: Dict[int, int] = field(default_factory=dict)

    def walk(self, nodes: ForwardNodes, depth: int) -> None:
        self.depth = max(self.depth, depth)
        for node in nodes:
            self.nodes += 1
            name = _node_name(node)
            if name not in self.senders:
                self.senders.append(name)
            for segment in node.get("content") or ():
                segment_type = segment.get("type")
                if segment_type == "text":
                    if len(self.preview) < self.preview_chars:
                        text = str(segment.get("data") or "").replace("\n", " ")
                        self.preview = (self.preview + text)[: self.preview_chars]
                elif segment_type == "image":
                    self.images += 1
                    self.image_indices[id(segment)] = self.images
                elif segment_type == "forward":
                    self.walk(segment.get("data") or (), depth + 1)


class _Expander:
    """按层数、节点数、字数上限展开转发文字。"""

    def __init__(self, limits: ForwardLimits, image_indices: Mapping[int, int]) -> None:
        self._limits = limits
        self._image_indices = image_indices
        self.lines: List[str] = []
        self.image_hashes: List[str] = []
        self.folded_nodes = 0
        self.truncated = False
        self._used_nodes = 0
        self._used_chars = 0

    def walk(self, nodes: ForwardNodes, depth: int) -> None:
        indent = "  " * depth
        for index, node in enumerate(nodes):
            if self._used_nodes >= self._limits.max_nodes or self._used_chars >= self._limits.max_chars:
                self.folded_nodes += count_forward_nodes(nodes[index:])
                self.truncated = True
                return
            self._used_nodes += 1

            parts: List[str] = []
            nested: List[ForwardNodes] = []
            for segment in node.get("content") or ():
                parts.append(self._segment_text(segment, depth, nested))
            body = "".join(parts).replace("\r\n", " ").replace("\n", " ")
            remaining = self._limits.max_chars - self._used_chars
            if len(body) > remaining:
                body = body[:remaining] + "…"
                self.truncated = True
            self._used_chars += len(body)
            self.lines.append(f"{indent}{_node_name(node)}: {body}")
            for nested_nodes in nested:
                self.walk(nested_nodes, depth + 1)

    def _segment_text(self, segment: Mapping[str, Any], depth: int, nested: List[ForwardNodes]) -> str:
        segment_type = segment.get("type")
        if segment_type == "text":
            return str(segment.get("data") or "")
        if segment_type == "image":
            self.image_hashes.append(str(segment.get("hash") or ""))
            number = self._image_indices[id(segment)]
            description = segment.get("description") or segment.get("data") or ""
            detail = f"（已有描述：{description[:100]}）" if isinstance(description, str) and description.strip() else ""
            return f"[图片{number}]" + detail
        if segment_type == "emoji":
            return "[表情]"
        if segment_type == "at":
            data: Dict[str, Any] = segment.get("data") or {}
            name = data.get("target_user_cardname") or data.get("target_user_nickname") or data.get("target_user_id")
            return f"@{name}"
        if segment_type == "reply":
            return "[引用]"
        if segment_type == "voice":
            return "[语音]"
        if segment_type == "file":
            return "[文件]"
        if segment_type == "forward":
            nested_nodes: ForwardNodes = segment.get("data") or ()
            if depth < self._limits.max_depth:
                nested.append(nested_nodes)
                return "[嵌套转发，见下]"
            count = count_forward_nodes(nested_nodes)
            self.folded_nodes += count
            self.truncated = True
            return f"[嵌套转发 {count} 条，未展开]"
        return f"[{segment_type}]"
