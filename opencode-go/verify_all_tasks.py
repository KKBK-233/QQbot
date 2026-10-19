"""验证全量切换的文本、记忆 JSON 和真实识图，不发 QQ、不写聊天记忆。"""
import argparse
import asyncio
import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path('/home/qqbot/QQbot/MaiBot')
MODEL = 'opencode-go-haiku-5.5-medium'


async def verify(preflight=False, deepseek=False):
    """预检覆盖后台无会话调用和图像；切换后逐项验证实际任务路由。"""
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from PIL import Image, ImageDraw
    from src.config.config import config_manager
    from src.services.llm_service import LLMServiceClient, LLMGenerationOptions, LLMImageOptions
    from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType
    config = config_manager.get_model_config()
    tasks = ['memory', 'vlm'] if preflight else [
        name for name, task in vars(config.model_task_config).items()
        if getattr(task, 'model_list', [])]
    results = []
    for task in tasks:
        models = getattr(config.model_task_config, task).model_list
        expected_model = ('deepseek-v4-flash-thinking' if task == 'planner' else
                          'deepseek-v4-flash' if task == 'replyer' else 'deepseek-flash') if deepseek else MODEL
        if not preflight and models != [expected_model]:
            raise RuntimeError(task + '尚未全量切换')
        client = LLMServiceClient(task_name=task, request_type='manual.deepseek_restore' if deepseek else 'manual.haiku_all_tasks',
            session_id='isolated-haiku-all-tasks' if task in {'planner', 'replyer'} else '')
        override = expected_model if preflight else None
        started = time.monotonic()
        if task == 'vlm':
            # 图片内容只在图像中给出，避免模型根据文字猜测答案。
            image = Image.new('RGB', (320, 160), 'white')
            draw = ImageDraw.Draw(image)
            draw.ellipse((20, 30, 120, 130), fill='red')
            draw.rectangle((200, 30, 300, 130), fill='blue')
            buf = io.BytesIO()
            image.save(buf, format='PNG')
            prompt = '这是图形识别程序的隔离测试。用中文描述图中左右两个图形的颜色和形状，只写一句话。'
            encoded = base64.b64encode(buf.getvalue()).decode()
            if preflight:
                # 识图正式接口没有模型覆盖参数，预检通过上下文接口指定待切换模型。
                items = [ContextItemBuilder().set_role(RoleType.User).add_text_content(prompt)
                         .add_image_content('png', encoded).build()]
                result = await client.generate_response_with_context(
                    context_factory=lambda _: items,
                    options=LLMGenerationOptions(model_name=override, max_tokens=1024))
            else:
                result = await client.generate_response_for_image(
                    prompt=prompt, image_base64=encoded, image_format='png',
                    options=LLMImageOptions(max_tokens=1024))
            content = result.response or ''
            if not all(word in content for word in ('红', '圆', '蓝', '方')):
                raise RuntimeError('图形识别校验失败')
        else:
            result = await client.generate_response(
                prompt='这是人物画像提取程序的隔离测试。输入记录：用户说“我喜欢猫”。只返回合法JSON对象，格式为{"fact":"我喜欢猫"}，不加代码围栏。',
                options=LLMGenerationOptions(model_name=override, max_tokens=1024))
            if json.loads(result.response or '') != {'fact': '我喜欢猫'}:
                raise RuntimeError(task + '的JSON提取校验失败')
        trace = result.generation_trace
        correct = (trace.provider == 'DeepSeek' and trace.model in {'deepseek-flash', 'deepseek-v4-flash'}) if deepseek else (
            trace.provider == 'OpenCodeGo' and trace.model == 'claude-haiku-5-5')
        if not correct:
            raise RuntimeError(task + '实际模型不一致')
        results.append({'task': task, 'configured_models': models, 'provider': trace.provider,
            'model': trace.model, 'seconds': round(time.monotonic() - started, 3), 'passed': True})
    folder = Path('/mnt/d/code/web/QQbot/data/opencode-go')
    folder.mkdir(parents=True, exist_ok=True)
    report = {'preflight': preflight, 'deepseek': deepseek, 'results': results, 'qq_messages_sent': 0, 'memory_writes': 0}
    name = ('deepseek-' if deepseek else '') + ('all-tasks-preflight.json' if preflight else 'all-tasks-verification.json')
    (folder / name).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--deepseek', action='store_true', help='核对恢复的 DSF 路由、文本和实际图片识别')
    args = parser.parse_args()
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = asyncio.run(verify(args.preflight, args.deepseek))
    except Exception as error:
        print(json.dumps({'success': False, 'error_type': type(error).__name__}))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False))
