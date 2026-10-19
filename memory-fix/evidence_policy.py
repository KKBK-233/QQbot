"""从真人原句生成可追溯记忆，模型只选择证据，不能决定事实正文。"""
import re

VERSION = 2
_NO_SAVE = re.compile(r'不要记|别记|不用记|只是(?:测试|示例|玩笑)|开玩笑|假设|如果|假如|比如|例如|```|\[CQ:|https?://')
_REVERSAL = re.compile(r'才怪|并不是|说错了|不是事实|不是真的|骗你的|开玩笑|只是测试')
_SELF = re.compile(r'^(?:我|本人|自己)(?:的)?(?:喜欢|不喜欢|讨厌|偏好|习惯|希望|更喜欢|不希望|想要|叫|是|住在|来自|在.{0,12}工作|对.{1,16}过敏|使用|收藏|爱好)')
_TEMP = re.compile(r'今天|昨天|明天|刚才|暂时|可能|似乎|也许|打算|计划|今晚|这次|这轮|本次')
_MEMORY_PREFIX = re.compile(r'^(?:@\S+\s*)?(?:请)?(?:记住|记一下|记下来|更正|纠正)[：:，,\s]*$')
_AT_ONLY = re.compile(r'^(?:@\S+\s*)+$')


def valid_quote(text, quote):
    """必须逐字摘录完整句子，拒绝引用、代码、测试和被后文否定的断章取义。"""
    text, quote = str(text or '').strip(), str(quote or '').strip()
    if not 4 <= len(quote) <= 500 or _NO_SAVE.search(text) or _REVERSAL.search(text):
        return False
    start = text.find(quote)
    if start < 0 or any(c in quote for c in ('“', '”', '「', '」', '"')):
        return False
    before, after = text[:start].rstrip(), text[start + len(quote):].lstrip()
    if before and before[-1] not in '。！？!?；;\n' and not _MEMORY_PREFIX.fullmatch(before) and not _AT_ONLY.fullmatch(before):
        return False
    # 引文不能省略同句后续；不同句子的独立陈述可以分别保存。
    return (not after or quote[-1] in '。！？!?；;\n' or after[0] in '。！？!?；;\n' or _AT_ONLY.fullmatch(after))


def canonical_record(text, quote, name, kind='interaction'):
    """稳定自述保留原谓语；其它原话只进入有日期的互动区，不推断身份和性格。"""
    if not valid_quote(text, quote):
        return None
    quote = quote.strip().rstrip('。！；;')
    if kind == 'self' and _SELF.match(quote) and not _TEMP.search(quote) and not re.search(r'[?？]|吗$|吧$|呢$', quote):
        predicate = re.sub(r'^(?:本人|自己|我)', '', quote, count=1)
        section = 'interaction_preferences' if re.search(r'希望|习惯|偏好|不希望', predicate) else 'stable_facts'
        return {'text': name + predicate, 'section': section, 'stability': 'stable'}
    if len(quote) < 8:
        return None
    return {'text': f'{name}在交流中说：“{quote}”', 'section': 'recent_interactions', 'stability': 'temporal'}
