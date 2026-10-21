"""统一使用宿主原生上下文 Item 构造器。"""

from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType


def text_item(role: RoleType, text: str):
    """用宿主正式 Item 构造器保留原生思考及工具协议。"""
    return ContextItemBuilder().set_role(role).add_text_content(text).build()
