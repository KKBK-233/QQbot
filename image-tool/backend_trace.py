"""生图公共阶段摘要与本机有界诊断分开保存，正文不进入状态接口。"""
import json
import os
from pathlib import Path
import re

PREFIX = 'QQBOT_IMAGE_TRACE '
EVENTS = {'response.created', 'response.in_progress', 'response.completed', 'response.failed', 'response.incomplete', 'error',
          'response.output_item.added', 'response.output_item.done',
          'response.output_text.done', 'response.refusal.done',
          'response.image_generation_call.in_progress', 'response.image_generation_call.generating',
          'response.image_generation_call.partial_image'}
ERROR_CODES = {'usage_limit_reached', 'rate_limit_exceeded', 'content_policy_violation', 'moderation_blocked',
               'server_error', 'internal_server_error', 'invalid_request_error', 'invalid_image', 'invalid_image_format',
               'invalid_image_url', 'invalid_image_size', 'authentication_error', 'model_not_found'}
EXCEPTIONS = {'GatewayError', 'HTTPError', 'URLError', 'SSLError', 'SSLEOFError', 'TimeoutError',
              'ConnectionError', 'ConnectionResetError', 'IncompleteRead', 'RemoteDisconnected',
              'FileNotFoundError', 'ValueError', 'SystemExit'}
STATUSES = {'queued', 'in_progress', 'generating', 'completed', 'failed', 'incomplete', 'cancelled'}
INCOMPLETE_REASONS = {'max_output_tokens', 'content_filter', 'turn_limit'}


def diagnostic_text(value):
    """只保留有界的输出文本，脱敏常见凭据与内嵌图片，不处理输入或推理内容。"""
    if not isinstance(value, str):
        return None
    value = value[:8192]
    value = re.sub(r'data:[^\s,]{1,120};base64,[A-Za-z0-9+/=]+', '[图片数据已省略]', value)
    value = re.sub(r'(?i)\bBearer\s+[^\s,;\"\']+', 'Bearer [已脱敏]', value)
    value = re.sub(r'(https?://)[^/@\s]+:[^/@\s]+@', r'\1[已脱敏]@', value)
    value = re.sub(r'(?i)((?:access_token|refresh_token|api[_-]?key|authorization|cookie)[\"\']?\s*[=:]\s*)[^\r\n,;]+',
                   r'\1[已脱敏]', value)
    value = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)',
                   '[已脱敏]', value)
    value = re.sub(r'[A-Za-z0-9+/=_-]{160,}', '[长数据已省略]', value)
    return value[:2048]


class BackendDiagnostic:
    """管理员本机诊断：保存终态、回答和拒绝说明，排除推理、图片、请求及凭据。"""
    def __init__(self):
        self.data = {'schema_version': 1, 'output_items': [], 'errors': []}
        self.items = {}

    def error(self, value):
        if not isinstance(value, dict):
            return
        record = {key: text for key in ('code', 'type', 'message')
                  if (text := diagnostic_text(value.get(key))) is not None}
        if record and record not in self.data['errors'] and len(self.data['errors']) < 8:
            self.data['errors'].append(record)

    def item(self, value):
        """仅摘录生图结果元数据与 assistant 最终内容，绝不遍历 reasoning。"""
        if not isinstance(value, dict) or value.get('type') not in ('message', 'image_generation_call'):
            return None
        record = {'type': value['type']}
        if isinstance(value.get('status'), str) and value['status'] in STATUSES:
            record['status'] = value['status']
        if value['type'] == 'image_generation_call':
            result = value.get('result')
            record['result_present'] = isinstance(result, str) and bool(result)
            record['result_chars'] = len(result) if isinstance(result, str) else 0
            record['image_config'] = image_config(value)
            revised = diagnostic_text(value.get('revised_prompt'))
            if revised is not None:
                record['revised_prompt'] = revised
        elif value.get('role', 'assistant') == 'assistant':
            record['content'] = []
            content = value.get('content')
            for part in (content[:4] if isinstance(content, list) else []):
                if not isinstance(part, dict) or part.get('type') not in ('output_text', 'refusal'):
                    continue
                key = 'refusal' if part['type'] == 'refusal' else 'text'
                text = diagnostic_text(part.get(key))
                if text is not None:
                    record['content'].append({'type': part['type'], key: text})
        else:
            return None
        error = value.get('error')
        if isinstance(error, dict):
            record['error'] = {key: text for key in ('code', 'type', 'message')
                               if (text := diagnostic_text(error.get(key))) is not None}
            self.error(error)
        return record

    def event(self, event):
        """从流终态或最终响应补齐记录；最终 output 存在时替换以避免重复。"""
        if not isinstance(event, dict):
            return
        response = event.get('response')
        if isinstance(response, dict):
            status = response.get('status')
            if isinstance(status, str) and status in STATUSES:
                self.data['response_status'] = status
            detail = response.get('incomplete_details')
            if isinstance(detail, dict):
                self.data['incomplete_reason'] = diagnostic_text(detail.get('reason'))
            self.error(response.get('error'))
            if event.get('type') in ('response.completed', 'response.failed', 'response.incomplete'):
                output = response.get('output')
                if isinstance(output, list) and output:
                    records = {str(index): record for index, value in enumerate(output[:16])
                               if (record := self.item(value)) is not None}
                    if records:
                        self.items = records
        self.error(event.get('error'))
        if event.get('type') == 'error':
            self.error(event)
        if event.get('type') == 'response.output_item.done':
            value = event.get('item')
            record = self.item(value)
            if record is not None and len(self.items) < 16:
                key = value.get('id')
                key = key if isinstance(key, str) else str(len(self.items))
                self.items[key] = record
        if event.get('type') in ('response.output_text.done', 'response.refusal.done') and len(self.items) < 16:
            part_type = 'refusal' if event['type'] == 'response.refusal.done' else 'output_text'
            key_name = 'refusal' if part_type == 'refusal' else 'text'
            text = diagnostic_text(event.get(key_name))
            if text is not None:
                key = event.get('item_id')
                key = key if isinstance(key, str) else str(len(self.items))
                record = self.items.setdefault(key, {'type': 'message', 'content': []})
                if record['type'] == 'message' and len(record['content']) < 4:
                    part = {'type': part_type, key_name: text}
                    if part not in record['content']:
                        record['content'].append(part)
        self.data['output_items'] = list(self.items.values())

    def summary(self):
        """公共日志只含布尔标记、白名单状态与数量，不携带输出正文。"""
        items = self.data['output_items']
        parts = [part for item in items for part in item.get('content', [])]
        result = {'image_call_statuses': [item['status'] for item in items
                  if item['type'] == 'image_generation_call' and 'status' in item],
                  'image_result_present': any(item.get('result_present', False) for item in items),
                  'assistant_text_present': any(part['type'] == 'output_text' and part.get('text') for part in parts),
                  'refusal_present': any(part['type'] == 'refusal' for part in parts)}
        if self.data.get('response_status') in STATUSES:
            result['response_status'] = self.data['response_status']
        if self.data.get('incomplete_reason') in INCOMPLETE_REASONS:
            result['incomplete_reason'] = self.data['incomplete_reason']
        return result

    def save(self, path):
        """按任务原子写入本机文件；诊断写入故障不改变生成结果。"""
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix('.tmp')
            with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as stream:
                json.dump(self.data, stream, ensure_ascii=False)
            temp.replace(path)
            return True
        except OSError:
            return False


def safe_model(value):
    """仅接受短模型标识，不让错误正文、令牌或自由文本进入诊断。"""
    return value if isinstance(value, str) and re.fullmatch(r'gpt-[A-Za-z0-9_.-]{1,80}', value) else None


def image_config(value):
    """区分请求配置、服务器生效配置与实际图片结果，筛除正文与图像字节。"""
    if not isinstance(value, dict):
        return {}
    result = {}
    if safe_model(value.get('model')):
        result['model'] = value['model']
    for key, choices in (('quality', {'auto', 'low', 'medium', 'high', 'xhigh', 'max'}),
                         ('background', {'auto', 'opaque', 'transparent'})):
        if isinstance(value.get(key), str) and value[key] in choices:
            result[key] = value[key]
    size = value.get('size')
    if isinstance(size, str) and (size == 'auto' or re.fullmatch(r'\d{1,5}x\d{1,5}', size)):
        result['size'] = size
    return result


class BackendTrace:
    """记录后端是否接受请求及流中失败阶段，忽略图像、提示词和错误原文。"""
    def __init__(self):
        self.data = {'attempts': 0, 'accepted': False, 'events': {}}
        self.diagnostic = BackendDiagnostic()

    def begin_images(self, payload, route, request_bytes):
        """原生图片请求独立记录，不把图片型号标成文字驱动，不保存参考图正文。"""
        self.begin({})
        self.data.update(native_route=route, request_bytes=request_bytes,
                         reference_count=len(payload.get('images', [])),
                         requested_image=image_config(payload), model_reported=False)

    def images_response(self, response):
        """只采用服务端返回字段；缺少型号时不拿请求型号填充生效型号。"""
        self.data['rendered_image'] = image_config(response)
        model = safe_model(response.get('model'))
        self.data['model_reported'] = bool(model)
        if model:
            self.data['effective_image'] = {'model': model}
        self.diagnostic.data['reported_image'] = self.data['rendered_image']
        self.images_error(response)

    def images_error(self, response):
        """上游错误只保存脱敏说明，公共状态仅接收白名单错误码。"""
        if not isinstance(response, dict):
            return
        error = response.get('error')
        self.diagnostic.error(error)
        if isinstance(error, dict) and error.get('code') in ERROR_CODES:
            self.data['error_code'] = error['code']

    def begin(self, payload):
        self.data['attempts'] += 1
        # 后端已有的多次尝试分别留存；公开摘要只描述当前尝试，避免旧拒绝污染结果。
        previous = self.diagnostic.data
        history = previous.get('previous_attempts', [])[-2:]
        if self.data['attempts'] > 1:
            history = history + [{key: value for key, value in previous.items() if key != 'previous_attempts'}]
        self.diagnostic = BackendDiagnostic()
        self.diagnostic.data['attempt'] = self.data['attempts']
        if history:
            self.diagnostic.data['previous_attempts'] = history
        for key in ('exception_type', 'http_status', 'error_code', 'response_status', 'incomplete_reason',
                    'image_call_statuses', 'image_result_present', 'assistant_text_present', 'refusal_present',
                    'rendered_image'):
            self.data.pop(key, None)
        self.data['request_bytes'] = len(json.dumps(payload).encode('utf-8'))
        self.data['reference_count'] = sum(part.get('type') == 'input_image' for value in payload.get('input', [])
                                            for part in value.get('content', []) if isinstance(part, dict))
        if safe_model(payload.get('model')):
            self.data['requested_driver_model'] = payload['model']
        effort = (payload.get('reasoning') or {}).get('effort')
        if isinstance(effort, str) and effort in {'low', 'medium', 'high', 'xhigh', 'max'}:
            self.data['requested_reasoning'] = effort
        for tool in payload.get('tools', []):
            if isinstance(tool, dict) and tool.get('type') == 'image_generation':
                self.data['requested_image'] = image_config(tool)

    def event(self, event):
        if not isinstance(event, dict):
            return
        self.diagnostic.event(event)
        self.data.update(self.diagnostic.summary())
        for error in self.diagnostic.data['errors']:
            if error.get('code') in ERROR_CODES:
                self.data['error_code'] = error['code']
        name = event.get('type')
        if isinstance(name, str) and name in EVENTS:
            self.data['events'][name] = min(1000, self.data['events'].get(name, 0) + 1)
            if name == 'response.created':
                self.data['accepted'] = True
        error = (event.get('response') or {}).get('error') if isinstance(event.get('response'), dict) else event.get('error')
        code = error.get('code') if isinstance(error, dict) else event.get('code')
        if isinstance(code, str) and code in ERROR_CODES:
            self.data['error_code'] = code
        response = event.get('response')
        if isinstance(response, dict):
            if safe_model(response.get('model')):
                self.data['returned_driver_model'] = response['model']
            for tool in response.get('tools') or []:
                if isinstance(tool, dict) and tool.get('type') == 'image_generation':
                    self.data['effective_image'] = image_config(tool)
        item = event.get('item')
        if name == 'response.output_item.done' and isinstance(item, dict) and item.get('type') == 'image_generation_call':
            self.data['rendered_image'] = image_config(item)

    def failure(self, error):
        name = type(error).__name__
        self.data['exception_type'] = name if name in EXCEPTIONS else 'OtherError'
        self.diagnostic.data['exception_type'] = self.data['exception_type']
        self.diagnostic.data['exception_message'] = diagnostic_text(str(error))
        status = getattr(error, 'status', None) or getattr(error, 'code', None)
        if isinstance(status, int) and 400 <= status <= 599:
            self.data['http_status'] = status

    def line(self):
        return PREFIX + json.dumps(self.data, separators=(',', ':'))


def read_trace(raw):
    """重新核对白名单，不信任子进程提供的额外字段或任意字符串。"""
    for line in reversed(raw.decode('utf-8', errors='replace').splitlines()):
        if not line.startswith(PREFIX):
            continue
        try:
            value = json.loads(line[len(PREFIX):])
            result = {key: value[key] for key in ('attempts', 'request_bytes', 'reference_count')
                      if type(value.get(key)) is int and 0 <= value[key] <= 100_000_000}
            if type(value.get('accepted')) is bool:
                result['accepted'] = value['accepted']
            for key in ('image_result_present', 'assistant_text_present', 'refusal_present', 'diagnostic_written', 'model_reported'):
                if type(value.get(key)) is bool:
                    result[key] = value[key]
            if isinstance(value.get('response_status'), str) and value['response_status'] in STATUSES:
                result['response_status'] = value['response_status']
            if isinstance(value.get('incomplete_reason'), str) and value['incomplete_reason'] in INCOMPLETE_REASONS:
                result['incomplete_reason'] = value['incomplete_reason']
            statuses = value.get('image_call_statuses')
            if isinstance(statuses, list):
                result['image_call_statuses'] = [status for status in statuses[:16]
                                               if isinstance(status, str) and status in STATUSES]
            result['events'] = {key: count for key, count in value.get('events', {}).items()
                                if key in EVENTS and type(count) is int and 0 <= count <= 1000}
            if value.get('exception_type') in EXCEPTIONS | {'OtherError'}:
                result['exception_type'] = value['exception_type']
            if value.get('error_code') in ERROR_CODES:
                result['error_code'] = value['error_code']
            if type(value.get('http_status')) is int and 200 <= value['http_status'] <= 599:
                result['http_status'] = value['http_status']
            if value.get('native_route') in ('images/generations', 'images/edits'):
                result['native_route'] = value['native_route']
            for key in ('requested_driver_model', 'returned_driver_model'):
                if safe_model(value.get(key)):
                    result[key] = value[key]
            if value.get('requested_reasoning') in ('low', 'medium', 'high', 'xhigh', 'max'):
                result['requested_reasoning'] = value['requested_reasoning']
            for key in ('requested_image', 'effective_image', 'rendered_image'):
                if isinstance(value.get(key), dict):
                    result[key] = image_config(value[key])
            return result
        except (ValueError, TypeError, AttributeError):
            continue
    return None
