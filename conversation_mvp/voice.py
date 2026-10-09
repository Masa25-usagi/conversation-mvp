"""Finite on-demand lifecycle. Provider audio transport belongs to the bridge."""

import time
import base64
import threading
import uuid

from .contracts import utc_now, validate_output
from .models import measured, request_json


class Voice:
    def __init__(self, config):
        self.config = config
        self.turn_lock = threading.Lock()
        self.history = []

    def turn(self, payload, core):
        if self.config["voice"]["provider"] != "hermes_nous":
            raise ValueError("音声AIの実接続が未設定です。")
        if not self.turn_lock.acquire(blocking=False):
            raise ValueError("音声AIが返答中です。少し待ってください。")
        try:
            from .hermes_bridge import invoke
            value = {"operation": "turn", "model": self.config["voice"]["model"],
                     "history": self.history, "language": "ja"}
            if "text" in payload:
                text = payload["text"]
                if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                    raise ValueError("音声AIへの入力は1〜2000文字にしてください。")
                value["text"] = text.strip()
            else:
                if self.config["input"]["source"] != "browser":
                    raise ValueError("音声会話にはブラウザのマイク入力を有効にしてください。")
                if payload.get("source") != "browser_mic" or "browser_mic" not in core.acknowledged:
                    raise ValueError("マイク入力の確認が必要です。")
                if payload.get("sample_rate") != 16000:
                    raise ValueError("audio must be mono PCM16LE at 16000 Hz")
                pcm = base64.b64decode(payload["pcm_base64"], validate=True)
                if len(pcm) % 2 or not 6400 <= len(pcm) <= 480000:
                    raise ValueError("録音は0.2〜15秒にしてください。")
                from .audio import rms
                if rms(pcm) < self.config["input"]["min_rms"]:
                    raise ValueError("音声が小さすぎます。マイクに向かって話してください。")
                value["pcm_base64"] = payload["pcm_base64"]
            result = invoke(value, self.config["permissions"])
            self.history = (self.history + [{"role": "user", "content": result["text"]},
                                           {"role": "assistant", "content": result["reply"]}])[-12:]
            # Only text/metadata enters the audit. Neither input nor output audio is retained.
            core.audit.append("voice_turn", text=result["text"], reply=result["reply"],
                              model=result["model"], tts_model=result["tts_model"],
                              latency_ms=result["latency_ms"], usage=result["usage"], api_cost=None)
            return result
        finally:
            self.turn_lock.release()

    def run(self, payload, stop_event, on_ready):
        settings = self.config["voice"]
        started = time.perf_counter()
        if settings["provider"] == "hermes_nous":
            result = {"session_id": uuid.uuid4().hex, "ready": False,
                      "summary": "音声会話の接続を設定済みです。「音声AIと話す」から録音して送信できます。"}
            metrics = {"measurement_kind": "turn_based", "audio_transport": "browser PCM → Groq → Nous Portal → MP3",
                       "api_cost": None, "currency": "USD", "ready_latency_ms": None}
            on_ready(result, metrics)
            return {"result": result, "metrics": metrics, "closed": True}
        if settings["provider"] == "stub":
            result = {"session_id": uuid.uuid4().hex, "ready": False,
                      "summary": "音声プロバイダが未設定のため接続スタブを記録しました。"}
            metrics = measured(settings, time.perf_counter() - started, known_free=True)
            metrics.update({"measurement_kind": "stub", "opened_at": utc_now(), "session_duration_ms": 0,
                            "ready_latency_ms": None, "audio_transport": "not connected"})
            on_ready(result, metrics)
            return {"result": result, "metrics": metrics, "closed": True}
        if settings["provider"] != "json_http":
            raise ValueError("unsupported voice provider")
        opened_at = utc_now()
        response = request_json(settings["url"], {"operation": "open", "input": payload,
                                "max_session_seconds": settings["session_seconds"],
                                "auto_speak": False, "external_post": False}, settings, self.config["permissions"])
        result = validate_output(response["output"], "voice")
        metrics = measured(settings, time.perf_counter() - started, response.get("usage"))
        metrics.update({"measurement_kind": "bridge", "opened_at": opened_at,
                        "ready_latency_ms": round((time.perf_counter() - started) * 1000, 3) if result["ready"] else None,
                        "audio_transport": "delegated to configured bridge"})
        try:
            on_ready(result, metrics)
            if result["ready"]:
                stop_event.wait(settings["session_seconds"])
        finally:
            # The bridge must also enforce its own TTL, even if this client dies.
            closing = request_json(settings["url"], {"operation": "close", "session_id": result["session_id"]},
                                   settings, self.config["permissions"])
        if closing.get("closed") is not True:
            raise ValueError("voice bridge did not confirm session closure")
        final_usage = closing.get("usage")
        metrics = {**metrics, **measured(settings, time.perf_counter() - started, final_usage)}
        metrics["session_duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        metrics["closed_at"] = utc_now()
        return {"result": result, "metrics": metrics, "closed": True}
