"""角色动态记忆：按群和形态保存明确纠正、参考来源及可撤销的版本。"""

import hashlib
import json
from pathlib import Path
import re
import threading
import time

TOPICS = ('hair', 'eyes', 'tail', 'outfit', 'proportions', 'appearance')


def human_text(message, session):
    """核对同群来源，仅读取真人原话，不把机器人的错误自述作为新知识。"""
    if not message or message.get('session_id') != session:
        raise ValueError('记忆来源必须是本群实际消息')
    author = str(message.get('message_info', {}).get('user_info', {}).get('user_id') or '')
    if not author or author == '3976405558':
        raise ValueError('不能把机器人自己的回复自动学成角色特征')
    text = ''.join(str(p.get('data', {}).get('text', '') if isinstance(p.get('data'), dict) else p.get('data', ''))
                   for p in message.get('raw_message', []) if isinstance(p, dict) and p.get('type') == 'text')
    return author, text


def learning_evidence(message, session, quote, has_reference=False):
    """具体纠正采用用户原话；图片分析不冒充已确认事实，确认参考时直接保留原图。"""
    author, text = human_text(message, session)
    if not re.search(r'记住|记下来|以后|今后|默认|参考|特征|设定|改成|应该|应当|纠正|修正|正确', text):
        raise ValueError('需有明确的角色特征纠正或记住参考图的要求，普通闲聊不覆盖角色记忆')
    if quote:
        if len(quote) > 600 or quote not in text:
            raise ValueError('记忆特征须逐字摘录指定用户原话，不能保存模型自行补写的特征')
        if not re.search(r'发|眼|瞳|服|装|裙|衣|耳|尾|辫|头身|hair|eyes|tail|outfit', quote, re.IGNORECASE):
            raise ValueError('该原话没有具体外观特征；先明确哪里应当如何修改')
    elif not has_reference:
        raise ValueError('缺少具体特征或要记住的原图')
    return author


class CharacterMemory:
    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.RLock()

    def path(self, session, form, character='deepseek'):
        """路径只由宿主会话编号和固定形态生成，模型不能指定文件位置。"""
        if not session or form not in ('standard', 'chibi') or character not in ('deepseek', 'claude', 'gpt', 'elaina'):
            raise ValueError('缺少本群会话或角色形态无效')
        folder = self.root / hashlib.sha256(session.encode()).hexdigest()[:20]
        # DeepSeek沿用既有路径，新角色单独分目录，避免旧记忆丢失或角色互相覆盖。
        return (folder if character == 'deepseek' else folder / character) / (form + '.json')

    def read(self, session, form, character='deepseek'):
        path = self.path(session, form, character)
        if not path.exists():
            return {'session_id': session, 'character': character, 'form': form, 'revision': 0, 'facts': {}, 'reference': None, 'history': []}
        state = json.loads(path.read_text(encoding='utf-8'))
        if state.get('session_id') != session or state.get('form') != form or state.get('character', 'deepseek') != character:
            raise ValueError('角色记忆所属会话不匹配')
        state['character'] = character
        return state

    def save(self, state):
        """原子替换记忆文件，最多保留二十个旧版本供撤销。"""
        path = self.path(state['session_id'], state['form'], state.get('character', 'deepseek'))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)

    def remember(self, session, form, topic, quote, source_id, author, reference=None, character='deepseek'):
        """线程池并发调用时串行修改，避免丢失版本或互相替换临时文件。"""
        with self._lock:
            return self._remember(session, form, topic, quote, source_id, author, reference, character)

    def _remember(self, session, form, topic, quote, source_id, author, reference, character):
        if topic not in TOPICS:
            raise ValueError('记忆分类无效')
        state = self.read(session, form, character)
        fact = {'text': quote, 'source_message_id': source_id, 'author_id': author} if quote else None
        if (not fact or state['facts'].get(topic) == fact) and (not reference or state['reference'] == reference):
            return state
        snapshot = {key: state[key] for key in ('facts', 'reference')}
        state['history'] = (state['history'] + [snapshot])[-20:]
        # 同一个外观项采用最新明确纠正，旧内容保留在版本历史里而非重复叠加。
        state['facts'] = dict(state['facts'])
        if fact:
            state['facts'][topic] = fact
        if reference:
            state['reference'] = reference
        state['revision'] += 1
        state['updated_at'] = time.time()
        self.save(state)
        return state

    def undo(self, session, form, character='deepseek'):
        with self._lock:
            return self._undo(session, form, character)

    def _undo(self, session, form, character):
        state = self.read(session, form, character)
        if not state['history']:
            raise ValueError('没有可以撤销的角色记忆更新')
        old = state['history'].pop()
        state.update(old)
        state['revision'] += 1
        state['updated_at'] = time.time()
        self.save(state)
        return state

    @staticmethod
    def features(state):
        return [entry['text'] for entry in state['facts'].values()]
