"""验证实际MaiCore规划/回复路由，只检查返回值，不执行工具或发送群消息。"""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path('/home/qqbot/QQbot/MaiBot')
os.chdir(ROOT)
sys.path.insert(0,str(ROOT))
with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
    from src.services import llm_service
    from src.llm_models.payload_content.context_item import ContextItemBuilder,RoleType

async def main():
    results=[]
    for task in ('planner','replyer'):
        if task=='planner':
            prompt=[{'role':'system','content':'隔离路由测试。只调用probe_status一次。不需要其他工具，不能执行实际操作。'},
                    {'role':'user','content':'检查接入状态。'},
                    {'role':'system','content':'【宿主本轮上下文】本轮只验证工具能力。'}]
            options=[{'name':'probe_status','description':'隔离模拟状态工具，返回固定OK，不触发任何外部操作。',
                      'parameters_schema':{'type':'object','properties':{},'required':[]}}]
        else:
            prompt=[{'role':'system','content':'隔离路由测试。你是伊蕾娜。按用户指定格式简短回答。'},
                    {'role':'user','content':'只回复：伊蕾娜在线'},
                    {'role':'system','content':'【宿主本轮上下文】时间：2026-10-07 12:00:00'}]
            options=None
        start=time.monotonic()
        generated=await llm_service.generate(llm_service.LLMServiceRequest(task_name=task,
            request_type='plugin.context_cache_smoke',prompt=prompt,tool_options=options,max_tokens=4096 if task=='planner' else 80))
        assert generated.success,'实际服务路由未完成'
        output=generated.completion
        calls=output.tool_calls or []
        trace=output.generation_trace
        assert trace.provider=='DeepSeek' and trace.model=='deepseek-v4-flash','模型路由不符合配置'
        if task=='planner':
            assert len(calls)==1 and calls[0].func_name=='probe_status','规划器没有返回预期的原生工具调用'
        else:
            assert output.response and '伊蕾娜在线' in output.response,'回复器没有遵守指定短格式'
        results.append({'task':task,'provider':trace.provider,'model':trace.model,'seconds':round(time.monotonic()-start,3),
                        'tool_calls':[c.func_name for c in calls],'text_format_passed':task=='replyer',
                        'tools_executed':0,'qq_messages_sent':0,'memory_writes':0})
    dest=Path('/mnt/d/code/web/QQbot/data/deepseek-layout-benchmark/service-verification.json')
    dest.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    return results

if __name__=='__main__':
    try:
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            result=asyncio.run(main())
    except Exception as error:
        print(json.dumps({'success':False,'error_type':type(error).__name__}))
        raise SystemExit(1)
    print(json.dumps(result,ensure_ascii=False))
