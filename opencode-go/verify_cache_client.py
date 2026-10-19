"""通过真实Haiku客户端验证动态时间不再破坏固定源码缓存。"""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sys
import time

ROOT = Path('/home/qqbot/QQbot/MaiBot')


async def verify():
    """仅发送三次合成编码请求，动态时间、状态及问题每轮变化。"""
    os.chdir(ROOT)
    sys.path.insert(0,str(ROOT))
    from src.config.config import config_manager
    from src.llm_models.model_client.base_client import RequestTraceContext, ResponseRequest, client_registry
    from src.llm_models.model_client import ensure_client_type_loaded
    from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType
    from src.llm_models.model_client.opencode_go_protocol import response_metadata
    from src.llm_models.model_client.opencode_go_cache_layout import RULE_PREFIX, STATUS_MARKER
    from verify_cache import source_fixture
    config=config_manager.get_model_config()
    model=next(m for m in config.models if m.name=='opencode-go-haiku-5.5')
    provider=next(p for p in config.api_providers if p.name==model.api_provider)
    ensure_client_type_loaded(provider.client_type)
    client=client_registry.get_client_class_instance(provider)
    run=secrets.token_hex(8)
    fixed=source_fixture(run,'client-default-layout')
    rows=[]
    hashes=[]
    for index in range(3):
        def item(role,text):
            return ContextItemBuilder().set_role(role).add_text_content(text).build()
        items=[item(RoleType.System,fixed),
               item(RoleType.System,f'【宿主本轮上下文】 当前时间：2026-10-07 12:00:0{index}'),
               item(RoleType.System,RULE_PREFIX+'合成编码兼容场景；保持只回复CACHE_PROBE_OK。'+STATUS_MARKER+str(index)),
               item(RoleType.User,f'检查first_{index:03d}的空序列处理，仅按规则回复。')]
        request=ResponseRequest(model_info=model,context_items=items,max_tokens=64,
             trace_context=RequestTraceContext(session_id='cache-layout-'+run,task_name='cache_probe',request_type='manual.coding_cache_layout'))
        started=time.monotonic()
        response=await client.get_response(request)
        usage=response_metadata(response.provider_response)['usage']
        row={'round':index+1,'elapsed_seconds':round(time.monotonic()-started,2),**usage,
             'total_input_tokens':response.usage.prompt_tokens,
             'token_hit_rate_percent':round(response.usage.prompt_cache_hit_tokens/response.usage.prompt_tokens*100,2)}
        rows.append(row)
        prefix=[]
        for block in response.request_wire_payload['system']:
            prefix.append(block)
            if block.get('cache_control'):break
        hashes.append(hashlib.sha256(json.dumps(prefix,ensure_ascii=False,sort_keys=True).encode()).hexdigest())
    success=len(set(hashes))==1 and rows[1]['cache_read_input_tokens']>0 and rows[2]['cache_read_input_tokens']>0
    result={'success':success,'model':'claude-haiku-5-5','rows':rows,'prefix_stable':len(set(hashes))==1,
            'tools_executed':0,'qq_messages_sent':0,'memory_writes':0}
    folder=ROOT/'data/opencode-go';folder.mkdir(exist_ok=True,mode=0o700)
    path=folder/'cache-layout-verification.json'
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as output:json.dump(result,output,ensure_ascii=False,indent=2)
    path.chmod(0o600)
    return result


if __name__=='__main__':
    try:
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            result=asyncio.run(verify())
    except Exception as error:
        print(json.dumps({'success':False,'error_type':type(error).__name__}))
        raise SystemExit(1)
    print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(0 if result['success'] else 1)
