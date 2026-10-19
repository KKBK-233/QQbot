"""人工公众号入口的域名/提交策略与无凭据正文交付测试，不连接公网。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import importlib.util
import json
import unittest

# 公开快照没有本地搜索插件源码；缺少依赖时跳过，不执行不完整的入口。
root = Path(__file__).resolve().parents[1]
handoff = None
if (root / 'MaiBot/plugins/qqbot-web-search/manual_article.py').is_file():
    spec = importlib.util.spec_from_file_location('handoff', root / 'verify-wechat-article.py')
    handoff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handoff)


@unittest.skipUnless(handoff is not None, '需要本地搜索插件源码，公开快照未包含')
class HandoffTests(unittest.TestCase):
    def test_only_original_article_is_delivered(self):
        url = 'https://mp.weixin.qq.com/s/public'
        self.assertTrue(handoff.same_article(url, url + '#fragment'))
        self.assertFalse(handoff.same_article(url, 'https://mp.weixin.qq.com/s/other'))
        self.assertFalse(handoff.same_article(url, 'https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha'))

    def test_network_is_limited_to_article_and_challenge(self):
        self.assertTrue(handoff.allowed_request('https://mp.weixin.qq.com/s/public', 'GET', 'document', main_frame=True))
        self.assertTrue(handoff.allowed_request('https://ssl.captcha.qq.com/verify', 'POST', 'fetch'))
        self.assertTrue(handoff.allowed_request('https://captcha.gtimg.com/challenge.png', 'GET', 'image'))
        for url, method, resource, main in (
            ('http://mp.weixin.qq.com/s/public', 'GET', 'document', True),
            ('https://user:secret@mp.weixin.qq.com/s/public', 'GET', 'document', True),
            ('https://127.0.0.1/', 'GET', 'fetch', False),
            ('https://ssl.captcha.qq.com.evil.test/verify', 'POST', 'fetch', False),
            ('https://mp.weixin.qq.com/write', 'POST', 'fetch', False),
            ('https://captcha.gtimg.com/challenge', 'GET', 'document', True),
            ('https://mp.weixin.qq.com/login', 'GET', 'document', True)):
            self.assertFalse(handoff.allowed_request(url, method, resource, main_frame=main), url)

    def test_cache_has_only_exact_public_source_and_bounded_text(self):
        url = 'https://mp.weixin.qq.com/s/public'
        with TemporaryDirectory() as directory, patch.object(handoff, 'ROOT', Path(directory)):
            handoff.save_article(url, {'title': '公开文章', 'text': 'x' * 12000, 'cookie': 'must-not-copy'})
            path = next((Path(directory) / 'data/web-articles').glob('*.json'))
            data = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(data['url'], url)
            self.assertEqual(len(data['text']), 10000)
            self.assertTrue(data['truncated'])
            self.assertNotIn('cookie', data)


if __name__ == '__main__':
    unittest.main()
