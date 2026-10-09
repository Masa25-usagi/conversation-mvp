"""PCM capture/transcription adapters; raw audio is never retained by the core."""

import array
import base64
import io
import math
import queue
import subprocess
import sys
import threading
import time
import uuid
import wave
from collections import deque
from datetime import timedelta

from .contracts import parse_time, utc_now
from .models import request_json


def pcm_wav(pcm, sample_rate=16000):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(sample_rate)
        file.writeframes(pcm)
    return stream.getvalue()


def rms(pcm):
    values = array.array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    return math.sqrt(sum(v * v for v in values) / max(1, len(values))) / 32768


def multipart(wav, language):
    boundary = "conversation_" + uuid.uuid4().hex
    pieces = []
    for key, value in (("response_format", "json"), ("language", language), ("temperature", "0.0")):
        pieces.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    pieces.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="chunk.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode() + wav + b"\r\n")
    pieces.append(f"--{boundary}--\r\n".encode())
    return b"".join(pieces), f"multipart/form-data; boundary={boundary}"


class Audio:
    def __init__(self, core):
        self.core = core
        self.config = core.config
        self.queue = queue.Queue(8)
        self.last = {}
        self.thread = threading.Thread(target=self._worker, daemon=True, name="local-transcription")
        self.thread.start()

    def submit(self, value):
        source = value.get("source")
        if source not in {"browser_mic", "browser_system", "browser_mixed", "ffmpeg"}:
            raise ValueError("unknown audio source")
        if source not in self.core.acknowledged:
            raise ValueError("input requirements have not been acknowledged")
        mode = self.config["input"]["source"]
        if (source == "ffmpeg" and mode != "ffmpeg") or (source != "ffmpeg" and mode != "browser"):
            raise ValueError("enable the matching input source in config first")
        if value.get("sample_rate") != 16000:
            raise ValueError("audio must be mono PCM16LE at 16000 Hz")
        parse_time(value["start_at"])
        pcm = value.get("pcm")
        if pcm is None:
            pcm = base64.b64decode(value["pcm_base64"], validate=True)
        if not isinstance(pcm, bytes) or len(pcm) % 2 or not 6400 <= len(pcm) <= 480000:
            raise ValueError("audio chunk must contain 0.2..15 seconds of PCM")
        if self.queue.full():
            self.core.audit.append("audio_error", source=source, reason="文字起こしが追い付かないため音声入力を一時停止してください。")
            raise ValueError("transcription queue is full; pause capture")
        self.queue.put_nowait({"pcm": pcm, "source": source, "start_at": value["start_at"]})
        return {"accepted": True, "queued": self.queue.qsize()}

    def _worker(self):
        settings = self.config["input"]
        while True:
            chunk = self.queue.get()
            if chunk is None:
                self.queue.task_done()
                return
            try:
                pcm = chunk["pcm"]
                seconds = len(pcm) / 32000
                start = parse_time(chunk["start_at"])
                if rms(pcm) < settings["min_rms"]:
                    self.last.pop(chunk["source"], None)
                    self.core.audit.append("audio_silence", source=chunk["source"], start_at=chunk["start_at"],
                                           duration_seconds=seconds, reason="音量が無音の閾値を下回ったため文字起こしを呼び出しませんでした。")
                    continue
                started = time.perf_counter()
                if settings.get("provider") == "hermes_groq":
                    from .hermes_bridge import invoke
                    response = invoke({"operation": "transcribe", "language": settings["language"],
                                       "pcm_base64": base64.b64encode(pcm).decode()}, self.config["permissions"])
                else:
                    body, content_type = multipart(pcm_wav(pcm), settings["language"])
                    response = request_json(settings["whisper_url"], body, settings, self.config["permissions"], content_type)
                if not isinstance(response, dict) or not isinstance(response.get("text"), str):
                    raise ValueError("whisper-server JSON response must contain text")
                text = response["text"].strip()
                # Only suppress identical overlapping/retried recognition near in time.
                prior = self.last.get(chunk["source"])
                duplicate = prior and prior[0] == text and (start - prior[1]).total_seconds() < 1
                self.core.audit.append("audio_transcribed", source=chunk["source"], start_at=chunk["start_at"],
                                       duration_seconds=seconds, latency_ms=round((time.perf_counter()-started)*1000, 3),
                                       timestamp_precision="capture chunk window, not word alignment", duplicate=bool(duplicate))
                if text and not duplicate:
                    self.last[chunk["source"]] = (text, start)
                    self.core.ingest({"id": uuid.uuid4().hex, "text": text, "start_at": start.isoformat(),
                                      "end_at": (start+timedelta(seconds=seconds)).isoformat(),
                                      "source": chunk["source"], "provenance": "real"})
            except Exception as exc:
                self.core.audit.append("audio_error", source=chunk["source"], error_type=type(exc).__name__, reason=str(exc)[:200])
            finally:
                self.queue.task_done()

    def drain(self, timeout=120):
        end = time.monotonic() + timeout
        while self.queue.unfinished_tasks:
            if time.monotonic() > end:
                raise TimeoutError("audio queue did not finish")
            time.sleep(0.01)

    def close(self):
        self.drain()
        self.queue.put(None)
        self.thread.join(timeout=5)


class FFmpegCapture:
    def __init__(self, audio):
        self.audio = audio
        self.process = None
        self.thread = None
        self.stopping = threading.Event()
        self.errors = deque(maxlen=8)

    def start(self):
        if self.process and self.process.poll() is None:
            raise ValueError("capture is already active")
        if "ffmpeg" not in self.audio.core.acknowledged:
            raise ValueError("acknowledge ffmpeg input requirements first")
        if self.audio.config["input"]["source"] != "ffmpeg":
            raise ValueError("set input.source to ffmpeg first")
        args = self.audio.config["input"]["ffmpeg_args"]
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError("ffmpeg_args must be an argument array")
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *args,
                   "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"]
        self.stopping.clear()
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, shell=False)
        process = self.process
        threading.Thread(target=lambda: self._stderr(process), daemon=True).start()
        self.thread = threading.Thread(target=lambda: self._read(process), daemon=True)
        self.thread.start()
        self.audio.core.audit.append("audio_capture_started", source="ffmpeg")
        return {"started": True}

    def _stderr(self, process):
        for line in iter(process.stderr.readline, b""):
            self.errors.append(line.decode(errors="replace")[-500:])

    def _read(self, process):
        start = parse_time(utc_now())
        size = int(self.audio.config["input"]["chunk_seconds"] * 32000)
        try:
            while not self.stopping.is_set():
                pcm = process.stdout.read(size)
                if not pcm:
                    break
                if len(pcm) >= 6400:
                    self.audio.submit({"pcm": pcm, "start_at": start.isoformat(), "source": "ffmpeg", "sample_rate": 16000})
                start += timedelta(seconds=len(pcm)/32000)
            code = process.wait(timeout=3) if process.poll() is not None else None
            if code not in (None, 0) and not self.stopping.is_set():
                raise ValueError("ffmpeg capture failed; check the configured device: " + "".join(self.errors)[-500:])
        except Exception as exc:
            self.audio.core.audit.append("audio_error", source="ffmpeg", reason=str(exc)[:500])
            if process.poll() is None:
                process.terminate()

    def stop(self):
        self.stopping.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.thread:
            self.thread.join(timeout=4)
        self.audio.core.audit.append("audio_capture_stopped", source="ffmpeg")
        return {"stopped": True}
