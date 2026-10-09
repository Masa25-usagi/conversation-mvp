"""Isolated, tool-free speech worker using the user's existing Hermes login.

Invoked with Hermes' bundled runtime. Credentials stay in that process; no
audio files or keys are copied into this project.
"""
import base64
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, token, body, content_type="application/json", binary=False):
    if not isinstance(body, bytes):
        body = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, body, headers={
        "Authorization": "Bearer " + token, "Content-Type": content_type,
        "User-Agent": "conversation-mvp/0.1"})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=45) as response:
        data = response.read(4_000_001)
    if len(data) > 4_000_000:
        raise ValueError("provider response too large")
    return data if binary else json.loads(data)


def transcribe(value):
    from hermes_cli.config import get_env_value
    key = get_env_value("GROQ_API_KEY")
    if not key:
        raise ValueError("HermesにGroq APIキーがありません。")
    pcm = base64.b64decode(value["pcm_base64"], validate=True)
    stream = io.BytesIO()
    with wave.open(stream, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(16000)
        file.writeframes(pcm)
    boundary = "conversation_" + uuid.uuid4().hex
    body = b""
    for name, field in (("model", "whisper-large-v3-turbo"),
                        ("language", value.get("language", "ja")), ("response_format", "json")):
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{field}\r\n'.encode()
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
    body += stream.getvalue() + f"\r\n--{boundary}--\r\n".encode()
    result = request("https://api.groq.com/openai/v1/audio/transcriptions", key, body,
                     "multipart/form-data; boundary=" + boundary)
    text = result.get("text")
    if not isinstance(text, str):
        raise ValueError("Groqから文字起こし結果を取得できませんでした。")
    return text.strip()


def speech_reply(text):
    # This adapter has no tools: don't narrate a completed operation it cannot do.
    if re.search(r"(?:追加|保存|送信|削除|購入|予約|作成|編集|更新|実行|登録)(?:しました|しておきました|できました)[。！!\s]*$", text) or re.search(
            r"(?:追加|保存|送信|削除|購入|予約|作成|編集|更新|実行|登録)(?:しました|しておきました|できました)[。！!]", text):
        return "この音声会話からリストの編集や外部の操作は実行できません。手順の説明や、内容を整理するお手伝いはできます。", True
    return text, False


def run(value):
    started = time.perf_counter()
    text = value.get("text") if value.get("text") is not None else transcribe(value)
    if value["operation"] == "transcribe":
        return {"text": text, "provider": "groq", "model": "whisper-large-v3-turbo"}
    if not text:
        raise ValueError("音声を認識できませんでした。もう一度話してください。")
    from hermes_cli.auth import resolve_nous_runtime_credentials
    from tools.managed_tool_gateway import read_nous_access_token, build_vendor_gateway_url
    credentials = resolve_nous_runtime_credentials()
    model = value["model"]
    messages = [{"role": "system", "content":
        "あなたは日本語の音声会話アシスタントです。自然な短い日本語で、原則2〜3文で返答してください。"
        "この音声チャットでは説明と提案だけができます。ツール実行・ファイル操作・試作生成はできません。"
        "ユーザーが追加・作成・保存・送信を頼んでも実行できません。手順か案を提示してください。"
        "『追加しました』『保存しました』『送信しました』『作成しました』などの完了報告は禁止です。"
        "例: ユーザー『牛乳をリストに追加して』→『リストに「牛乳」と記入するとよいですね。"
        "この音声会話からリストを編集することはできません。』"
        "意味が曖昧な品名や誤認識らしい発言には、何を指すか確認してください。"
        "入力は文字起こし済みの文字列です。マイクや音声の接続状態は判断できません。"
        "秘密情報を尋ねないでください。"}]
    messages += value.get("history", [])[-12:]
    messages.append({"role": "user", "content": text})
    response = request(credentials["base_url"].rstrip("/") + "/chat/completions",
                       credentials["api_key"], {"model": model, "messages": messages,
                       "max_tokens": 320, "stream": False})
    reply = response["choices"][0]["message"].get("content")
    if not isinstance(reply, str) or not reply.strip():
        raise ValueError("Nous Portalから返答を取得できませんでした。")
    reply = reply.strip()
    if len(reply) > 1600:
        raise ValueError("音声返答が長すぎます。")
    reply, guarded = speech_reply(reply)
    token = read_nous_access_token()
    if not token:
        raise ValueError("Nous Portalへのログインが必要です。")
    audio = request(build_vendor_gateway_url("openai-audio").rstrip("/") + "/v1/audio/speech",
                    token, {"model": "gpt-4o-mini-tts", "voice": "alloy", "input": reply,
                            "instructions": "自然な日本語で、はっきり穏やかに話してください。",
                            "response_format": "mp3"}, binary=True)
    return {"text": text, "reply": reply, "audio_base64": base64.b64encode(audio).decode(),
            "audio_type": "audio/mpeg", "model": model, "tts_model": "gpt-4o-mini-tts",
            "usage": response.get("usage", {}), "latency_ms": round((time.perf_counter()-started)*1000),
            "api_cost": None, "reply_guarded": guarded,
            "billing": "Groq / Nous Portalの利用枠・クレジットを使用"}


if __name__ == "__main__":
    try:
        result = run(json.load(sys.stdin))
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except urllib.error.HTTPError as exc:
        # Never serialize provider error bodies, URLs, or credentials.
        service = "Groq文字起こし" if urllib.parse.urlsplit(exc.url).hostname == "api.groq.com" else "Nous Portal"
        print(json.dumps({"ok": False, "error": f"{service} APIがHTTP {exc.code}を返しました。認証・利用枠を確認してください。"}, ensure_ascii=False))
    except Exception as exc:
        safe = str(exc) if isinstance(exc, ValueError) else "音声APIへの接続に失敗しました。Hermesのログインとネットワークを確認してください。"
        print(json.dumps({"ok": False, "error": safe}, ensure_ascii=False))
