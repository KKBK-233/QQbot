"""为任务识图插件接入同群历史与转发媒体，不更改主程序或长期记忆。"""

import argparse
from pathlib import Path
import shutil


def repair(folder):
    """放开原图编号与任务编号的绑定，保留会话核对和有限媒体遍历。"""
    helper = Path(__file__).resolve().parent / 'image-tool/maibot-plugin/message_media.py'
    shutil.copy2(helper, folder / 'message_media.py')
    path = folder / 'task_flow.py'
    text = path.read_text(encoding='utf-8')
    if 'from .message_media import' not in text:
        text = text.replace('from PIL import Image, ImageOps', 'from PIL import Image, ImageOps\ntry:\n    from .message_media import read_images, MediaUnavailable\nexcept ImportError:\n    from message_media import read_images, MediaUnavailable', 1)
    start = text.index('        task = self.pending.get(session)', text.index('    async def analyze('))
    end = text.index('            if not images:', start)
    text = text[:start] + '''        task = self.pending.get(session)
        # 明确指定历史消息可独立识图，不要求图片编号等于当前文字任务编号。
        if not session or not message_id:
            return {'success': False, 'content': '缺少当前会话或图片消息编号。'}
        if task is None:
            task = PendingTask(message_id, '分析图片中的可见角色特征；区分各张图片，不编造细节。', time.time(), True)
        if task.evidence is not None and task.evidence.get('source_request_id', task.message_id) == message_id:
            return task.evidence
        try:
            raw, sources = await read_images(ctx, session, message_id, max_images=2, history=True)
            images = [await asyncio.to_thread(prepare_image, encoded) for encoded in raw]
''' + text[end:]
    text = text.replace("result = {'success': True, 'content': response[:2400], 'image_count': len(images)}",
        "result = {'success': True, 'content': response[:2400], 'image_count': len(images), 'source_message_ids': sources}")
    marker = '        except (ValueError, OSError, RuntimeError, asyncio.TimeoutError) as exc:'
    if 'except MediaUnavailable' not in text:
        text = text.replace(marker, "        except MediaUnavailable as exc:\n            result = {'success': False, 'content': str(exc)}\n" + marker, 1)
    text = text.replace('        task.evidence = result\n        return result', "        result['source_request_id'] = message_id\n        task.evidence = result\n        return result", 1)
    # 上面的替换操作可重复执行，旧块按边界替换，不重复追加来源字段。
    text = text.replace("        result['source_request_id'] = message_id\n        result['source_request_id'] = message_id\n", "        result['source_request_id'] = message_id\n")
    old = "if task.visual and task.evidence is None and 'analyze_task_image' in available:"
    new = "if task.visual and task.evidence is None and 'analyze_task_image' in available and not any(c.get('func_name') == 'analyze_task_image' for c in calls):"
    text = text.replace(old, new)
    path.write_text(text, encoding='utf-8')
    plugin = folder / 'plugin.py'
    value = plugin.read_text(encoding='utf-8').replace('读取当前明确任务附带或引用的图片，提取角色特征、反推绘图提示词；结果交给回复器回答。',
        '读取本群指定历史消息、附图、引用或合并转发里的图片，提取特征或反推提示词。message_id可填写之前图片消息的编号，不必等于当前任务编号；多张候选不明确时先确认。最多分析两张，结果不能当作已写入长期记忆。')
    value = value.replace('当前任务消息编号', '本群图片/转发消息编号，或附带图片引用的任务编号')
    plugin.write_text(value, encoding='utf-8')
    manifest = folder / '_manifest.json'
    import json
    config = json.loads(manifest.read_text(encoding='utf-8'))
    if 'message.get_by_time_in_chat' not in config['capabilities']:
        config['capabilities'].append('message.get_by_time_in_chat')
    manifest.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    print('同群历史及有限转发识图已接入；未调用模型，未写入长期记忆。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', default='/home/qqbot/QQbot/MaiBot/plugins/qqbot-task-assist')
    repair(Path(parser.parse_args().folder))
