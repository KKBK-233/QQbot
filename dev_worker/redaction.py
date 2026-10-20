"""结果展示脱敏；原始任务与事件日志仍是私有资料，不作为公开报告。"""
from pathlib import Path
import os
import re

MASK = '[credential]'
# 字段名匹配覆盖环境变量、JSON 和 HTTP 认证字段，不将 token 用量当成凭据。
_NAME = r'(?:[A-Za-z0-9]+[_-])*(?:(?:api|access|private)[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|token|secret|password|passwd|authorization|cookie|set-cookie|credential)s?'
_KEY = re.compile(r'^(?:' + _NAME + r')$', re.I)
_USAGE_KEYS = frozenset({'input_tokens','output_tokens','prompt_tokens','completion_tokens',
                       'total_tokens','reasoning_tokens','cached_tokens','cache_read_input_tokens',
                       'cache_creation_input_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens'})
_ASSIGNMENT = re.compile(
    r'(?P<label>[\"\']?\b' + _NAME + r'[\"\']?\s*[:=]\s*)'
    r'(?P<value>\[credential\]|\"(?:\\.|[^\"\\])*\"|\'(?:\\.|[^\'\\])*\'|[^\s,;&}\]]+)', re.I)
_BEARER = re.compile(r'\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+', re.I)
_HEADER = re.compile(r'^(?P<label>[ \t]*(?:authorization|proxy-authorization|cookie|set-cookie)[ \t]*:[ \t]*)[^\r\n]*', re.I | re.M)
_TOKEN = re.compile(r'\b(?:sk-[A-Za-z0-9_-]+|gh[pousr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+)')
_PRIVATE_KEY = re.compile(r'-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----.*?(?:-----END \1-----|\Z)', re.S)
_URL_USERINFO = re.compile(r'(\b[a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@', re.I)


class Redactor:
    """已知密钥按原值遮盖，未知值补充常见凭据语法；不声称覆盖任意编码。"""
    def __init__(self, secrets=()):
        self.secrets=tuple(sorted({value for value in secrets if isinstance(value,str) and value},
                                  key=len,reverse=True))

    @classmethod
    def from_env_file(cls, path: Path | None = None):
        """只读取宿主专用凭据与敏感环境字段，不打印、不回写文件。"""
        secrets=[value for key,value in os.environ.items() if _KEY.fullmatch(key)]
        if path is not None and path.is_file():
            for line in path.read_text(encoding='utf-8-sig').splitlines():
                line=line.strip()
                if line.startswith('export '): line=line[7:].lstrip()
                key,sep,value=line.partition('=')
                if not sep or not _KEY.fullmatch(key.strip()): continue
                value=value.strip()
                if value.startswith(('"', "'")):
                    quote=value[0]
                    end=value.find(quote,1)
                    value=value[1:end] if end>=1 else value[1:]
                else:
                    value=re.split(r'\s+#',value,maxsplit=1)[0].rstrip()
                if value: secrets.append(value)
        return cls(secrets)

    def text(self, value: str) -> str:
        # 必须先脱敏再截断，避免密钥跨越报告边界时留下前半段。
        value=_PRIVATE_KEY.sub(MASK,value)
        for secret in self.secrets: value=value.replace(secret,MASK)
        value=_HEADER.sub(lambda match:match['label']+MASK,value)
        value=_TOKEN.sub(MASK,value)
        value=_BEARER.sub(MASK,value)
        value=_URL_USERINFO.sub(lambda match:match[1]+MASK+'@',value)
        return _ASSIGNMENT.sub(lambda match:match['label']+MASK,value)

    def value(self, value):
        """递归处理模型可控字段，保留数值用量和普通诊断结构。"""
        if isinstance(value,str): return self.text(value)
        if isinstance(value,dict):
            return {self.text(str(key)):MASK if str(key).lower() not in _USAGE_KEYS and _KEY.fullmatch(str(key)) else self.value(item)
                    for key,item in value.items()}
        if isinstance(value,(list,tuple)): return [self.value(item) for item in value]
        return value
