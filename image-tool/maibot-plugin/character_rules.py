"""用户确认的角色参考与外观规则，不从历史错误回复学习角色身份。"""

import json
import hashlib
from pathlib import Path
import re

# 只对用户确认过参考图的本群启用，不隐式跨群读取图片。
DS_REFERENCES = {'eea9ec46d0880b86983de2e4235f4bca': '-1461680975'}
DS_DETAIL_REFERENCES = {'eea9ec46d0880b86983de2e4235f4bca': '-293459031'}
DS_CHIBI_REFERENCES = {'eea9ec46d0880b86983de2e4235f4bca': '-1178010614'}
DS_CARD = json.loads(Path(__file__).with_name('ds_character.json').read_text(encoding='utf-8'))
AI_CARDS = json.loads(Path(__file__).with_name('ai_characters.json').read_text(encoding='utf-8'))
DS_NAME = re.compile(r'deepseek\s*[娘酱]|ds\s*[娘酱]|鲸鱼娘', re.IGNORECASE)
CHARACTER_NAMES = {
    'deepseek': DS_NAME,
    'claude': re.compile(r'(?:claude|克劳德|克洛德)\s*[娘酱]', re.IGNORECASE),
    'gpt': re.compile(r'(?:chat\s*gpt|gpt)\s*(?:龙)?[娘酱]|GPT小龙娘', re.IGNORECASE),
    'elaina': re.compile(r'伊蕾娜|伊雷娜|イレイナ|(?<![A-Za-z])Elaina(?![A-Za-z])', re.IGNORECASE),
}

# 清除规划器编造的身份描述；场景、动作与背景颜色仍保留。
_COLOR = r'(?:深蓝|浅蓝|亮蓝|蓝紫|紫|蓝|银灰|银|灰|金|黄|粉|红|橙铜|橙|绿|白|黑|棕|褐|深|浅)(?:色)?'
_APPEARANCE = re.compile(
    _COLOR + r'(?:的)?(?:中长|长|短)?(?:头发|发|眼睛|眼|瞳)|'
    r'(?:中长|长|短)(?:头发|发)|(?:双马尾|单马尾|麻花辫)|'
    r'(?:二|两|三|四|[0-9]+(?:\.[0-9]+)?)\s*头身|'
    r'(?:猫耳|兔耳|猫娘|鱼尾替代双腿|米色长袍)|'
    r'\b(?:(?:long|short)\s+)?(?:purple|blue|black|blond|blonde|silver|pink|red|brown|white)(?:\s+(?:long|short))?\s+(?:hair|haired|eyes)\b|'
    r'\b(?:long|short)\s+hair\b|\bcat[- ]?ears?\b', re.IGNORECASE)


def scene_prompt(prompt):
    """自由提示词只保留画面要求，外观身份统一来自注册卡和核实后的用户修改。"""
    cleaned = _APPEARANCE.sub('', prompt)
    return re.sub(r'[,，;；、\s]{2,}', '，', cleaned).strip(' ,，;；、')


def verified_appearance_change(message, session, quote):
    """外观覆盖必须是同群真人消息中的原文，不能拿模型自己的错误回复作授权。"""
    if not quote or len(quote) > 300 or not message or message.get('session_id') != session:
        raise ValueError('修改角色外观需提供本群用户的明确原话和要求消息编号')
    author = str(message.get('message_info', {}).get('user_info', {}).get('user_id') or '')
    if not author or author == '3976405558':
        raise ValueError('不能用机器人自己的旧回复覆盖角色外观')
    parts = message.get('raw_message', [])
    texts = []
    for part in parts:
        if isinstance(part, dict) and part.get('type') == 'text':
            data = part.get('data', '')
            texts.append(data.get('text', '') if isinstance(data, dict) else str(data))
    text = ''.join(texts)
    if quote not in text:
        raise ValueError('外观修改内容必须逐字来自指定用户消息，不接受模型补写')
    if not re.search(r'发|染|眼|瞳|服|装|裙|衣|耳|尾|辫|猫|鲸|头身|hair|eyes|outfit|dress|ears|tail', quote, re.IGNORECASE):
        raise ValueError('提供的原话不包含明确的角色外观修改')
    return quote


def requested_characters(prompt, character=''):
    """明确角色名才启用注册外观；同框请求按出现顺序分别附图，不把普通龙当GPT娘。"""
    if character and character not in CHARACTER_NAMES:
        raise ValueError('角色参考只支持deepseek、claude、gpt、elaina或留空')
    matches = sorted((match.start(), role) for role, pattern in CHARACTER_NAMES.items()
                     if (match := pattern.search(prompt)))
    roles = [role for _, role in matches]
    # 明确点名的角色与参数冲突时停止提交，避免错误角色卡覆盖真人要求。
    if roles and character and character not in roles:
        raise ValueError('character参数与提示词中的明确角色名冲突，请按用户指定角色填写，不得混用参考图')
    if character and character not in roles:
        roles.insert(0, character)
    return roles


def character_requested(prompt, character=''):
    return bool(requested_characters(prompt, character))


def registered_reference(character, form, inbox):
    """管理员保存的公共同人图按固定文件名和摘要读取；模型不能输入路径或URL。"""
    if character not in AI_CARDS or form not in ('standard', 'chibi'):
        raise ValueError('公共角色参考或形态无效')
    reference = AI_CARDS[character]['profiles'][form]['reference']
    name = reference['file']
    if not re.fullmatch(r'[a-z-]+\.(?:png|jpg|webp)', name):
        raise ValueError('公共角色参考文件名无效')
    data = (Path(inbox) / name).read_bytes()
    if hashlib.sha256(data).hexdigest() != reference['sha256']:
        raise ValueError('公共角色参考已变化，需管理员重新确认')
    return name


def character_form(prompt, form='auto'):
    """明确形态优先，自动识别Q版和表情包，避免把普通比例原图当作Q版标准。"""
    if form not in ('auto', 'standard', 'chibi'):
        raise ValueError('角色形态仅支持auto、standard或chibi')
    if form != 'auto':
        return form
    if re.search(r'非\s*q\s*版|不(?:要|用)\s*q\s*版', prompt, re.IGNORECASE):
        return 'standard'
    if re.search(r'q\s*版|chibi|二头身|2\.5头身', prompt, re.IGNORECASE):
        return 'chibi'
    if re.search(r'少女版|少女版本|普通版|标准版|普通比例|正常比例', prompt):
        return 'standard'
    return 'chibi' if '表情包' in prompt else 'standard'


def character_reference(session, prompt, character='', form='auto'):
    """只使用本群已确认的原图，其他群不会读取此图。"""
    references = DS_CHIBI_REFERENCES if character_form(prompt, form) == 'chibi' else DS_REFERENCES
    return references.get(session) if 'deepseek' in requested_characters(prompt, character) else None


def character_references(session, prompt, character='', form='auto'):
    """少女版同时附三视图和立绘；仅注册来源群可取图，Q版沿用原参考。"""
    primary = character_reference(session, prompt, character, form)
    if not primary:
        return []
    detail = DS_DETAIL_REFERENCES.get(session) if character_form(prompt, form) == 'standard' else None
    return [primary, detail] if detail else [primary]


def identity_anchor(prompt, reference_index, form, learned, learned_reference, reference_count, character):
    """逐个登记角色身份和附图位置，同框时不让某一张图覆盖另一角色。"""
    card = DS_CARD if character == 'deepseek' else AI_CARDS[character]
    name = card['name']
    indexes = '、'.join(str(reference_index + offset) for offset in range(reference_count)) if reference_index else ''
    reference = ('【固定角色参考：' + name + '】第' + indexes + '张附图只用于确定' + name + '的脸、发型、配色、服装及身体结构。'
                 '保持角色身份；若另有原图，其构图只按本次要求修改。\n') if reference_index else '【注册角色卡】本群未配置固定原图，使用以下注册外观，不声称已附参考图。\n'
    profile = character_form(prompt, form)
    identity = name + '的注册外观：' + '；'.join(card['community_features'] + card['profiles'][profile]['features']) + '。'
    label = '【本群已确认的动态修正】' if character == 'deepseek' else '【本群已确认的动态修正：' + name + '】'
    dynamic = ('\n' + label + '；'.join(learned) + '。这些条目只覆盖本角色点名的外观项，优先于旧角色卡和旧图，其余身份保持。') if learned else ''
    if learned_reference:
        dynamic += '\n附图是聊天中新确认的角色标准；与旧角色卡不一致的外观以这张新图为准，不继承旧版本冲突描述。'
    return reference + identity + dynamic


def anchored_prompt(prompt, reference_index, appearance_change='', form='auto', learned=(), learned_reference=False, has_original=False, reference_count=1, character='deepseek', anchors=None):
    """角色图确定身份，原图确定修改对象；将自由要求与固定身份分开说明。"""
    anchors = anchors or [{'character': character, 'reference_index': reference_index,
                          'learned': learned, 'learned_reference': learned_reference, 'reference_count': reference_count}]
    identity = '\n'.join(identity_anchor(prompt, item['reference_index'], form, item.get('learned', ()),
                                         item.get('learned_reference', False), item.get('reference_count', 1),
                                         item['character']) for item in anchors)
    has_learned = any(item.get('learned') or item.get('learned_reference') for item in anchors)
    locked = '渐变蓝色披散长卷发和蓝瞳' if len(anchors) == 1 and anchors[0]['character'] == 'deepseek' else '各自外观'
    # 新画自行补场景，编辑按用户要求保留或更换实景；透明效果需要明确要求。
    background = ''
    opaque_requested = bool(re.search(r'不透明|非透明|不要透明|不要抠图|opaque|(?:no|not)\s+transparent', prompt, re.IGNORECASE))
    transparent_requested = not opaque_requested and bool(re.search(
        r'透明背景|背景透明|透明底|保留(?:原图)?透明度|透明度不变|抠图|去(?:掉|除)?背景|transparent|remove\s+(?:the\s+)?background', prompt, re.IGNORECASE))
    if not transparent_requested:
        if has_original:
            background = '\n【编辑背景】原图已有实景背景且本次未要求更换时保留；按本次背景要求绘制，全画布保持不透明背景并绘制到四边四角；透明空缺用与场景一致的内容补完整，不做渐隐透明边缘。'
        else:
            background = '\n【新画背景】按本次要求绘制背景；未指定时按主体、动作和氛围自行设计合适的具体场景，完整不透明背景铺满四边四角，不默认纯白留空；角色参考只确定身份。用户明确指定纯色或白底时照办。'
    return (identity +
            '\n【本次画面要求】' + scene_prompt(prompt) +
            ('\n【核实后的用户外观修改】' + appearance_change + '。只覆盖用户点名的对应外观项，其他身份仍保持。' if appearance_change else
             '\n只允许本群已确认的动态修正改变点名外观，其余身份保持。' if has_learned else
             '\n本次未授权更改发色、发型或角色身份，严格保留注册的' + locked + '。') +
            background + '\n画面要求里的模型补充不得重新定义角色身份。仅取角色图的身份特征，不继承其背景或坐姿。'
            '替换角色时耳部和发饰按各自参考，不把待替换原图的耳机、麦克风或其他角色配饰学成角色特征；用户明确要求保留配饰时照办。'
            '生成一张图，不自动增加文字或水印。')
