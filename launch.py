"""Double-click launcher for the local conversation app."""

import argparse
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from conversation_mvp.config import load
from conversation_mvp.core import Core
from conversation_mvp.server import serve
from conversation_mvp.models import is_local


def ensure_local_models(config):
    settings = [config[role] for role in ("trigger", "main", "coder")
                if config[role]["provider"] == "ollama" and is_local(config[role]["url"])]
    for origin in {urllib.parse.urlsplit(s["url"]).netloc for s in settings}:
        tags_url = f"http://{origin}/api/tags"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def get_models():
            with opener.open(tags_url, timeout=2) as response:
                return json.load(response)["models"]

        try:
            models = get_models()
        except urllib.error.URLError:
            executable = shutil.which("ollama")
            if not executable or origin not in {"127.0.0.1:11434", "localhost:11434"}:
                raise ValueError("Ollamaを起動してから、もう一度このアプリを開いてください。") from None
            runtime = Path(config["app"]["runtime_dir"])
            runtime.mkdir(parents=True, exist_ok=True)
            with (runtime / "ollama-server.log").open("a") as log:
                subprocess.Popen([executable, "serve"], stdout=log, stderr=log, start_new_session=True)
            for _ in range(20):
                time.sleep(0.25)
                try:
                    models = get_models()
                    break
                except urllib.error.URLError:
                    continue
            else:
                raise ValueError("Ollama APIを起動できませんでした。ollama-server.logを確認してください。")
        available = {m["name"]: m for m in models}
        for setting in settings:
            if urllib.parse.urlsplit(setting["url"]).netloc != origin:
                continue
            name = setting["model"]
            if name not in available:
                raise ValueError(f"モデルがありません。ターミナルで ollama pull {name} を実行してください。")
            if available[name].get("remote_host") and not config["permissions"]["external_model_data"]:
                raise ValueError("クラウドモデルです。会話の外部送信が許可されていません。")


def main():
    parser = argparse.ArgumentParser(description="会話アプリを起動して画面を開きます")
    parser.add_argument("--no-browser", action="store_true")
    active_config = Path(__file__).parent / "configs/active-ollama.toml"
    parser.add_argument("--config", default=str(active_config) if active_config.exists() else None)
    args = parser.parse_args()
    config = load(args.config)
    url = f"http://127.0.0.1:{config['app']['port']}"

    # Repeated double-clicks reopen the running app instead of acquiring its log.
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            page = response.read(64000).decode("utf-8")
        if '<title>会話から、もう一歩。</title>' not in page:
            raise ValueError("設定したポートを別のアプリが使用しています。config.tomlのportを変更してください。")
        print(f"起動済みの画面: {url}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    except urllib.error.URLError:
        pass

    ensure_local_models(config)
    core = Core(config)
    ready = threading.Event()

    def open_when_ready():
        for _ in range(50):
            if ready.wait(0.2):
                return
            try:
                with urllib.request.urlopen(url, timeout=0.5) as response:
                    if response.status == 200:
                        webbrowser.open(url)
                        return
            except (OSError, urllib.error.URLError):
                continue

    if not args.no_browser:
        threading.Thread(target=open_when_ready, daemon=True).start()
    try:
        serve(core)
    finally:
        ready.set()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"起動できませんでした: {exc}")
        input("Enterで閉じます。")
        raise SystemExit(1)
