"""修正独立任务插件的改图路由，不修改聊天人设或生成图片。"""

import argparse
from pathlib import Path


def repair(folder):
    """改图交给单入口编辑工具，识图和提示词反推保留原流程。"""
    path = folder / 'task_flow.py'
    source = path.read_text(encoding='utf-8')
    if 'IMAGE_EDIT_WORDS =' not in source:
        marker = "PRIORITY = "
        assert marker in source
        source = source.replace(marker,
            "IMAGE_EDIT_WORDS = re.compile(r'图生图|(?:改|修改|编辑|调整|重画|重绘).{0,8}(?:图|图片|这张|那张)|(?:图|图片|这张|那张).{0,8}(?:改|修改|编辑|调整)|(?:原图|原基础|这张|那张).{0,12}(?:换|变|加|删|去掉)|(?:背景|头发|眼睛|衣服|颜色).{0,8}(?:改成|换成|变成)')\n"
            "IMAGE_EDIT_STYLE = '用户在要求修改图片，使用chatgpt_image的edit；原图由编辑工具直接传给后端，不以聊天模型的识图成功为前提。先发图片再发文字要求时，message_id应填写上下文中明确对应的图片消息编号；修改你生成的图可用对应成功任务的parent_id。目标图片或具体修改内容不明确时简短确认，不猜选别人的图片，不以文字描述替代原图重新生成。'\n" + marker, 1)
    old = 'bool(IMAGE_WORDS.search(body)), is_task=bool(TASK_WORDS.search(body)))'
    new = 'bool(IMAGE_WORDS.search(body)) and not bool(IMAGE_EDIT_WORDS.search(body)), is_task=bool(TASK_WORDS.search(body)))'
    assert old in source or new in source
    source = source.replace(old, new)
    old = "        modified['items'] = list(kwargs['items']) + [text_item('SystemMessageItem', PRIORITY),"
    new = "        routing = PRIORITY + ('\\n' + IMAGE_EDIT_STYLE if IMAGE_EDIT_WORDS.search(task.text) else '')\n        modified['items'] = list(kwargs['items']) + [text_item('SystemMessageItem', routing),"
    assert old in source or new in source
    source = source.replace(old, new)
    path.write_text(source, encoding='utf-8')
    print('改图路由已修正：不强制先识图，编辑工具使用明确原图。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', default='/home/qqbot/QQbot/MaiBot/plugins/qqbot-task-assist')
    repair(Path(parser.parse_args().folder))
