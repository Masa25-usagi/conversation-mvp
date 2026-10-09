import base64
import copy
import json
import math
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from conversation_mvp.audit import Audit, verify
from conversation_mvp.audio import Audio, pcm_wav
from conversation_mvp.config import load
from conversation_mvp.contracts import ROOT, TRIGGERS, schema, strict_json, utterance, validate_output
from conversation_mvp.core import Core
from conversation_mvp.evaluation import build, dataset, evaluate, extract, write_jsonl
from conversation_mvp.models import Models, PermissionNeeded, detect_rules, guard_endpoint, main_rules, measured, normalized
from conversation_mvp.policy import enforce
from conversation_mvp.sandbox import validate_bundle
from conversation_mvp.server import make_server, replay


def item(text="散歩のアプリを小さく試作したい。", identity="test_1", offset=0, source="manual", provenance="synthetic"):
    now = datetime(2026,9,30,16,0,tzinfo=timezone(timedelta(hours=9)))+timedelta(seconds=offset)
    return {"id":identity,"text":text,"start_at":now.isoformat(),"end_at":(now+timedelta(seconds=1)).isoformat(),"source":source,"provenance":provenance}


def decision(action="START_PROTOTYPE", operations=None):
    return {"action":action,"reason":"会議目的に合うため試作します。","context_interpretation":"個人の小さな試作を求める文脈です。",
            "title":"試作","brief":"散歩のアプリを試作したい。","operations":operations if operations is not None else ["isolated_prototype","show_private"]}


class ContractTests(unittest.TestCase):
    def test_only_five_triggers(self):
        self.assertEqual(tuple(schema("trigger")["properties"]["trigger"]["enum"]),TRIGGERS)
        for name in TRIGGERS:
            validate_output({"trigger":name,"confidence":.7,"reason":"兆候を検出しました。"},"trigger")

    def test_unknown_trigger_and_extra_keys_rejected(self):
        for payload in ({"trigger":"RUN","confidence":1,"reason":"実行します。"},
                        {"trigger":"IGNORE","confidence":1,"reason":"待ちます。","execute":True}):
            with self.assertRaises(ValueError):validate_output(payload,"trigger")

    def test_reason_requires_one_sentence(self):
        with self.assertRaises(ValueError):validate_output({"trigger":"IGNORE","confidence":1,"reason":"待ちます。実行します。"},"trigger")

    def test_bool_is_not_confidence(self):
        with self.assertRaises(ValueError):validate_output({"trigger":"IGNORE","confidence":True,"reason":"待ちます。"},"trigger")

    def test_duplicate_json_and_nonfinite_rejected(self):
        for text in ('{"trigger":"IGNORE","trigger":"RUN"}','{"confidence":NaN}'):
            with self.assertRaises(ValueError):strict_json(text)

    def test_timestamp_requires_timezone_and_order(self):
        for change in ({"start_at":"2026-09-30T00:00:00"},{"end_at":"2026-09-29T00:00:00+09:00"}):
            with self.assertRaises(ValueError):utterance({**item(),**change})


class ModelPolicyTests(unittest.TestCase):
    def setUp(self):self.config=load(read_env=False)

    def test_aliases_preserve_original(self):
        u=item("こーりん、相談に来て。")
        result=detect_rules({"utterance":u},self.config)
        self.assertEqual(result["trigger"],"CALL_VOICE_AI")
        self.assertEqual(u["text"],"こーりん、相談に来て。")
        self.assertIn("プロトタイプ",normalized("プロト タイプ",self.config))

    def test_main_distinguishes_name_mention(self):
        u=item("コリンという名前の機能について説明する。")
        result=main_rules({"utterance":u,"detection":{"trigger":"CALL_VOICE_AI"},"meeting_goal":"試作","preceding":[]},self.config)
        self.assertEqual(result["action"],"IGNORE")

    def test_following_cancellation_prevents_prototype(self):
        result=main_rules({"utterance":item(),"detection":{"trigger":"TRIGGER_PROTOTYPE"},"meeting_goal":"試作",
                           "following":[item("やっぱり今は作らない。","later",3)]},self.config)
        self.assertEqual(result["action"],"IGNORE")

    def test_prior_meeting_policy_can_stop_prototype(self):
        result=main_rules({"utterance":item(),"detection":{"trigger":"TRIGGER_PROTOTYPE"},"meeting_goal":"試作",
                           "preceding":[item("この会議では試作しない。","prior",-3)]},self.config)
        self.assertEqual(result["action"],"IGNORE")

    def test_unrelated_quotation_does_not_cancel_idea(self):
        result=main_rules({"utterance":item(),"detection":{"trigger":"TRIGGER_PROTOTYPE"},"meeting_goal":"試作",
                           "following":[item("この単語を引用して説明している。","later",3)]},self.config)
        self.assertEqual(result["action"],"START_PROTOTYPE")

    def test_non_prototype_meeting_goal_becomes_private_suggestion(self):
        result=main_rules({"utterance":item(),"detection":{"trigger":"TRIGGER_PROTOTYPE"},"meeting_goal":"数学の説明を理解する"},self.config)
        self.assertEqual(result["action"],"SUGGEST_PRIVATE")

    def test_every_high_permission_capability_is_blocked(self):
        for operation in ("external_send","publish","charge","production_write","data_delete"):
            result=enforce({"trigger":"TRIGGER_PROTOTYPE"},decision(operations=[operation]))
            self.assertEqual(result.action,"REQUIRE_PERMISSION")

    def test_speech_and_external_post_are_stubs(self):
        for operation in ("meeting_speech","external_post"):
            self.assertEqual(enforce({"trigger":"TRIGGER_PROTOTYPE"},decision(operations=[operation])).action,"STUB")

    def test_main_cannot_upgrade_private_signal_to_prototype(self):
        self.assertEqual(enforce({"trigger":"SUGGEST_PRIVATE"},decision()).action,"IGNORE")

    def test_implied_permissions_cannot_be_omitted(self):
        result=enforce({"trigger":"TRIGGER_PROTOTYPE"},decision(operations=[]))
        self.assertIn("isolated_prototype",result.operations)

    def test_remote_data_and_billing_are_independent_opt_ins(self):
        settings={"billable":True}
        for permissions in ({"external_model_data":False,"paid_model_calls":True},
                            {"external_model_data":True,"paid_model_calls":False}):
            with self.assertRaises(PermissionNeeded):guard_endpoint("https://provider.invalid/api",settings,permissions)
        guard_endpoint("https://provider.invalid/api",settings,{"external_model_data":True,"paid_model_calls":True})

    def test_remote_http_and_embedded_credentials_are_rejected(self):
        for url in ("http://provider.invalid/api","https://user:user-040a9d7@example.com/api"):
            with self.assertRaises(ValueError):guard_endpoint(url,{"billable":False},{"external_model_data":True,"paid_model_calls":False})

    def test_unknown_cost_remains_unknown_and_prices_are_computed(self):
        settings={"provider":"json_http","model":"confirmed","billable":True}
        self.assertIsNone(measured(settings,.01)["api_cost"])
        costs=measured({**settings,"input_price_per_million":2,"output_price_per_million":4},.01,{"input_tokens":1000,"output_tokens":500})
        self.assertAlmostEqual(costs["api_cost"],.004)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.config=load(read_env=False)
        self.config["app"].update({"runtime_dir":self.temp.name,"prototype_cooldown_seconds":0,"lookahead_seconds":.1})
        self.core=Core(self.config)
    def tearDown(self):
        self.core.close()
        self.temp.cleanup()

    def test_demo_proves_provenance_and_silence(self):
        replay(self.core,ROOT/"fixtures/demo.jsonl")
        state=self.core.state()
        ready=[p for p in state["prototypes"] if p["status"]=="完成"]
        self.assertEqual(len(ready),1)
        file=Path(ready[0]["entrypoint"])
        self.assertTrue(file.is_file())
        self.assertTrue(file.is_relative_to(Path(self.temp.name)/"prototypes"))
        self.assertIn("今日の散歩",file.read_text())
        manifest=json.loads(Path(ready[0]["manifest"]).read_text())
        self.assertIn("request_event_id",manifest)
        self.assertEqual(manifest["trigger_utterance"]["text"],ready[0]["utterance"]["text"])
        self.assertTrue(any(t["gate"]["action"]=="IGNORE" for t in state["timeline"]))
        self.assertTrue(any(t["gate"]["action"]=="REQUIRE_PERMISSION" for t in state["timeline"]))
        self.assertTrue(any(t["gate"]["action"]=="SUGGEST_PRIVATE" for t in state["timeline"]))
        self.assertEqual(state["voice"][-1]["metrics"]["measurement_kind"],"stub")
        self.assertIsNone(state["voice"][-1]["metrics"]["ready_latency_ms"])
        self.assertGreater(len(verify(self.core.audit.path)),20)

    def test_ignore_does_not_call_main(self):
        original=self.core.models.call
        calls=[]
        def call(role,payload):calls.append(role);return original(role,payload)
        self.core.models.call=call
        self.core.ingest(item("今日は晴れている。"))
        self.core.flush()
        self.assertEqual(calls,["trigger"])
        self.assertIn("メインモデルは呼び出していません",self.core.state()["timeline"][0]["context_interpretation"])

    def test_future_negation_is_seen_by_core(self):
        self.core.ingest(item())
        self.core.ingest(item("やっぱり今は作らない。","later",3))
        self.core.flush()
        self.assertEqual(self.core.state()["prototypes"],[])

    def test_duplicate_input_is_idempotent(self):
        self.assertTrue(self.core.ingest(item())["accepted"])
        self.assertTrue(self.core.ingest(item())["duplicate"])
        self.core.flush()
        self.assertEqual(len(self.core.state()["prototypes"]),1)

    def test_bad_model_json_fails_closed(self):
        with patch.object(self.core.models,"call",side_effect=ValueError("extra keys")):
            self.core.ingest(item())
            self.core.flush()
        self.assertEqual(self.core.state()["prototypes"],[])
        self.assertEqual(self.core.state()["timeline"][0]["gate"]["action"],"IGNORE")

    def test_revoke_persists_without_deleting_files(self):
        self.core.ingest(item())
        self.core.flush()
        p=self.core.state()["prototypes"][0]
        self.core.revoke(p["prototype_id"])
        self.assertIsNone(self.core.active_artifact(p["prototype_id"]))
        self.assertTrue(Path(p["entrypoint"]).exists())
        self.core.close()
        self.core=Core(self.config)
        self.assertEqual(self.core.state()["prototypes"][0]["status"],"取消済み")
        self.assertIsNone(self.core.active_artifact(p["prototype_id"]))

    def test_prototype_count_limit_is_enforced(self):
        self.config["app"]["prototype_limit_per_session"]=1
        self.core.ingest(item())
        self.core.flush()
        self.core.ingest(item("集中タイマーを作りたい。","second",5))
        self.core.flush()
        self.assertEqual(len(self.core.state()["prototypes"]),1)
        self.assertIn("上限",self.core.state()["timeline"][-1]["gate"]["reason"])

    def test_recording_input_requires_acknowledgement(self):
        with self.assertRaises(PermissionNeeded):self.core.ingest(item(source="browser_mic"))
        self.core.acknowledge_input("browser_mic",True)
        self.assertTrue(self.core.ingest(item(source="browser_mic"))["accepted"])

    def test_private_note_path_is_logged(self):
        self.core.ingest(item("話の論点を整理して。"))
        self.core.flush()
        path=Path(self.temp.name)/"notes.jsonl"
        self.assertTrue(path.exists())
        self.assertTrue(any(e["kind"]=="note_updated" and e["data"]["path"]==str(path) for e in self.core.audit.events))

    def test_cancel_queued_prototype_does_not_create_files(self):
        started,release=threading.Event(),threading.Event()
        original=self.core.sandbox.run
        def run(prototype_id,payload):
            started.set();release.wait(3);return original(prototype_id,payload)
        self.core.sandbox.run=run
        try:
            self.core.ingest(item())
            self.assertTrue(started.wait(2))
            self.core.ingest(item("集中タイマーを試作したい。","queued",3))
            deadline=time.monotonic()+2
            while len(self.core.state()["prototypes"])<2 and time.monotonic()<deadline:time.sleep(.01)
            queued=self.core.state()["prototypes"][1]
            self.core.revoke(queued["prototype_id"])
            release.set();self.core.flush()
            self.assertFalse((self.core.sandbox.root/queued["prototype_id"]).exists())
            self.assertTrue(any(e["kind"]=="prototype_canceled" and e["data"]["prototype_id"]==queued["prototype_id"] for e in self.core.audit.events))
        finally:release.set()

    def test_symlinked_artifact_directory_is_not_exposed(self):
        self.core.ingest(item());self.core.flush()
        p=self.core.state()["prototypes"][0]
        directory=Path(p["path"])
        preserved=directory.with_name(directory.name+"-preserved")
        directory.rename(preserved)
        directory.symlink_to(preserved,target_is_directory=True)
        self.assertIsNone(self.core.active_artifact(p["prototype_id"]))


class AuditSandboxTests(unittest.TestCase):
    def test_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            audit=Audit(directory);audit.append("sample",reason="original");audit.close()
            path=Path(directory)/"audit.jsonl";path.write_text(path.read_text().replace("original","modified"))
            with self.assertRaises(ValueError):verify(path)

    def test_two_process_owners_cannot_open_same_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            audit=Audit(directory)
            try:
                with self.assertRaises(ValueError):Audit(directory)
            finally:audit.close()

    def test_generated_paths_and_overlarge_files_rejected(self):
        settings=load(read_env=False)["coder"]
        for name in ("../escape.html","/tmp/escape.html",".env","command.sh","folder/index.html","manifest.json"):
            with self.assertRaises(ValueError):validate_bundle({"summary":"test","files":[{"path":name,"content":"test"}]},settings)
        with self.assertRaises(ValueError):validate_bundle({"summary":"test","files":[{"path":"index.html","content":"x"*200001}]},settings)


class AudioHTTPTests(unittest.TestCase):
    def test_pcm_reaches_transcriber_and_common_core(self):
        with tempfile.TemporaryDirectory() as directory:
            config=load(read_env=False);config["app"].update({"runtime_dir":directory,"lookahead_seconds":0});config["input"]["source"]="browser"
            core=Core(config);audio=Audio(core);core.acknowledge_input("browser_mic",True)
            pcm=b"".join(struct.pack("<h",int(6000*math.sin(i/10))) for i in range(16000))
            self.assertEqual(pcm_wav(pcm)[:4],b"RIFF")
            try:
                with patch("conversation_mvp.audio.request_json",return_value={"text":"散歩のアプリを小さく試作したい。"}) as request:
                    audio.submit({"source":"browser_mic","sample_rate":16000,"start_at":item()["start_at"],"pcm_base64":base64.b64encode(pcm).decode()})
                    audio.drain();core.flush();self.assertEqual(request.call_count,1)
                self.assertEqual(core.state()["timeline"][0]["utterance"]["provenance"],"real")
                self.assertEqual(core.state()["prototypes"][0]["status"],"完成")
            finally:audio.close();core.close()

    def test_silence_does_not_call_transcriber(self):
        with tempfile.TemporaryDirectory() as directory:
            config=load(read_env=False);config["app"]["runtime_dir"]=directory;config["input"]["source"]="browser"
            core=Core(config);audio=Audio(core);core.acknowledge_input("browser_mic",True)
            try:
                with patch("conversation_mvp.audio.request_json") as request:
                    audio.submit({"source":"browser_mic","sample_rate":16000,"start_at":item()["start_at"],"pcm":b"\0"*32000});audio.drain()
                    self.assertEqual(request.call_count,0)
                self.assertTrue(any(e["kind"]=="audio_silence" for e in core.audit.events))
            finally:audio.close();core.close()

    def test_http_csrf_host_and_artifact_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            config=load(read_env=False);config["app"].update({"runtime_dir":directory,"lookahead_seconds":0})
            core=Core(config);server=make_server(core,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f"http://127.0.0.1:{server.server_port}"
            def request(path,body=None,token=True,headers=None):
                h={"X-MVP-Token":server.mvp_token} if token else {}
                if body is not None:h["Content-Type"]="application/json"
                h.update(headers or {})
                return urllib.request.urlopen(urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=h),timeout=5)
            try:
                with request("/api/session",token=False) as response:
                    self.assertEqual(json.load(response)["token"],server.mvp_token)
                    self.assertEqual(response.headers["Cache-Control"],"no-store")
                with self.assertRaises(urllib.error.HTTPError) as exc:
                    request("/api/session",token=False,headers={"Origin":"https://attacker.invalid"})
                self.assertEqual(exc.exception.code,403)
                with self.assertRaises(urllib.error.HTTPError):
                    request("/api/session",token=False,headers={"Host":"attacker.invalid"})
                with self.assertRaises(urllib.error.HTTPError) as exc:request("/api/state",token=False)
                self.assertEqual(exc.exception.code,403)
                with self.assertRaises(urllib.error.HTTPError):request("/api/utterance",{"text":"test"},headers={"Origin":"https://attacker.invalid"})
                with self.assertRaises(urllib.error.HTTPError):request("/",headers={"Host":"attacker.invalid"})
                with request("/api/utterance",{"text":"散歩アプリを試作したい。","provenance":"synthetic"}) as response:self.assertEqual(response.status,202)
                core.flush();p=core.state()["prototypes"][0]
                with request(f"/artifact/{p['prototype_id']}/index.html",token=False) as response:
                    csp=response.headers["Content-Security-Policy"];self.assertIn("sandbox allow-scripts",csp);self.assertIn("connect-src 'none'",csp);self.assertNotIn("allow-same-origin",csp)
                with request("/api/prototype/revoke",{"prototype_id":p["prototype_id"]}) as response:self.assertEqual(response.status,200)
                with self.assertRaises(urllib.error.HTTPError):request(f"/artifact/{p['prototype_id']}/index.html",token=False)
            finally:
                server.shutdown();server.server_close();server.mvp_audio.close();core.close();thread.join(timeout=3)


class EvaluationTests(unittest.TestCase):
    def test_balanced_synthetic_data_cannot_be_called_real(self):
        cases=dataset(ROOT/"fixtures/synthetic-eval.jsonl",require_balanced=True)
        self.assertEqual(len(cases),40)
        with self.assertRaises(ValueError):dataset(ROOT/"fixtures/synthetic-eval.jsonl",require_real=True)

    def test_errors_are_not_reported_as_successful_latency_or_zero_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/"blocked.toml";config.write_text('[trigger]\nprovider="unconfigured"\nmodel="unconfirmed"\n')
            report=evaluate(ROOT/"fixtures/synthetic-eval.jsonl",[("unknown",config)],Path(directory)/"report.json")
            row=report["candidates"][0]
            self.assertEqual(row["status"],"failed_all");self.assertEqual(row["error_rate"],1)
            self.assertIsNone(row["latency_p50_ms"]);self.assertIsNone(row["total_api_cost"])

    def test_real_candidates_require_human_labels_and_ten_per_class(self):
        with tempfile.TemporaryDirectory() as directory:
            config=load(read_env=False);config["app"].update({"runtime_dir":directory,"lookahead_seconds":0})
            core=Core(config)
            try:
                core.ingest(item("今日は晴れ。",provenance="real"));core.flush()
                candidates=Path(directory)/"candidates.jsonl";self.assertEqual(extract(core.audit.path,candidates),1)
                with self.assertRaises(ValueError):dataset(candidates)
                labels=Path(directory)/"labels.json";labels.write_text(json.dumps({"test_1":{"class":"wanted_silence","annotator":"human"}}))
                existing=Path(directory)/"real.jsonl.pending";existing.write_text("existing user file")
                with self.assertRaises(ValueError):build(candidates,labels,Path(directory)/"real.jsonl")
                self.assertEqual(existing.read_text(),"existing user file")
            finally:core.close()


if __name__=="__main__":unittest.main()
