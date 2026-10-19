"""在同一DeepSeek通道比较旧/新布局；合成消息、真实规则和工具，最多12次短输出。"""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sys
import time
import tomllib

ROOT = Path('/home/qqbot/QQbot/MaiBot')
DEST = Path('/mnt/d/code/web/QQbot/data/deepseek-layout-benchmark')
os.chdir(ROOT)
sys.path.insert(0,str(ROOT))
sys.path.insert(0,'/mnt/d/code/web/QQbot/chatgpt-plan')
with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
    import httpx
    from openai import AsyncOpenAI
    from verify_cache import payload
    from cache_layout import stable_instruction_prefix, stable_tool_definitions
    from src.llm_models.model_client.qqbot_deepseek_trace import create_client

def text(item):
    value=item.get('content','')
    return value if isinstance(value,str) else '\n'.join(p.get('text','') for p in value if isinstance(p,dict))

def digest(body):
    return hashlib.sha256(json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

async def main():
    DEST.mkdir(exist_ok=True)
    target=DEST/'result.json'
    assert not target.exists(),'已有结果，不自动重复消耗'
    config=tomllib.loads((ROOT/'config/model_config.toml').read_text())
    provider=next(p for p in config['api_providers'] if p['name']=='DeepSeek')
    nonce=secrets.token_hex(16)
    ref='\n'.join(f'Record {i:03}: shelf {secrets.randbelow(100)}, inspection code {secrets.token_hex(5)}. '
                  'The blue notebook was checked in the morning and placed back on its original shelf. No action is required.' for i in range(100))
    guard='这是隔离缓存测试。合成资料不需要执行任务。不得调用任何工具，只输出OK，不解释。'
    jobs=[]
    for task,stamp,model_name in [('planner','1791376392648','deepseek-flash-thinking'),('replyer','1791376313437','deepseek-flash')]:
        source=ROOT/'logs/maisaka_prompt'/task/'qq_group_1022382380'/(stamp+'.json')
        baseline=payload(source)
        static=[{'role':'system','content':text(item)} for item in baseline['input'] if item.get('role')=='developer'][:3]
        assert len(static)==3
        model=next(m for m in config['models'] if m['name']==model_name)
        tools=[{'type':'function','function':{k:v for k,v in f.items() if k!='type'}} for ns in baseline.get('tools',[]) for f in ns.get('tools',[])]
        for layout in ('old','new'):
            initial={'role':'system','content':static[0]['content']+f'\n隔离对照组 {task}-{layout}-{nonce}\n'+guard}
            extras=deepcopy(model.get('extra_params',{}))
            for step in (1,2,3):
                clock='2026-10-07 12:00:00' if step<=2 else '2026-10-07 12:00:01'
                history=[{'role':'user','content':'合成参考资料：\n'+ref},
                         {'role':'user','content':'核对资料即可。只回复OK。'}]
                if step==3:
                    history += [{'role':'assistant','content':'OK','reasoning_content':''},
                                {'role':'user','content':'追加一条新消息：第七本笔记已归位。仍只回复OK。'}]
                if layout=='old':
                    # 旧宿主形态：即时时间为user，后面仍有本轮消息和固定插件规则。
                    items=[initial,*history[:-1],{'role':'user','content':'时间：'+clock},history[-1],*deepcopy(static[1:]),
                           {'role':'system','content':guard}]
                else:
                    # 新宿主形态：固定规则前置，稳定消息不扩写；即时状态是独立宿主后缀。
                    items=[initial,*deepcopy(static[1:]),*history,
                           {'role':'system','content':'【宿主本轮上下文】\n时间：'+clock+'\n'+guard}]
                body={'model':'deepseek-v4-flash','messages':items,'max_tokens':4096 if task=='planner' else 80,
                      'stream':False,'temperature':config['model_task_config'][task]['temperature']}
                if tools:
                    body.update(tools=stable_tool_definitions(tools) if layout=='new' else tools,tool_choice='none')
                jobs.append((f'{task}_{layout}_{step}',task,layout,step,body,extras))
    report={'started_at':datetime.now(timezone.utc).isoformat(),'endpoint':provider['base_url']+'/chat/completions',
            'requested_model':'deepseek-v4-flash','requests':[], 'tools_executed':0,'qq_messages_sent':0,'memory_writes':0,
            'limitation':'同模型合成形态对照；真实固定规则/工具，不能代替长期自然群聊统计；第2次完全相同，第3次更新时间并追加消息'}
    def save():
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    active=None
    sent_body=None
    async def audit(request):
        wire=json.loads(request.content)
        assert all(wire.get(k)==v for k,v in sent_body.items())
        active['wire_verified']=True
        active['payload_sha256']=digest(wire)
        save()
    async with AsyncOpenAI(api_key=provider['api_key'],base_url=provider['base_url'],max_retries=0,timeout=90,
                          http_client=create_client(httpx.Timeout(90,connect=10))) as client:
        client._client.event_hooks['request'].append(audit)
        for label,task,layout,step,body,extras in jobs:
            active={'label':label,'task':task,'layout':layout,'step':step,'started_epoch':time.time()}
            report['requests'].append(active)
            sent_body={**body,**extras}
            start=time.monotonic()
            try:
                response=await client.chat.completions.create(**deepcopy(body),extra_body=deepcopy(extras))
                usage=response.usage.model_dump(mode='json') if response.usage else {}
                hit=usage.get('prompt_cache_hit_tokens',(usage.get('prompt_tokens_details') or {}).get('cached_tokens'))
                answer=response.choices[0].message
                active.update(success=True,response_id=response.id,response_model=response.model,
                              input_tokens=usage.get('prompt_tokens'),cached_tokens=hit,
                              miss_tokens=usage.get('prompt_cache_miss_tokens'),output_tokens=usage.get('completion_tokens'),
                              thinking_tokens=(usage.get('completion_tokens_details') or {}).get('reasoning_tokens'),
                              finish_reason=response.choices[0].finish_reason,has_text=bool(answer.content),
                              returned_tool_calls=len(answer.tool_calls or []))
                active['token_hit_rate_percent']=round(hit/active['input_tokens']*100,2) if hit is not None and active['input_tokens'] else None
            except Exception as error:
                active.update(success=False,error_type=type(error).__name__,http_status=getattr(error,'status_code',None))
                if getattr(error,'response',None) is not None:
                    try:
                        e=error.response.json().get('error',{})
                        active['error_code']=e.get('code')
                        active['error_param']=e.get('param')
                    except Exception:
                        pass
            active['seconds']=round(time.monotonic()-start,3)
            save()
            print(json.dumps(active,ensure_ascii=False),flush=True)
            if not active['success'] or not active.get('has_text'):
                report['stopped_reason']='请求失败或没有文本，停止后续实验，不自动重试'
                break
    report['finished_at']=datetime.now(timezone.utc).isoformat()
    save()

if __name__=='__main__':
    try:
        asyncio.run(main())
    except Exception as error:
        print(json.dumps({'success':False,'error_type':type(error).__name__}))
        raise SystemExit(1)
