"""OpenCode Go 原生 Messages 协议转换；不包含凭据或网络操作。"""
from copy import deepcopy
import json

MODEL = 'claude-haiku-5-5'
BASE = 'https://opencode.ai/zen/go/v1'
# OpenCode 固定走 WSL 本机代理桥；DeepSeek 由自己的客户端选择直连通道。
PROXY = 'http://127.0.0.1:2081'


def thinking_settings(model_params=None, request_params=None):
    """只允许已适配的思考与 effort 参数，不透传任意请求字段。"""
    params = {**(model_params or {}), **(request_params or {})}
    if set(params) - {'thinking', 'output_config'}:
        raise ValueError('不支持的额外模型参数')
    thinking = params.get('thinking', {'type': 'disabled'})
    if not isinstance(thinking, dict) or set(thinking) - {'type', 'display'}:
        raise ValueError('思考参数格式错误')
    if thinking.get('type') not in {'disabled', 'adaptive'}:
        raise ValueError('只支持关闭思考或adaptive思考')
    if 'display' in thinking and (thinking['type'] != 'adaptive' or thinking['display'] not in {'omitted', 'summarized'}):
        raise ValueError('思考展示参数格式错误')
    output = params.get('output_config')
    if output is not None and (not isinstance(output, dict) or set(output) != {'effort'} or output['effort'] not in {'low', 'medium', 'high'}):
        raise ValueError('仅支持low、medium、high effort')
    return deepcopy({'thinking': thinking, **({'output_config': output} if output is not None else {})})


def validate_native_blocks(blocks):
    """校验原生内容数组；完整保留思考签名、遮蔽块及文本/工具顺序。"""
    if not isinstance(blocks, list) or not blocks:
        raise ValueError('原生响应内容不能为空')
    calls = []
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError('原生内容块格式错误')
        kind = block.get('type')
        if kind == 'thinking':
            if not isinstance(block.get('thinking'), str) or not isinstance(block.get('signature'), str) or not block['signature']:
                raise ValueError('思考块缺少文本字段或签名')
        elif kind == 'redacted_thinking':
            if not isinstance(block.get('data'), str) or not block['data']:
                raise ValueError('遮蔽思考块缺少原生数据')
        elif kind == 'text':
            if not isinstance(block.get('text'), str):
                raise ValueError('文本块格式错误')
        elif kind == 'tool_use':
            if not block.get('id') or not block.get('name') or not isinstance(block.get('input'), dict):
                raise ValueError('原生工具调用格式错误')
            calls.append(block['id'])
        else:
            raise ValueError('不支持的原生内容类型')
    if len(calls) != len(set(calls)):
        raise ValueError('原生工具调用编号重复')
    return calls


def content_blocks(content):
    """保留文本和内嵌图片；未知内容明确拒绝，不静默丢失。"""
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content else []
    blocks = []
    for part in content or []:
        if part.get('type') == 'text':
            if part.get('text'):
                blocks.append({'type': 'text', 'text': part['text']})
        elif part.get('type') == 'image_url':
            url = part['image_url']['url']
            if not url.startswith('data:image/') or ';base64,' not in url:
                raise ValueError('OpenCode适配只接受内嵌图片，不请求外部图片地址')
            media, encoded = url[5:].split(';base64,', 1)
            if media not in {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}:
                raise ValueError('不支持的图片格式')
            blocks.append({'type': 'image', 'source': {'type': 'base64', 'media_type': media, 'data': encoded}})
        else:
            raise ValueError('不支持的消息内容类型')
    return blocks


def messages_payload(messages, tools=None, max_tokens=512):
    """把宿主的通用消息投影转换为原生工具调用和工具结果。"""
    if not isinstance(max_tokens, int) or not 1 <= max_tokens <= 8192:
        raise ValueError('测试适配限制输出为1至8192 tokens')
    system, turns = [], []
    pending = set()
    for message in messages:
        role = message['role']
        if role in {'system', 'developer'}:
            blocks = content_blocks(message.get('content'))
            if any(b['type'] != 'text' for b in blocks):
                raise ValueError('系统规则只允许文本')
            system.extend(blocks)
            continue
        if role == 'tool':
            call_id = message['tool_call_id']
            if call_id not in pending:
                raise ValueError('工具结果缺少对应调用')
            blocks = [{'type': 'tool_result', 'tool_use_id': call_id, 'content': message.get('content') or ''}]
            pending.remove(call_id)
            role = 'user'
        elif role in {'assistant', 'user'}:
            if pending:
                raise ValueError('必须先回传所有工具结果，再开始新消息')
            if 'native_content' in message:
                if role != 'assistant' or message.get('tool_calls'):
                    raise ValueError('原生内容只能用于完整assistant回放')
                blocks = deepcopy(message['native_content'])
                pending.update(validate_native_blocks(blocks))
                # 保留原生assistant边界，不与相邻assistant合并或重排思考块。
                turns.append({'role': 'assistant', 'content': blocks})
                continue
            blocks = content_blocks(message.get('content'))
            if message.get('reasoning_content'):
                raise ValueError('试用适配不回放其他供应商的原生思考')
            for call in message.get('tool_calls') or []:
                if role != 'assistant' or call.get('type') != 'function':
                    raise ValueError('无效的工具调用')
                arguments = json.loads(call['function']['arguments'])
                if not isinstance(arguments, dict) or not call.get('id') or call['id'] in pending:
                    raise ValueError('工具参数必须是对象，调用编号必须唯一')
                pending.add(call['id'])
                blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': arguments})
        else:
            raise ValueError('不支持的消息角色')
        if blocks:
            if turns and turns[-1]['role'] == role:
                turns[-1]['content'].extend(blocks)
            else:
                turns.append({'role': role, 'content': blocks})
    if pending:
        raise ValueError('请求包含缺少结果的工具调用')
    if not turns or turns[-1]['role'] != 'user':
        raise ValueError('请求必须以用户消息或完整工具结果结束')
    if turns[0]['role'] == 'assistant':
        # 滑动窗口可能从历史回复开始；补中性起点，保留原角色、正文及思考签名。
        turns.insert(0, {'role': 'user', 'content': [
            {'type': 'text', 'text': '【历史上下文起点】以下是此前对话记录。'}]})
    payload = {'model': MODEL, 'max_tokens': max_tokens, 'stream': False, 'thinking': {'type': 'disabled'}, 'messages': turns}
    if system:
        payload['system'] = system
    if tools:
        payload['tools'] = [{'name': t['name'], 'description': t.get('description', ''),
                             'input_schema': t.get('parameters') or {'type': 'object', 'properties': {}}} for t in tools]
    return payload


def response_parts(data):
    """校验实际型号与终止状态，提取文本、工具及真实用量。"""
    if not isinstance(data, dict) or not isinstance(data.get('content'), list):
        raise ValueError('响应必须为对象且包含内容数组')
    if any(not isinstance(part, dict) for part in data['content']):
        raise ValueError('响应内容块必须为对象')
    validate_native_blocks(data['content'])
    if data.get('model') != MODEL:
        raise ValueError('服务端没有返回指定的Haiku 5.5；不接受静默回退')
    if data.get('stop_reason') not in {'end_turn', 'tool_use'}:
        raise ValueError('响应未完整结束或输出被截断')
    texts, calls, ids = [], [], set()
    for part in data.get('content', []):
        if part.get('type') == 'text':
            texts.append(part['text'])
        elif part.get('type') == 'tool_use':
            if not part.get('id') or part['id'] in ids or not part.get('name') or not isinstance(part.get('input'), dict):
                raise ValueError('响应工具调用格式错误')
            ids.add(part['id'])
            calls.append(part)
        elif part.get('type') in {'thinking', 'redacted_thinking'}:
            # 思考只通过原生回放保存，不拼入群聊正文或通用reasoning字段。
            continue
        else:
            raise ValueError('试用适配不支持此响应类型')
    if (data['stop_reason'] == 'tool_use') != bool(calls):
        raise ValueError('工具调用与终止状态不一致')
    if not texts and not calls:
        raise ValueError('模型没有返回文本或工具')
    usage = data.get('usage') or {}
    if not isinstance(usage, dict):
        raise ValueError('用量必须为对象')
    if any(type(usage.get(k, 0)) is not int or usage.get(k, 0) < 0 for k in ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')):
        raise ValueError('用量必须为非负整数')
    uncached = int(usage.get('input_tokens', 0))
    read = int(usage.get('cache_read_input_tokens', 0))
    write = int(usage.get('cache_creation_input_tokens', 0))
    output = int(usage.get('output_tokens', 0))
    return '\n'.join(texts), calls, {'prompt': uncached + read + write, 'output': output,
                                    'cache_hit': read, 'cache_miss': uncached + write,
                                    'cache_reported': 'cache_read_input_tokens' in usage}


def response_metadata(data):
    """诊断只允许固定枚举和数字，不回显服务端任意字符串或正文。"""
    if not isinstance(data, dict):
        return {'response_object': False}
    stop = data.get('stop_reason')
    usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
    blocks = data.get('content') if isinstance(data.get('content'), list) else []
    types = {'text', 'tool_use', 'thinking', 'redacted_thinking'}
    allowed_stops = {'end_turn', 'tool_use', 'max_tokens', 'stop_sequence', 'pause_turn', 'refusal'}
    return {'model_matches': data.get('model') == MODEL,
            'stop_reason': stop if isinstance(stop, str) and stop in allowed_stops else 'unknown',
            'content_types': [p.get('type') if isinstance(p.get('type'), str) and p.get('type') in types else 'unknown'
                              for p in blocks[:32] if isinstance(p, dict)],
            'usage': {k: usage[k] for k in ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
                      if type(usage.get(k)) is int and usage[k] >= 0}}
