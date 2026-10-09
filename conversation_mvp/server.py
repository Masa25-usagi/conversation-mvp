"""Loopback-only private UI and replaceable JSON input/output boundary."""

import json
import mimetypes
import secrets
import threading
import time
import urllib.parse
import uuid
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .audio import Audio, FFmpegCapture
from .contracts import ROOT, parse_time, strict_json, utc_now


def replay(core, path, interval=0, flush=True):
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = strict_json(line)
        # New replay session, same transcript timestamps and explicit provenance.
        item["id"] = uuid.uuid4().hex
        core.ingest(item)
        if interval:
            time.sleep(interval)
    if flush:
        core.flush()


def make_server(core, port=None):
    token = secrets.token_urlsafe(32)
    audio = None
    capture = None
    demo_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # No tokens, conversation contents or credentials in HTTP logs.

        def _host(self):
            accepted = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in accepted:
                raise ValueError("invalid Host header")

        def _same_origin(self):
            self._host()
            origin = self.headers.get("Origin")
            allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if origin is not None and origin not in allowed:
                raise PermissionError("cross-origin request refused")

        def _authorized(self):
            self._same_origin()
            if not secrets.compare_digest(self.headers.get("X-MVP-Token", ""), token):
                raise PermissionError("session token required")

        def _send(self, status, payload, kind="application/json; charset=utf-8", artifact=False):
            if not isinstance(payload, bytes):
                payload = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if artifact:
                self.send_header("Content-Security-Policy", "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'")
            else:
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self'; connect-src 'self'; media-src 'self' blob:; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            try:
                self._host()
                path = urllib.parse.urlsplit(self.path).path
                if path == "/":
                    boot = json.dumps({"token": token, "chunkSeconds": core.config["input"]["chunk_seconds"]})
                    body = (ROOT / "web/index.html").read_text(encoding="utf-8").replace("/*BOOT*/", "window.MVP=" + boot + ";")
                    return self._send(200, body.encode(), "text/html; charset=utf-8")
                if path == "/api/session":
                    # Same bootstrap token already embedded in the root HTML.
                    # Keep Host/Origin checks; all other APIs still require it.
                    self._same_origin()
                    return self._send(200, {"token": token})
                if path in {"/app.js", "/style.css", "/pcm-worklet.js"}:
                    file = ROOT / "web" / path[1:]
                    kind = "text/css" if file.suffix == ".css" else "text/javascript"
                    return self._send(200, file.read_bytes(), kind + "; charset=utf-8")
                if path.startswith("/artifact/"):
                    parts = path.split("/")
                    if len(parts) != 4:
                        return self._send(404, {"error": "not found"})
                    directory = core.active_artifact(parts[2])
                    name = parts[3]
                    if not directory or name not in {"index.html", "manifest.json", "provenance.md"}:
                        return self._send(404, {"error": "not found"})
                    file = directory / name
                    if file.is_symlink() or not file.is_file() or file.resolve().parent != directory.resolve():
                        return self._send(404, {"error": "not found"})
                    kind = mimetypes.guess_type(name)[0] or "text/plain"
                    return self._send(200, file.read_bytes(), kind + "; charset=utf-8", artifact=True)
                self._authorized()
                if path == "/api/state":
                    return self._send(200, core.state())
                if path == "/api/requirements":
                    return self._send(200, strict_json((ROOT / "docs/input-requirements.json").read_text(encoding="utf-8")))
                if path == "/api/audit":
                    return self._send(200, {"events": core.audit.snapshot()[-500:]})
                return self._send(404, {"error": "not found"})
            except PermissionError as exc:
                self._send(403, {"error": str(exc)})
            except ValueError as exc:
                self._send(400, {"error": str(exc)})

        def do_POST(self):
            try:
                self._authorized()
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("application/json required")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 800000:
                    raise ValueError("invalid request size")
                body = strict_json(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
                path = urllib.parse.urlsplit(self.path).path
                if path == "/api/utterance":
                    now = utc_now()
                    item = {"id": body.get("id", uuid.uuid4().hex), "text": body["text"],
                            "start_at": body.get("start_at", now), "end_at": body.get("end_at", now),
                            "source": "manual", "provenance": body.get("provenance", "real")}
                    return self._send(202, core.ingest(item))
                if path == "/api/input/acknowledge":
                    core.acknowledge_input(body["source"], body.get("reviewed"))
                    return self._send(200, {"acknowledged": True})
                if path == "/api/audio":
                    return self._send(202, audio.submit(body))
                if path == "/api/capture/start":
                    return self._send(200, capture.start())
                if path == "/api/capture/stop":
                    return self._send(200, capture.stop())
                if path == "/api/prototype/revoke":
                    return self._send(200, core.revoke(body["prototype_id"]))
                if path == "/api/voice/stop":
                    core.voice_stop.set()
                    return self._send(200, {"stop_requested": True})
                if path == "/api/voice/turn":
                    return self._send(200, core.voice.turn(body, core))
                if path == "/api/voice/call":
                    now = utc_now()
                    # Button requests still pass through the common judgment/policy path.
                    alias = core.config["aliases"]["assistant"][0]
                    return self._send(202, core.ingest({"id": uuid.uuid4().hex, "text": f"{alias}、相談に来て。",
                                                       "start_at": now, "end_at": now, "source": "button", "provenance": "real"}))
                if path == "/api/demo":
                    if not demo_lock.acquire(blocking=False):
                        raise ValueError("demo is already running")
                    def demo():
                        try:
                            replay(core, ROOT / "fixtures/demo.jsonl", interval=0.35)
                        finally:
                            demo_lock.release()
                    threading.Thread(target=demo, daemon=True).start()
                    return self._send(202, {"started": True, "provenance": "synthetic"})
                if path == "/api/flush":
                    audio.drain()
                    core.flush()
                    return self._send(200, {"finished": True})
                return self._send(404, {"error": "not found"})
            except PermissionError as exc:
                self._send(403, {"error": str(exc)})
            except (ValueError, KeyError, TypeError) as exc:
                self._send(400, {"error": str(exc)})
            except TimeoutError as exc:
                self._send(504, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port if port is not None else core.config["app"]["port"]), Handler)
    audio = Audio(core)
    capture = FFmpegCapture(audio)
    server.daemon_threads = True
    server.mvp_audio = audio
    server.mvp_capture = capture
    server.mvp_token = token
    return server


def serve(core):
    try:
        server = make_server(core)
    except BaseException:
        core.close()
        raise
    print(f"個人用画面: http://127.0.0.1:{server.server_port}", flush=True)
    print("音声は開始ボタンを押すまで取得しません。Ctrl+Cで終了します。", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if server.mvp_capture.process:
            server.mvp_capture.stop()
        server.server_close()
        server.mvp_audio.close()
        core.close()
