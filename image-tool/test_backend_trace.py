"""错误阶段白名单与失败信息隔离回归，不连接后端。"""
import json
import ssl
from pathlib import Path
import tempfile
import unittest
from backend_trace import BackendTrace, BackendDiagnostic, PREFIX, read_trace
from bridge import backend_failure


class BackendTraceTests(unittest.TestCase):
    def test_completed_response_with_failed_image_and_refusal_keeps_private_explanation(self):
        trace = BackendTrace()
        trace.event({'type': 'response.output_item.done', 'item': {'id': 'image-1',
                     'type': 'image_generation_call', 'status': 'failed', 'result': None,
                     'error': {'code': 'moderation_blocked', 'message': '仅用于本机的拒绝说明'}}})
        trace.event({'type': 'response.completed', 'response': {'status': 'completed', 'output': [
            {'type': 'reasoning', 'summary': [{'text': '绝不记录的推理'}]},
            {'type': 'image_generation_call', 'status': 'failed', 'result': None},
            {'type': 'message', 'role': 'assistant', 'status': 'completed', 'content': [
                {'type': 'refusal', 'refusal': '仅用于本机的拒绝说明'}]}]}})
        result = read_trace(trace.line().encode())
        self.assertEqual(result['response_status'], 'completed')
        self.assertEqual(result['image_call_statuses'], ['failed'])
        self.assertFalse(result['image_result_present'])
        self.assertTrue(result['refusal_present'])
        self.assertEqual(result['error_code'], 'moderation_blocked')
        self.assertNotIn('拒绝说明', json.dumps(result, ensure_ascii=False))
        private = json.dumps(trace.diagnostic.data, ensure_ascii=False)
        self.assertIn('拒绝说明', private)
        self.assertNotIn('绝不记录的推理', private)
        self.assertEqual(len(trace.diagnostic.data['output_items']), 2)

    def test_final_response_alone_and_plain_text_refusal_are_not_guessed(self):
        trace = BackendTrace()
        trace.event({'type': 'response.completed', 'response': {'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': '无法执行本次编辑'}]},
            {'type': 'image_generation_call', 'status': 'failed', 'error': {'message': '原因尚不明确'}}]}})
        result = read_trace(trace.line().encode())
        self.assertTrue(result['assistant_text_present'])
        self.assertFalse(result['refusal_present'])
        self.assertIn('无法执行', str(trace.diagnostic.data))

    def test_terminal_text_events_survive_absent_message_item(self):
        trace = BackendTrace()
        trace.event({'type': 'response.output_text.done', 'item_id': 'message-1', 'text': '后端最终解释'})
        trace.event({'type': 'response.refusal.done', 'item_id': 'message-2', 'refusal': '后端明确拒绝'})
        trace.event({'type': 'response.completed', 'response': {'status': 'completed',
            'output': [{'type': 'reasoning', 'summary': [{'text': '不允许记录'}]}]}})
        self.assertTrue(read_trace(trace.line().encode())['refusal_present'])
        self.assertTrue(read_trace(trace.line().encode())['assistant_text_present'])
        self.assertNotIn('不允许记录', str(trace.diagnostic.data))

    def test_existing_backend_attempts_do_not_mix_refusal_into_later_success(self):
        trace = BackendTrace()
        trace.begin({})
        trace.event({'type': 'response.output_item.done', 'item': {'type': 'message', 'content': [
            {'type': 'refusal', 'refusal': '前次尝试的说明'}]}})
        trace.begin({})
        trace.event({'type': 'response.output_item.done', 'item': {'type': 'image_generation_call',
                     'status': 'completed', 'result': 'image-bytes'}})
        self.assertFalse(read_trace(trace.line().encode())['refusal_present'])
        self.assertTrue(read_trace(trace.line().encode())['image_result_present'])
        self.assertIn('前次尝试的说明', str(trace.diagnostic.data['previous_attempts']))

    def test_success_redacts_credentials_and_never_logs_image_bytes(self):
        trace = BackendTrace()
        trace.event({'type': 'response.output_item.done', 'item': {
            'type': 'image_generation_call', 'status': 'completed', 'result': 'private-image-bytes',
            'revised_prompt': '保留背景 Bearer fixture-token-123',
            'error': {'message': 'access_token=fixture-secret; api_key=sk-abcdefgh123'}}})
        trace.event({'type': 'response.output_item.done', 'item': {'type': 'message', 'content': [
            {'type': 'output_text', 'text': '"refresh_token":"fixture-refresh", data:image/png;base64,AAAA'}]}})
        public = read_trace(trace.line().encode())
        self.assertTrue(public['image_result_present'])
        self.assertEqual(public['image_call_statuses'], ['completed'])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'task' / 'backend-diagnostic.json'
            self.assertTrue(trace.diagnostic.save(path))
            raw = path.read_text(encoding='utf-8')
            for secret in ('private-image-bytes', 'fixture-token-123', 'fixture-secret',
                           'sk-abcdefgh123', 'fixture-refresh', 'base64,AAAA'):
                self.assertNotIn(secret, raw)
            self.assertIn('保留背景', raw)

    def test_incomplete_response_and_forged_metadata_are_bounded(self):
        trace = BackendTrace()
        trace.event({'type': 'response.incomplete', 'response': {'status': 'incomplete',
                     'incomplete_details': {'reason': 'max_output_tokens'}}})
        self.assertEqual(read_trace(trace.line().encode())['incomplete_reason'], 'max_output_tokens')
        raw = (PREFIX + json.dumps({'response_status': 'secret', 'refusal_present': 'secret',
            'image_call_statuses': ['failed', [], 'secret'], 'incomplete_reason': 'secret'})).encode()
        self.assertEqual(read_trace(raw), {'events': {}, 'image_call_statuses': ['failed']})
        for index in range(30):
            trace.event({'type': 'response.output_item.done', 'item': {'id': str(index),
                'type': 'message', 'content': [{'type': 'output_text', 'text': 'a' * 9000}]}})
        self.assertEqual(len(trace.diagnostic.data['output_items']), 16)
        self.assertLessEqual(len(trace.diagnostic.data['output_items'][0]['content'][0]['text']), 2048)

    def test_diagnostic_write_failure_and_malformed_events_do_not_raise(self):
        diagnostic = BackendDiagnostic()
        for event in (None, [], {'type': []}, {'type': 'response.output_item.done', 'item': {
            'type': 'message', 'status': [], 'content': 'invalid'}},
            {'type': 'response.completed', 'response': {'status': [], 'output': [None]}}):
            diagnostic.event(event)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'blocked'
            path.write_text('占位文件', encoding='utf-8')
            self.assertFalse(diagnostic.save(path / 'diagnostic.json'))

    def test_server_image_config_is_separate_from_requested_and_rendered(self):
        trace = BackendTrace()
        trace.begin({'model': 'gpt-6.1-sol', 'reasoning': {'effort': 'low'},
                     'tools': [{'type': 'image_generation', 'background': 'opaque'}]})
        trace.event({'type': 'response.created', 'response': {'model': 'gpt-6.1-sol', 'tools': [
            {'type': 'image_generation', 'model': 'gpt-image-2-codex', 'quality': 'auto', 'background': 'auto',
             'secret': 'token-secret'}]}})
        trace.event({'type': 'response.output_item.done', 'item': {'type': 'image_generation_call',
            'quality': 'high', 'background': 'opaque', 'size': '1024x1024', 'result': 'image-secret', 'revised_prompt': 'prompt-secret'}})
        result = read_trace(trace.line().encode())
        self.assertEqual(result['requested_driver_model'], 'gpt-6.1-sol')
        self.assertEqual(result['returned_driver_model'], 'gpt-6.1-sol')
        self.assertEqual(result['requested_image'], {'background': 'opaque'})
        self.assertEqual(result['effective_image']['model'], 'gpt-image-2-codex')
        self.assertEqual(result['rendered_image'], {'quality': 'high', 'background': 'opaque', 'size': '1024x1024'})
        self.assertNotIn('secret', json.dumps(result))

    def test_model_metadata_rejects_free_text(self):
        raw = (PREFIX + json.dumps({'requested_driver_model': 'token-secret', 'returned_driver_model': 'gpt-6.1-sol\nsecret',
            'effective_image': {'model': 'secret', 'quality': 'secret', 'background': 'auto', 'size': 'secret', 'result': 'secret'}})).encode()
        self.assertEqual(read_trace(raw), {'events': {}, 'effective_image': {'background': 'auto'}})

    def test_stream_failure_preserves_only_phase_and_code(self):
        trace = BackendTrace()
        trace.begin({'input': [{'content': [{'type': 'input_image', 'image_url': 'secret-image'},
                                           {'type': 'input_text', 'text': 'secret-prompt'}]}]})
        trace.event({'type': 'response.created', 'response': {'id': 'secret-id'}})
        trace.event({'type': 'response.failed', 'response': {'error': {'code': 'server_error', 'message': 'secret-message'}}})
        trace.failure(ssl.SSLEOFError('secret-exception'))
        result = read_trace(trace.line().encode())
        self.assertTrue(result['accepted'])
        self.assertEqual(result['reference_count'], 1)
        self.assertEqual(result['exception_type'], 'SSLEOFError')
        self.assertEqual(result['error_code'], 'server_error')
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(backend_failure(trace.line().encode())[0], 'network')

    def test_forged_trace_drops_untrusted_text(self):
        raw = (PREFIX + json.dumps({'accepted': True, 'prompt': 'secret', 'error_code': 'secret',
            'events': {'secret': 1, 'response.failed': 1}, 'http_status': 'secret', 'reference_count': -1})).encode()
        self.assertEqual(read_trace(raw), {'accepted': True, 'events': {'response.failed': 1}})
        self.assertIsNone(read_trace((PREFIX + '[]').encode()))

    def test_invalid_event_does_not_break_original_stream(self):
        trace = BackendTrace()
        for event in ([], None, {'type': []}, {'type': 'error', 'error': {'code': []}}):
            trace.event(event)
        self.assertEqual(trace.data['events'], {'error': 1})


if __name__ == '__main__':
    unittest.main()
