import base64
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from conversation_mvp.config import load
from conversation_mvp.hermes_bridge import invoke
from conversation_mvp.voice import Voice
from conversation_mvp.hermes_worker import speech_reply


class SpeechBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.config = load(read_env=False)
        self.config['input']['source'] = 'browser'
        self.config['voice'].update(provider='hermes_nous', model='test-model')
        self.config['permissions'].update(external_model_data=True, paid_model_calls=True)
        self.voice = Voice(self.config)
        self.core = SimpleNamespace(acknowledged=set(), audit=Mock())
        self.pcm = base64.b64encode(b'\x00\x20' * 3200).decode()
        self.packet = dict(source='browser_mic', sample_rate=16000, pcm_base64=self.pcm)

    def test_disabled_external_or_paid_permission_starts_no_worker(self):
        for permissions in ({'external_model_data': False, 'paid_model_calls': True},
                            {'external_model_data': True, 'paid_model_calls': False}):
            with patch('conversation_mvp.hermes_bridge.subprocess.run') as run:
                with self.assertRaises(PermissionError):
                    invoke({'operation': 'turn'}, permissions)
                run.assert_not_called()

    def test_microphone_requires_acknowledgement_before_any_api(self):
        with patch('conversation_mvp.hermes_bridge.invoke') as call:
            with self.assertRaisesRegex(ValueError, '確認'):
                self.voice.turn(self.packet, self.core)
            call.assert_not_called()

    def test_silence_and_oversize_never_call_api(self):
        self.core.acknowledged.add('browser_mic')
        for pcm in (b'\x00' * 6400, b'\x00\x20' * 240001):
            with patch('conversation_mvp.hermes_bridge.invoke') as call:
                with self.assertRaises(ValueError):
                    self.voice.turn({**self.packet, 'pcm_base64': base64.b64encode(pcm).decode()}, self.core)
                call.assert_not_called()

    def test_concurrent_turn_does_not_call_api(self):
        self.voice.turn_lock.acquire()
        try:
            with patch('conversation_mvp.hermes_bridge.invoke') as call:
                with self.assertRaisesRegex(ValueError, '返答中'):
                    self.voice.turn({'text': 'こんにちは'}, self.core)
                call.assert_not_called()
        finally:
            self.voice.turn_lock.release()

    def test_audio_not_retained_and_followup_has_context(self):
        result = dict(text='牛乳を忘れない方法は？', reply='買い物リストを使いましょう。',
                      audio_base64='test-audio', model='test-model', tts_model='tts-model',
                      usage={'total_tokens': 10}, latency_ms=123)
        self.core.acknowledged.add('browser_mic')
        with patch('conversation_mvp.hermes_bridge.invoke', return_value=result) as call:
            self.assertEqual(self.voice.turn(self.packet, self.core), result)
            self.voice.turn({'text': 'ほかには？'}, self.core)
            self.assertEqual(call.call_args.args[0]['history'][0]['content'], result['text'])
        for event in self.core.audit.append.call_args_list:
            self.assertNotIn('audio_base64', event.kwargs)
            self.assertNotIn('pcm_base64', event.kwargs)

    def test_failed_turn_keeps_context_and_releases_lock(self):
        with patch('conversation_mvp.hermes_bridge.invoke', side_effect=ValueError('provider failed')):
            with self.assertRaises(ValueError):
                self.voice.turn({'text': 'こんにちは'}, self.core)
        self.assertEqual(self.voice.history, [])
        self.assertFalse(self.voice.turn_lock.locked())
        self.core.audit.append.assert_not_called()

    def test_tool_free_voice_does_not_narrate_completed_operations(self):
        reply, guarded = speech_reply('リストに牛乳を追加しました。他にありますか？')
        self.assertTrue(guarded)
        self.assertIn('実行できません', reply)
        self.assertFalse(speech_reply('リストに牛乳を記入するとよいですね。')[1])
        self.assertFalse(speech_reply('牛乳は購入しましたか？')[1])

    def test_worker_error_never_exposes_stderr(self):
        bootstrap = SimpleNamespace(stdout='["python", "-c", "import runpy; runpy.run_module(\"hermes_cli.main\")"]')
        failed = SimpleNamespace(returncode=1, stdout='secret-token', stderr='secret-token')
        with patch('conversation_mvp.hermes_bridge.subprocess.run', side_effect=[bootstrap, failed]):
            with self.assertRaises(ValueError) as error:
                invoke({'operation': 'turn'}, self.config['permissions'])
            self.assertNotIn('secret-token', str(error.exception))


if __name__ == '__main__':
    unittest.main()
