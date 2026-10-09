"""Bounded subprocess bridge; Hermes supplies auth, never agent tool execution."""
import json
import shutil
import subprocess
import threading
from pathlib import Path

_slots = threading.BoundedSemaphore(2)


def invoke(value, permissions):
    if not permissions.get("external_model_data"):
        raise PermissionError("外部APIへの音声・テキスト送信が無効です。")
    if not permissions.get("paid_model_calls"):
        raise PermissionError("音声APIの利用枠・クレジット使用が無効です。")
    if not _slots.acquire(blocking=False):
        raise ValueError("音声APIを処理中です。少し待ってから再送してください。")
    try:
        executable = shutil.which("hermes")
        if not executable:
            candidate = Path.home() / ".local/bin/hermes"
            executable = str(candidate) if candidate.is_file() else None
        if not executable:
            raise ValueError("Hermes Agentが見つかりません。")
        runtime = subprocess.run([executable, "--print-runtime-command"],
                                 capture_output=True, text=True, timeout=15, check=True)
        command = json.loads(runtime.stdout)
        prefix, separator, _ = command[-1].partition("; runpy.run_module(")
        if not separator or command[-2] != "-c":
            raise ValueError("Hermesの実行形式が変わりました。接続設定を確認してください。")
        worker = Path(__file__).with_name("hermes_worker.py").resolve()
        command[-1] = prefix + "; runpy.run_path(" + repr(str(worker)) + ", run_name='__main__')"
        completed = subprocess.run(command, input=json.dumps(value, ensure_ascii=False),
                                   capture_output=True, text=True, timeout=150)
        if completed.returncode:
            raise ValueError("Hermes音声接続を起動できませんでした。")
        result = json.loads(completed.stdout)
        if result.get("ok") is not True:
            raise ValueError(result.get("error", "音声APIの接続に失敗しました。"))
        return result["result"]
    except subprocess.TimeoutExpired:
        raise TimeoutError("音声APIの応答が時間内に届きませんでした。") from None
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        raise ValueError("Hermes音声接続を起動できませんでした。") from None
    finally:
        _slots.release()
