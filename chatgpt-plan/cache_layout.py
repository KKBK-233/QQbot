"""稳定固定宿主规则和工具定义，保留历史、外部资料及即时状态的身份。"""

from copy import deepcopy
import json
from typing import Any, Dict, List, Optional, Tuple


STATIC_PREFIXES = ('你的身份是成年时期的《魔女之旅》伊蕾娜，', '以当前人设为准，历史中机器人已经说过的话')
STATUS_MARKER = '\n【本会话生图任务只读状态】'


def _split_known_instruction(item: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
    """识别 MaiBot 实际使用的字符串块和兼容的 input_text 块。"""
    if item.get('role') not in {'developer', 'system'}:
        return None
    content = item.get('content')
    block = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
        block = content[0]
        if block.get('type') not in {'input_text', 'text'}:
            return None
        text = str(block.get('text', ''))
    else:
        return None
    if not text.startswith(STATIC_PREFIXES):
        return None
    fixed, separator, dynamic = text.partition(STATUS_MARKER)

    def with_text(value: str) -> Dict[str, Any]:
        updated = dict(item)
        if block is None:
            updated['content'] = value
        else:
            updated['content'] = [{**block, 'text': value}]
        return updated

    return with_text(fixed), with_text(separator + dynamic) if separator else None


def stable_instruction_prefix(inputs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """仅处理已部署插件的两个固定规则块，不重排聊天历史或工具调用、结果、思考。"""
    leading = 0
    while leading < len(inputs) and inputs[leading].get('role') in {'developer', 'system'}:
        leading += 1
    front, remaining = [], []
    for item in inputs[leading:]:
        split = _split_known_instruction(item)
        if split is None:
            remaining.append(item)
            continue
        fixed, dynamic = split
        front.append(fixed)
        if dynamic is not None:
            remaining.append(dynamic)
    return [*inputs[:leading], *front, *remaining]


def stable_tool_definitions(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """仅稳定函数顺序和JSON键，不改变工具名称、参数、权限或返回内容。"""
    canonical = [json.loads(json.dumps(tool, ensure_ascii=False, sort_keys=True)) for tool in deepcopy(tools)]
    return sorted(canonical, key=lambda tool: str(tool.get('function', tool).get('name', '')))
