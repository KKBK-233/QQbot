"""DeepSeek思考请求的消息投影，只补协议字段，不改写用户或工具内容。"""


def convert_thinking_messages(items, converter):
    """保留相邻模型输出对应的原生推理，旧非思考输出显式使用空字段。"""
    assistant_types = {'ReasoningItem', 'AssistantMessageItem', 'FunctionCallItem',
                       'ProviderActivityItem', 'ProviderOpaqueItem'}
    result = []
    offset = 0
    while offset < len(items):
        item = items[offset]
        if type(item).__name__ not in assistant_types:
            result.extend(converter([item]))
            offset += 1
            continue
        group = []
        while offset < len(items) and type(items[offset]).__name__ in assistant_types:
            group.append(items[offset])
            offset += 1
        projected = converter(group)
        # 仅回传原生文本，不把摘要或不透明提供商数据冒充原始推理。
        reasoning = '\n'.join(part for value in group if type(value).__name__ == 'ReasoningItem'
                              for part in getattr(value, 'text_parts', ()))
        for message in projected:
            if message.get('role') == 'assistant':
                message = {**message, 'reasoning_content': reasoning}
            result.append(message)
    return result
