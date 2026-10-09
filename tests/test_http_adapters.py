import copy
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from conversation_mvp.config import load
from conversation_mvp.core import Core
from conversation_mvp.models import Models, request_json
from conversation_mvp.voice import Voice
from test_mvp import item


class Endpoint:
    def __init__(self,respond):
        self.requests=[]
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append(body)
                status,headers,result=respond(body)
                payload=json.dumps(result).encode()
                self.send_response(status)
                self.send_header('Content-Type','application/json')
                for key,value in headers.items():self.send_header(key,value)
                self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.url=f'http://127.0.0.1:{self.server.server_port}/api'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
    def __enter__(self):self.thread.start();return self
    def __exit__(self,*args):self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)


class AdapterWireTests(unittest.TestCase):
    def test_truncated_ollama_generation_is_rejected(self):
        output={'trigger':'IGNORE','confidence':.9,'reason':'対象がありません。'}
        with Endpoint(lambda _: (200,{}, {'message':{'content':json.dumps(output)},'done_reason':'length'})) as endpoint:
            config=load(read_env=False)
            config['trigger'].update(provider='ollama',url=endpoint.url)
            with self.assertRaisesRegex(ValueError,'生成上限'):
                Models(config).call('trigger',{'utterance':item()})

    def test_placeholder_ollama_html_is_rejected(self):
        output={'summary':'試作','files':[{'path':'index.html','content':'省略したHTML'}]}
        with Endpoint(lambda _: (200,{}, {'message':{'content':json.dumps(output)}})) as endpoint:
            config=load(read_env=False)
            config['coder'].update(provider='ollama',url=endpoint.url,num_predict=4096)
            with self.assertRaisesRegex(ValueError,'string length'):
                Models(config).call('coder',{'utterance':item()})
            self.assertEqual(endpoint.requests[0]['options']['num_predict'],4096)
            self.assertEqual(endpoint.requests[0]['format']['properties']['files']['maxItems'],1)

    def test_ollama_schema_and_nonstreaming_protocol(self):
        output={'trigger':'IGNORE','confidence':.9,'reason':'対象となる兆候がありません。'}
        with Endpoint(lambda _: (200,{}, {'message':{'content':json.dumps(output,ensure_ascii=False)},'prompt_eval_count':20,'eval_count':5})) as endpoint:
            config=load(read_env=False);config['trigger'].update({'provider':'ollama','model':'confirmed-test-model','url':endpoint.url})
            result=Models(config).call('trigger',{'utterance':item('今日は晴れ。')})
            self.assertEqual(result.output,output)
            self.assertFalse(endpoint.requests[0]['stream'])
            self.assertFalse(endpoint.requests[0]['format']['additionalProperties'])
            self.assertEqual(result.metrics['input_tokens'],20)
            self.assertEqual(result.metrics['api_cost'],0)

    def test_normalized_bridge_can_generate_an_isolated_bundle(self):
        def respond(body):
            role=body['role']
            outputs={
                'trigger':{'trigger':'TRIGGER_PROTOTYPE','confidence':.8,'reason':'小さな試作につながる発言を検出しました。'},
                'decision':{'action':'START_PROTOTYPE','reason':'文脈に合い隔離した試作で確認できるため始めます。','context_interpretation':'ローカルだけで案を確認する会議です。','title':'試作','brief':'散歩のテーマを選ぶ画面','operations':['isolated_prototype','show_private']},
                'coder':{'summary':'テスト用のコード生成モデルによる試作です。','files':[{'path':'index.html','content':'<!doctype html><h1>模型による試作</h1><button>試す</button>'}]}}
            return 200,{}, {'output':outputs[role],'usage':{'input_tokens':20,'output_tokens':10}}
        with Endpoint(respond) as endpoint,tempfile.TemporaryDirectory() as directory:
            config=load(read_env=False);config['app'].update({'runtime_dir':directory,'lookahead_seconds':0})
            for role in ('trigger','main','coder'):config[role].update({'provider':'json_http','model':'confirmed-test-model','url':endpoint.url})
            core=Core(config)
            try:
                core.ingest(item());core.flush();artifact=core.state()['prototypes'][0]
                self.assertEqual(artifact['status'],'完成')
                self.assertIn('模型による試作',Path(artifact['entrypoint']).read_text())
                self.assertEqual([r['role'] for r in endpoint.requests],['trigger','decision','coder'])
                self.assertIsNone(artifact['metrics']['api_cost'])
            finally:core.close()

    def test_invalid_bridge_schema_blocks_execution(self):
        with Endpoint(lambda _: (200,{}, {'output':{'trigger':'TRIGGER_PROTOTYPE','confidence':1,'reason':'試作の候補です。','execute':True}})) as endpoint,tempfile.TemporaryDirectory() as directory:
            config=load(read_env=False);config['app'].update({'runtime_dir':directory,'lookahead_seconds':0});config['trigger'].update({'provider':'json_http','url':endpoint.url})
            core=Core(config)
            try:
                core.ingest(item());core.flush();self.assertEqual(core.state()['prototypes'],[])
                self.assertEqual(core.state()['timeline'][0]['gate']['action'],'IGNORE')
            finally:core.close()

    def test_redirect_is_not_followed(self):
        with Endpoint(lambda _: (302,{'Location':'https://should-not-be-contacted.invalid'},{})) as endpoint:
            config=load(read_env=False)
            with self.assertRaises(ValueError):request_json(endpoint.url,{},config['trigger'],config['permissions'])
            self.assertEqual(len(endpoint.requests),1)

    def test_voice_bridge_closes_and_records_total_usage(self):
        def respond(body):
            if body['operation']=='open':return 200,{}, {'output':{'session_id':'test-session','ready':True,'summary':'音声ブリッジのテストです。'}}
            return 200,{}, {'closed':True,'usage':{'input_tokens':1000,'output_tokens':500}}
        with Endpoint(respond) as endpoint:
            config=load(read_env=False);config['voice'].update({'provider':'json_http','url':endpoint.url,'session_seconds':.01,'input_price_per_million':2,'output_price_per_million':4})
            ready=[];result=Voice(config).run({'utterance':item()},threading.Event(),lambda output,metrics:ready.append((output,metrics)))
            self.assertTrue(result['closed']);self.assertTrue(ready[0][0]['ready'])
            self.assertGreaterEqual(ready[0][1]['ready_latency_ms'],0)
            self.assertAlmostEqual(result['metrics']['api_cost'],.004)
            self.assertEqual([r['operation'] for r in endpoint.requests],['open','close'])
            self.assertFalse(endpoint.requests[0]['auto_speak'])
            self.assertFalse(endpoint.requests[0]['external_post'])


if __name__=='__main__':unittest.main()
