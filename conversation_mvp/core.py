"""One shared pipeline, independent of capture source and presentation surface."""

import hashlib
import json
import os
import queue
import threading
import time
import uuid
import heapq
from collections import OrderedDict
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

from .audit import Audit, canonical
from .contracts import parse_time, utterance
from .models import Models, PermissionNeeded, normalized
from .policy import enforce
from .sandbox import Sandbox
from .voice import Voice


class Core:
    def __init__(self, config):
        self.config = config
        self.audit = Audit(config["app"]["runtime_dir"])
        self.models = Models(config)
        self.sandbox = Sandbox(config, self.models)
        self.voice = Voice(config)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.context = []
        self.ids = {e["data"]["utterance"]["id"] for e in self.audit.events if e["kind"] == "utterance_received"}
        self.revoked = {e["data"]["prototype_id"] for e in self.audit.events if e["kind"] == "prototype_revoked"}
        self.acknowledged = set()
        self.pending = []
        self.pending_sequence = 0
        self.active_policy = 0
        self.force_flush = False
        self.closed = False
        self.prototype_count = 0
        self.last_prototype = float("-inf")
        self.prototype_fingerprints = set()
        self.detect_queue = queue.Queue(config["app"]["queue_limit"])
        self.prototype_queue = queue.Queue(config["app"]["prototype_limit_per_session"])
        self.voice_queue = queue.Queue(1)
        self.voice_stop = threading.Event()
        self.voice_busy = False
        self.audit.append("session_started", meeting_goal=config["app"]["meeting_goal"],
                          prior_policy=config["app"]["prior_policy"],
                          adapters={k: config[k]["provider"] for k in ("trigger", "main", "coder", "voice")},
                          input_source=config["input"]["source"])
        self.threads = [threading.Thread(target=task, daemon=True, name=name) for name, task in (
            ("signals", self._detect_worker), ("context-decisions", self._policy_worker),
            ("prototypes", self._prototype_worker), ("on-demand-voice", self._voice_worker))]
        for thread in self.threads:
            thread.start()

    def acknowledge_input(self, source, reviewed):
        if source not in {"browser_mic", "browser_system", "browser_mixed", "ffmpeg", "jsonl"} or reviewed is not True:
            raise ValueError("review recording/terms/participant-notice requirements before connecting")
        with self.lock:
            self.audit.append("input_requirements_acknowledged", source=source,
                              requirements=["service_terms", "participant_notice", "retention_and_processing"])
            self.acknowledged.add(source)

    def ingest(self, value):
        item = utterance(value)
        with self.lock:
            if self.closed:
                raise ValueError("core is closed")
            if item["source"] in {"browser_mic", "browser_system", "browser_mixed", "ffmpeg", "jsonl"} and item["source"] not in self.acknowledged:
                raise PermissionNeeded("録音・文字起こしの接続前確認がまだ記録されていません。")
            if item["id"] in self.ids:
                return {"accepted": False, "duplicate": True, "utterance_id": item["id"]}
            if self.detect_queue.full():
                self.audit.append("input_rejected", reason="判断役の待ち行列が上限に達しました。", utterance=item)
                raise ValueError("signal queue is full; pause input and retry")
            threshold = parse_time(item["end_at"]) - timedelta(seconds=self.config["app"]["context_seconds"])
            self.context = [u for u in self.context if parse_time(u["end_at"]) >= threshold]
            preceding = self.context[-self.config["app"]["context_max_utterances"]:]
            self.audit.append("utterance_received", utterance=item,
                              normalized_text=normalized(item["text"], self.config))
            self.context.append(item)
            self.context = self.context[-self.config["app"]["context_max_utterances"]:]
            self.ids.add(item["id"])
            self.detect_queue.put_nowait((item, list(preceding)))
            return {"accepted": True, "duplicate": False, "utterance_id": item["id"]}

    def _complete(self, item, detection, decision, gate, **extra):
        self.audit.append("decision_completed", utterance=item, detection=detection,
                          decision=decision, gate=gate, **extra)

    def _model_failure(self, item, stage, exc, detection=None):
        action = "REQUIRE_PERMISSION" if isinstance(exc, PermissionNeeded) else "IGNORE"
        reason = str(exc) if isinstance(exc, PermissionNeeded) else "モデルの応答を検証できなかったため実行を見送りました。"
        self.audit.append("model_error", utterance_id=item["id"], stage=stage,
                          error_type=type(exc).__name__, detail=str(exc)[:200])
        self._complete(item, detection, None, {"action": action, "reason": reason, "operations": []},
                       context_interpretation="モデル接続またはスキーマ検証に失敗したため行動しません。")

    def _detect_worker(self):
        while True:
            task = self.detect_queue.get()
            if task is None:
                self.detect_queue.task_done()
                return
            item, preceding = task
            try:
                result = self.models.call("trigger", {"utterance": item, "preceding": preceding,
                                                       "aliases": self.config["aliases"]})
                detection = result.output
                self.audit.append("signal_detected", utterance_id=item["id"], detection=detection, metrics=result.metrics)
                if detection["trigger"] == "IGNORE":
                    self._complete(item, detection, None,
                                   {"action": "IGNORE", "reason": detection["reason"], "operations": []},
                                   context_interpretation="判断役が候補を出さなかったためメインモデルは呼び出していません。")
                else:
                    with self.condition:
                        if len(self.pending) >= self.config["app"]["queue_limit"]:
                            self._complete(item, detection, None, {"action": "IGNORE", "reason": "文脈判断の待ち行列が上限に達したため見送りました。", "operations": []})
                        else:
                            self.pending_sequence += 1
                            deadline = time.monotonic() + self.config["app"]["lookahead_seconds"]
                            heapq.heappush(self.pending, (deadline, self.pending_sequence, item, detection, preceding))
                            self.audit.append("context_review_scheduled", utterance_id=item["id"],
                                              lookahead_seconds=self.config["app"]["lookahead_seconds"])
                            self.condition.notify_all()
            except Exception as exc:
                self._model_failure(item, "trigger", exc)
            finally:
                self.detect_queue.task_done()

    def _policy_worker(self):
        while True:
            with self.condition:
                while not self.pending:
                    if self.closed:
                        return
                    self.condition.wait()
                delay = self.pending[0][0] - time.monotonic()
                if delay > 0 and not self.force_flush:
                    self.condition.wait(min(delay, 1))
                    continue
                _, _, item, detection, preceding = heapq.heappop(self.pending)
                self.active_policy += 1
                following = []
                found = False
                for u in self.context:
                    if found:
                        following.append(u)
                    if u["id"] == item["id"]:
                        found = True
                # If the target has rolled out, use timestamped subsequent context.
                if not found:
                    following = [u for u in self.context if parse_time(u["start_at"]) >= parse_time(item["end_at"])]
            try:
                payload = {"utterance": item, "detection": detection, "preceding": preceding, "following": following,
                           "meeting_goal": self.config["app"]["meeting_goal"], "prior_policy": self.config["app"]["prior_policy"],
                           "capabilities": ["show_private", "write_note", "isolated_prototype", "voice_session_stub"]}
                result = self.models.call("decision", payload)
                gate = asdict(enforce(detection, result.output))
                if gate["action"] == "START_PROTOTYPE":
                    with self.lock:
                        digest = hashlib.sha256(normalized(result.output["brief"], self.config).encode()).hexdigest()
                        if digest in self.prototype_fingerprints:
                            gate = {"action": "IGNORE", "reason": "同じアイデアをすでに試作しているため重複を見送りました。", "operations": []}
                        elif self.prototype_count >= self.config["app"]["prototype_limit_per_session"]:
                            gate = {"action": "IGNORE", "reason": "一回の会話の試作数が上限に達したため見送りました。", "operations": []}
                        elif time.monotonic() - self.last_prototype < self.config["app"]["prototype_cooldown_seconds"]:
                            gate = {"action": "IGNORE", "reason": "試作の連続起動を避ける待機時間中のため見送りました。", "operations": []}
                        else:
                            self.prototype_fingerprints.add(digest)
                            self.prototype_count += 1
                            self.last_prototype = time.monotonic()
                self._complete(item, detection, result.output, gate, preceding=preceding,
                               following=following, metrics=result.metrics)
                if gate["action"] == "START_PROTOTYPE":
                    prototype_id = uuid.uuid4().hex
                    request = self.audit.append("prototype_requested", prototype_id=prototype_id,
                                                utterance=item, detection=detection, decision=result.output, gate=gate,
                                                preceding=preceding, following=following)
                    self.prototype_queue.put_nowait((prototype_id, {**payload, "decision": result.output, "gate": gate,
                                                                   "request_event_id": request["event_id"]}))
                elif gate["action"] == "SUGGEST_PRIVATE":
                    note = {"id": uuid.uuid4().hex, "utterance_id": item["id"], "timestamp": item["start_at"],
                            "title": result.output["title"], "text": result.output["brief"], "reason": gate["reason"]}
                    self.audit.append("private_suggestion", note=note)
                    if "write_note" in gate["operations"]:
                        path = Path(self.config["app"]["runtime_dir"]) / "notes.jsonl"
                        with path.open("a", encoding="utf-8") as stream:
                            stream.write(canonical(note) + "\n")
                            stream.flush()
                            os.fsync(stream.fileno())
                        self.audit.append("note_updated", note_id=note["id"], path=str(path))
                elif gate["action"] == "CALL_VOICE_AI":
                    with self.lock:
                        if self.voice_busy:
                            self.audit.append("voice_skipped", utterance_id=item["id"], reason="音声セッションが起動中のため重複接続を見送りました。")
                        else:
                            self.voice_busy = True
                            self.voice_stop.clear()
                            self.voice_queue.put_nowait(payload)
                elif gate["action"] in {"REQUIRE_PERMISSION", "STUB"}:
                    self.audit.append("operation_blocked", utterance_id=item["id"], gate=gate,
                                      message="操作は実行されていません。MVPには公開・本番反映・削除の実行経路がありません。")
            except Exception as exc:
                self._model_failure(item, "main", exc, detection)
            finally:
                with self.condition:
                    self.active_policy -= 1
                    self.condition.notify_all()

    def _prototype_worker(self):
        while True:
            task = self.prototype_queue.get()
            if task is None:
                self.prototype_queue.task_done()
                return
            prototype_id, payload = task
            try:
                with self.lock:
                    revoked = prototype_id in self.revoked
                if revoked:
                    self.audit.append("prototype_canceled", prototype_id=prototype_id,
                                      utterance_id=payload["utterance"]["id"], reason="制作前に取り消されました。")
                else:
                    self.audit.append("prototype_started", prototype_id=prototype_id, utterance_id=payload["utterance"]["id"])
                    result = self.sandbox.run(prototype_id, payload)
                    self.audit.append("prototype_ready", utterance_id=payload["utterance"]["id"], **result)
            except Exception as exc:
                self.audit.append("prototype_failed", prototype_id=prototype_id, utterance_id=payload["utterance"]["id"],
                                  error_type=type(exc).__name__, reason=str(exc)[:200],
                                  path=str(self.sandbox.root / prototype_id))
            finally:
                self.prototype_queue.task_done()

    def _voice_worker(self):
        while True:
            task = self.voice_queue.get()
            if task is None:
                self.voice_queue.task_done()
                return
            source_id = task["utterance"]["id"]
            try:
                self.audit.append("voice_requested", utterance_id=source_id)
                result = self.voice.run(task, self.voice_stop,
                                        lambda output, metrics: self.audit.append("voice_ready", utterance_id=source_id,
                                                                                  output=output, metrics=metrics))
                self.audit.append("voice_closed", utterance_id=source_id, **result)
            except Exception as exc:
                self.audit.append("voice_failed", utterance_id=source_id, error_type=type(exc).__name__, reason=str(exc)[:200])
            finally:
                with self.lock:
                    self.voice_busy = False
                self.voice_queue.task_done()

    def revoke(self, prototype_id):
        with self.lock:
            known = any(e["kind"] == "prototype_requested" and e["data"]["prototype_id"] == prototype_id for e in self.audit.events)
            if not known:
                raise ValueError("unknown prototype")
            if prototype_id not in self.revoked:
                self.audit.append("prototype_revoked", prototype_id=prototype_id,
                                  reason="本人が試作を取り消しました。", files_deleted=False)
                self.revoked.add(prototype_id)
        return {"revoked": True, "files_deleted": False}

    def active_artifact(self, prototype_id):
        with self.lock:
            if prototype_id in self.revoked:
                return None
            for event in reversed(self.audit.events):
                if event["kind"] == "prototype_ready" and event["data"]["prototype_id"] == prototype_id:
                    directory = self.sandbox.root / prototype_id
                    if directory.is_symlink() or not directory.is_dir() or directory.resolve().parent != self.sandbox.root.resolve():
                        return None
                    return directory
        return None

    def flush(self, timeout=None):
        if timeout is None:
            timeout = max(120, sum(self.config[role]["timeout_seconds"] for role in ("trigger", "main", "coder", "voice")) + 30)
        end = time.monotonic() + timeout
        while self.detect_queue.unfinished_tasks:
            if time.monotonic() > end:
                raise TimeoutError("detector did not finish")
            time.sleep(0.01)
        with self.condition:
            self.force_flush = True
            self.condition.notify_all()
            while self.pending or self.active_policy:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    self.force_flush = False
                    raise TimeoutError("context review did not finish")
                self.condition.wait(min(remaining, 0.1))
            self.force_flush = False
        for work in (self.prototype_queue, self.voice_queue):
            while work.unfinished_tasks:
                if time.monotonic() > end:
                    raise TimeoutError("execution did not finish")
                time.sleep(0.01)

    def state(self):
        events = self.audit.snapshot()
        timeline, prototypes, suggestions, voice, errors = OrderedDict(), {}, [], [], []
        for event in events:
            data = event["data"]
            kind = event["kind"]
            if kind == "utterance_received":
                u = data["utterance"]
                timeline[u["id"]] = {"utterance": u, "status": "検出中"}
            elif kind == "signal_detected" and data["utterance_id"] in timeline:
                timeline[data["utterance_id"]].update({"detection": data["detection"], "trigger_metrics": data["metrics"], "status": "文脈を確認中"})
            elif kind == "decision_completed":
                source_id = data["utterance"]["id"]
                if source_id in timeline:
                    timeline[source_id].update({"decision": data.get("decision"), "gate": data["gate"],
                                               "context_interpretation": data.get("context_interpretation"),
                                               "status": data["gate"]["action"], "decision_event_id": event["event_id"]})
            elif kind == "prototype_requested":
                prototypes[data["prototype_id"]] = {"prototype_id": data["prototype_id"], "status": "制作待ち", "utterance": data["utterance"], "gate": data["gate"]}
            elif kind in {"prototype_started", "prototype_ready", "prototype_failed", "prototype_canceled", "prototype_revoked"}:
                prototype_id = data["prototype_id"]
                if prototype_id in prototypes:
                    prototypes[prototype_id].update(data)
                    prototypes[prototype_id]["status"] = {"prototype_started": "制作中", "prototype_ready": "完成", "prototype_failed": "失敗", "prototype_canceled": "取消済み", "prototype_revoked": "取消済み"}[kind]
            elif kind == "private_suggestion":
                suggestions.append(data["note"])
            elif kind in {"voice_ready", "voice_closed", "voice_failed", "voice_turn"}:
                voice.append({"kind": kind, **data})
            elif kind in {"model_error", "audio_error", "input_rejected"}:
                errors.append({"kind": kind, **data})
        for prototype_id in self.revoked:
            if prototype_id in prototypes:
                prototypes[prototype_id]["status"] = "取消済み"
        return {"revision": len(events), "session_id": self.audit.session_id,
                "timeline": list(timeline.values())[-120:], "prototypes": list(prototypes.values())[-30:],
                "suggestions": suggestions[-30:], "voice": voice[-20:], "errors": errors[-10:],
                "input_source": self.config["input"]["source"], "voice_busy": self.voice_busy,
                "audio_ready": self.config["input"]["source"] in {"browser", "ffmpeg"},
                "transcription_provider": self.config["input"].get("provider", "whisper_local"),
                "models": {k: self.config[k].get("model", "") for k in ("trigger", "main", "coder", "voice")},
                "adapters": {k: self.config[k]["provider"] for k in ("trigger", "main", "coder", "voice")}}

    def close(self):
        self.voice_stop.set()
        self.flush()
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        for work in (self.detect_queue, self.prototype_queue, self.voice_queue):
            work.put(None)
        for thread in self.threads:
            thread.join(timeout=5)
        self.audit.append("session_closed")
        self.audit.close()
