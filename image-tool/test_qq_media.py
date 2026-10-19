"""验证 QQ 补取的群隔离、图片来源限制和大小边界，不连接真实服务。"""
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'maibot-plugin'))
from qq_media import QQMediaClient, NoRedirect, read_bounded


class QQMediaTests(unittest.TestCase):
    def test_group_and_message_are_checked_before_image_fetch(self):
        client = QQMediaClient()
        client.fetch_image = Mock(return_value='original')
        for changes in ({'group_id': 456}, {'message_id': 99}, {'message_type': 'private'}):
            raw = {'message_id': -2, 'message_type': 'group', 'group_id': 123,
                   'message': [{'type': 'image', 'data': {'file': 'cache.png'}}], **changes}
            with patch.object(client, 'call', return_value=raw):
                self.assertIsNone(client.get_message('group-a', '123', '-2', 2))
        client.fetch_image.assert_not_called()

    def test_same_group_reads_exact_images_and_quote_with_count_limit(self):
        client = QQMediaClient()
        client.fetch_image = Mock(return_value='original')
        raw = {'message_id': -2, 'message_type': 'group', 'group_id': 123,
               'message': [{'type': 'reply', 'data': {'id': '-3'}},
                           *[{'type': 'image', 'data': {'file': f'{i}.png'}} for i in range(3)]]}
        with patch.object(client, 'call', return_value=raw):
            result = client.get_message('group-a', '123', '-2', 2)
        self.assertEqual(result['session_id'], 'group-a')
        self.assertEqual(result['raw_message'][0]['data']['target_message_id'], '-3')
        self.assertEqual(client.fetch_image.call_count, 2)

    def test_untrusted_image_addresses_never_open(self):
        client = QQMediaClient()
        client.opener = Mock()
        for url in ('http://multimedia.nt.qq.com.cn/a', 'https://example.com/a',
                    'file:///etc/passwd', 'https://multimedia.nt.qq.com.cn:9000/a',
                    'https://user:pass@multimedia.nt.qq.com.cn/a'):
            with patch.object(client, 'call', return_value={'url': url}):
                with self.assertRaises(ValueError):
                    client.fetch_image('cache.png')
        client.opener.open.assert_not_called()

    def test_qq_image_download_does_not_receive_onebot_auth(self):
        client = QQMediaClient()
        client.opener = Mock()
        response = io.BytesIO(b'\x89PNG\r\n\x1a\nfixture')
        client.opener.open.return_value = response
        with patch.object(client, 'call', return_value={'url': 'https://multimedia.nt.qq.com.cn/image'}):
            self.assertTrue(client.fetch_image('cache.png'))
        request = client.opener.open.call_args.args[0]
        self.assertFalse(request.has_header('Authorization'))

    def test_non_image_error_page_size_limit_and_redirect_are_rejected(self):
        with self.assertRaises(ValueError):
            read_bounded(io.BytesIO(b'12345'), 4)
        client = QQMediaClient()
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(b'<html>error</html>')
        with patch.object(client, 'call', return_value={'url': 'https://multimedia.nt.qq.com.cn/a'}):
            with self.assertRaises(ValueError):
                client.fetch_image('cache.png')
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com'))


if __name__ == '__main__':
    unittest.main()
