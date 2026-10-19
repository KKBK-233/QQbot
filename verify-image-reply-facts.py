"""回放实际错误回复的上下文，仅调用文字模型，不执行工具或发送群消息。"""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import sys

ROOT = Path('/home/qqbot/QQbot/MaiBot')
PLUGIN = (Path('/mnt/d/code/web/QQbot/image-tool/maibot-plugin')
          if '--source' in sys.argv else ROOT / 'plugins/qqbot-image')
SESSION = 'eea9ec46d0880b86983de2e4235f4bca'
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('image_reply_facts_probe', PLUGIN / 'reply_facts.py')
facts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(facts)


async def main():
    """读取两种故障现场，确认新增事实能克服错误历史；禁止执行模型返回动作。"""
    from src.services import llm_service
    from src.plugin_runtime.hook_payloads import deserialize_prompt_items
    from src.llm_models.payload_content.context_item import CONTEXT_ITEM_SCHEMA_VERSION
    from src.llm_models.payload_content.context_protocol import ContextProtocolMode
    folder = ROOT / 'logs/maisaka_prompt/replyer/qq_group_1022382380'
    cases = {}
    for path in sorted(folder.glob('*.json')):
        if not '1791126400000' <= path.stem <= '1791126603000':
            continue
        doc = json.loads(path.read_text())
        output = json.dumps(doc.get('output_items'), ensure_ascii=False)
        if '没接生图能力' in output:
            cases['生图能力'] = (path, doc)
        if '黑长直，紫瞳,嘴角' in output:
            cases['自身外观'] = (path, doc)
    assert len(cases) == 2, '未找到两种实际失败快照'
    result = []
    for name, (path, doc) in cases.items():
        adjusted = facts.factual_context({'session_id': SESSION, 'items': doc['request_items']},
                                         ROOT / 'data/plugins/local.qqbot-image/delivery')
        items = deserialize_prompt_items(adjusted['items'], item_schema_version=CONTEXT_ITEM_SCHEMA_VERSION,
                                         mode=ContextProtocolMode.REQUEST_CONTEXT)
        async def factory(*args, **kwargs):
            return items
        generated = await llm_service.generate(llm_service.LLMServiceRequest(
            task_name='replyer', request_type='plugin.image_reply_facts_probe',
            context_factory=factory, max_tokens=500))
        assert generated.success, '文字模型回放失败'
        text = generated.completion.response
        assert not generated.completion.tool_calls, '回放不应请求执行工具'
        if name == '自身外观':
            assert any(s in text for s in ('银灰', '银发', '灰发')), text
        else:
            assert not any(s in text for s in ('没接生图能力', '没有生图工具', '没这能力', '没法生图')), text
            assert not re.search(r'没有.{0,8}生图(?:工具|能力)|(?:接不了|不能|无法|没法)生图', text), text
            jobs = facts.recent_jobs(SESSION)
            if jobs and all(job['state'] in ('failed', 'uncertain') for job in jobs):
                assert not any(s in text for s in ('生成中', '排队中', '正在生成')), text
                assert not re.search(r'再.{0,8}(?:提交|生成|重试)|重新.{0,6}(?:提交|生成)', text), text
        result.append({'case': name, 'snapshot': path.name, 'reply': text})
    return result


with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
    results = asyncio.run(main())
print(json.dumps({'results': results, 'generation_requests': 0, 'qq_messages_sent': 0}, ensure_ascii=False))
