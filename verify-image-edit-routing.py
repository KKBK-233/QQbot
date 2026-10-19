"""回放当前未执行的改图请求，读取原图并验证规划；不生图、不发送群消息。"""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import os
import sys
from types import SimpleNamespace

root = Path('/home/qqbot/QQbot/MaiBot')
source = Path('/mnt/d/code/web/QQbot/image-tool/maibot-plugin')
os.chdir(root)
sys.path.insert(0,str(root))
spec=importlib.util.spec_from_file_location('edit_route_facts',source / 'reply_facts.py')
facts=importlib.util.module_from_spec(spec)
spec.loader.exec_module(facts)
media_spec=importlib.util.spec_from_file_location('edit_route_media',source / 'message_media.py')
media=importlib.util.module_from_spec(media_spec)
media_spec.loader.exec_module(media)


async def main():
    """原图只读验证，模型工具调用仅检查参数，绝不执行。"""
    from src.services import message_service, llm_service
    from src.plugin_runtime.host.message_utils import PluginMessageUtils
    from src.plugin_runtime.hook_payloads import deserialize_prompt_items
    from src.llm_models.payload_content.context_item import CONTEXT_ITEM_SCHEMA_VERSION
    from src.llm_models.payload_content.context_protocol import ContextProtocolMode
    session='eea9ec46d0880b86983de2e4235f4bca'
    async def get_message(mid, chat_id='', include_binary_data=False):
        message=message_service.get_message_by_id(mid,chat_id=chat_id)
        return PluginMessageUtils._session_message_to_dict(message,include_binary_data=include_binary_data) if message else None
    ctx=SimpleNamespace(message=SimpleNamespace(get_by_id=get_message))
    images,ids=await media.read_images(ctx,session,'183026553')
    assert len(images)==1 and len(images[0])>100, '原图无法读取'
    path=root / 'logs/maisaka_prompt/planner/qq_group_1022382380/1791129338929.json'
    d=json.loads(path.read_text())
    adjusted=facts.factual_context({'session_id':session,'items':d['request_items']},root / 'data/plugins/local.qqbot-image/delivery')
    items=deserialize_prompt_items(adjusted['items'],item_schema_version=CONTEXT_ITEM_SCHEMA_VERSION,mode=ContextProtocolMode.REQUEST_CONTEXT)
    async def factory(*args,**kwargs):
        return items
    generated=await llm_service.generate(llm_service.LLMServiceRequest(task_name='planner',request_type='plugin.edit_route_probe',
        context_factory=factory,tool_options=d['tool_definitions'],max_tokens=1300))
    assert generated.success, '规划回放失败'
    calls=[{'tool':c.func_name,'args':c.args} for c in generated.completion.tool_calls or []]
    edits=[c for c in calls if c['tool']=='chatgpt_image' and c['args'].get('action')=='edit']
    assert edits and edits[0]['args'].get('message_id')=='183026553', '未按指定原图执行edit：'+json.dumps(calls,ensure_ascii=False)
    return {'original_image_readable':True,'source_message_id':ids,'planned_calls':calls,'tools_executed':0,'qq_messages_sent':0}

with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
    result=asyncio.run(main())
print(json.dumps(result,ensure_ascii=False))
