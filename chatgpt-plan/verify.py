"""用已登录账号验证模型、MaiBot 工具续接；不会调用 QQ 或写入记忆。"""

from pathlib import Path

import asyncio
import argparse
import base64
import io
import json
import os
import sys
import time

import httpx

ROOT = Path('/home/qqbot/QQbot/MaiBot')
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from src.config.model_configs import APIProvider, ModelInfo
from src.llm_models.model_client.base_client import ResponseRequest
from src.llm_models.model_client.chatgpt_plan_auth import AccountStore, RESOURCE
from src.llm_models.model_client.openai_chatgpt_plan import OpenAIChatGPTPlanClient
from src.llm_models.payload_content.context_item import (
    ContextItemBuilder, ContextItemMeta, FunctionCallItem, FunctionCallOutputItem,
)
from src.llm_models.payload_content.tool_option import ToolOption
from src.llm_models.payload_content.context_item import RoleType


async def main(effort='low', with_vision=False, model_id='gpt-6.1-sol') -> None:
    started = time.monotonic()
    store = AccountStore()
    token = await asyncio.to_thread(store.token)
    with store.locked() as data:
        account_key = data['active']
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.get(RESOURCE + '/models', headers={'Authorization': 'Bearer ' + token})
        if response.status_code != 200:
            raise RuntimeError(f'模型目录 HTTP {response.status_code}')
        catalog = response.json()
        names = [m.get('slug', m.get('id')) for m in catalog.get('models', catalog.get('data', []))]
    if model_id not in names:
        raise RuntimeError('当前订阅账号的模型目录没有目标型号')
    model = ModelInfo(name=model_id.replace('gpt-', 'chatgpt-', 1) + '-' + effort, model_identifier=model_id, api_provider='ChatGPTPlan',
                      send_temperature=False, force_stream_mode=True, visual=True)
    provider = APIProvider(name='ChatGPTPlan', base_url=RESOURCE, auth_type='none',
                           client_type='openai_chatgpt_plan', max_retry=0, timeout=120)
    client = OpenAIChatGPTPlanClient(provider)
    context = [ContextItemBuilder().set_role(RoleType.System).add_text_content(
        '你正在验证 MaiBot 的模型接入。必须先调用 list_known_people，再根据工具结果回答。不得虚构人物。').build(),
        ContextItemBuilder().set_role(RoleType.User).add_text_content('列出你认识的全部模拟群友与已知事实。').build()]
    tool = ToolOption(name='list_known_people', description='返回此测试的模拟人物资料。',
                      parameters_schema_override={'type': 'object', 'properties': {}, 'required': []})
    request = ResponseRequest(model_info=model, context_items=context, tool_options=[tool],
                              max_tokens=16384, temperature=0.7, extra_params={'reasoning': {'effort': effort}})
    if with_vision:
        # 一张合成图并入续接验证，避免额外调用真实群图或产生生图费用。
        from PIL import Image, ImageDraw
        picture = Image.new('RGB', (128, 128), 'white')
        ImageDraw.Draw(picture).rectangle((24, 24, 104, 104), fill='red')
        buffer = io.BytesIO()
        picture.save(buffer, format='PNG')
        context[-1] = ContextItemBuilder().set_role(RoleType.User).add_text_content(
            '先调用工具获取甲乙丙的事实，再简短列出三人事实和图片中央形状及颜色。').add_image_content(
                'png', base64.b64encode(buffer.getvalue()).decode()).build()
        request = request.copy_with(context_items=context)
    first = await client.get_response(request)
    calls = [item for item in first.output_items if isinstance(item, FunctionCallItem)]
    if len(calls) != 1 or calls[0].tool_call.func_name != 'list_known_people':
        raise RuntimeError('模型没有正确调用模拟工具')
    context.extend(first.output_items)
    context.append(FunctionCallOutputItem(meta=ContextItemMeta.create(logical_turn_id=request.logical_turn_id),
                                         call_id=calls[0].tool_call.call_id,
                                         output='甲喜欢蓝色；乙喜欢画画；丙讨论过音乐。', success=True,
                                         tool_name='list_known_people'))
    second = await client.get_response(request.copy_with(context_items=context))
    reply = second.content or ''
    if not all(name in reply for name in ['甲', '乙', '丙']):
        raise RuntimeError('工具续接后遗漏模拟人物')
    if with_vision and not ('红' in reply and any(word in reply for word in ['正方形', '方块', '方形'])):
        raise RuntimeError('合成图片识别验证失败')
    actual_models = [first.provider_response.get('model'), second.provider_response.get('model')]
    if not all(name == model_id or str(name).startswith(model_id + '-') for name in actual_models):
        raise RuntimeError('实际返回模型与请求不一致')
    report = {'success': True, 'model': model_id, 'reasoning_effort': effort,
              'vision_verified': with_vision, 'verified_at': time.time(),
              'account_key': account_key, 'account_catalog_contains_model': model_id in names,
              'returned_models': actual_models, 'reply': reply, 'function_continuation': True,
              'simulated_tool_outputs': True, 'qq_messages_sent': 0, 'memory_writes': 0,
              'seconds': round(time.monotonic() - started, 2)}
    path = ROOT / 'data/chatgpt-plan/verification.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({key: value for key, value in report.items() if key != 'account_key'}, ensure_ascii=False))
    await client.client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', choices=['gpt-6.1-sol', 'gpt-6-luna'], default='gpt-6.1-sol')
    parser.add_argument('--effort', choices=['low', 'medium', 'high'], default='low')
    parser.add_argument('--with-vision', action='store_true')
    args = parser.parse_args()
    asyncio.run(main(args.effort, args.with_vision, args.model))
