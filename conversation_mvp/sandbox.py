"""File-only coding agent: no generated program runs on the host."""

import hashlib
import html
import json
import os
import re
import time
from pathlib import Path, PurePosixPath

from .contracts import validate_output
from .models import ModelResult, measured


def template_bundle(brief):
    title = "アイデアのミニ試作"
    if "散歩" in brief or "自然" in brief:
        title = "今日の散歩を決める"
        body = '<p>気分に合わせて、近所で試せる散歩のテーマを選びます。</p><button id="choose">散歩のテーマを選ぶ</button><p id="result" aria-live="polite"></p><small>実在する経路や天候は取得していません。</small>'
        script = "const themes=['木々の多い道を15分歩く','川沿いで空を眺める','いつもと違う道を一つ曲がる','公園で葉の色を探す'];document.querySelector('#choose').onclick=()=>{document.querySelector('#result').textContent=themes[Math.floor(Math.random()*themes.length)]};"
    elif "タイマー" in brief or "集中" in brief:
        title = "一つのことに集中する"
        body = '<label>時間を選んでください <select id="minutes"><option value="5">5分</option><option value="15">15分</option><option value="25">25分</option></select></label><p id="clock" aria-live="polite">05:00</p><button id="start">始める</button><button id="stop">止める</button>'
        script = "let timer=null,remaining=300;const draw=()=>document.querySelector('#clock').textContent=String(Math.floor(remaining/60)).padStart(2,'0')+':'+String(remaining%60).padStart(2,'0');document.querySelector('#start').onclick=()=>{clearInterval(timer);remaining=Number(document.querySelector('#minutes').value)*60;draw();timer=setInterval(()=>{remaining--;draw();if(remaining<=0)clearInterval(timer)},1000)};document.querySelector('#stop').onclick=()=>clearInterval(timer);"
    else:
        title = "アイデアを次の一歩にする"
        body = '<p>仮説を確かめるために、次の行動を一つずつ書き出します。</p><form id="add"><label>次の行動 <input id="task" maxlength="120" required></label><button>追加する</button></form><ul id="list"></ul><small>保存や送信は行いません。この画面で試すためのリストです。</small>'
        script = "document.querySelector('#add').onsubmit=e=>{e.preventDefault();const input=document.querySelector('#task'),li=document.createElement('li'),label=document.createElement('label'),check=document.createElement('input');check.type='checkbox';label.append(check,document.createTextNode(' '+input.value));li.append(label);document.querySelector('#list').append(li);input.value=''};"
    page = '<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+title+'</title><style>body{font-family:system-ui,sans-serif;max-width:620px;margin:40px auto;padding:24px;color:#1a3830;background:#f8faf6}h1{font-size:28px}button,input,select{font:inherit;border:1px solid #bbcec0;border-radius:8px;padding:12px;margin:6px}button{background:#205645;color:white;cursor:pointer}small{display:block;color:#567267}#result,#clock{font-size:30px}li{margin:12px 0}.source{border-left:3px solid #b8c8bb;padding:12px}</style><h1>'+title+'</h1><p class="source">この発言から作りました：'+html.escape(brief)+'</p>'+body+'<script>'+script+'</script></html>'
    return {"summary": f"「{title}」をローカルのテンプレートで試作しました。", "files": [{"path": "index.html", "content": page}]}


def validate_bundle(bundle, settings):
    validate_output(bundle, "coder")
    paths, total = set(), 0
    if len(bundle["files"]) > settings["max_files"]:
        raise ValueError("too many generated files")
    for item in bundle["files"]:
        name = item["path"]
        path = PurePosixPath(name)
        # Restrict to plain file names: no traversal, hidden files or executables.
        if "/" in name or "\\" in name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*\.(html|css|js|json|md|txt)", name):
            raise ValueError("generated path is outside file-only capability")
        if path.name in paths or path.name in {"manifest.json", "provenance.md"}:
            raise ValueError("duplicate or reserved generated path")
        paths.add(path.name)
        total += len(item["content"].encode())
    if "index.html" not in paths or total > settings["max_total_bytes"]:
        raise ValueError("bundle requires index.html and must fit byte budget")
    return bundle


class Sandbox:
    def __init__(self, config, models):
        self.root = Path(config["app"]["runtime_dir"]) / "prototypes"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.settings, self.models = config["coder"], models

    def run(self, prototype_id, payload):
        if not re.fullmatch(r"[a-f0-9]{32}", prototype_id):
            raise ValueError("invalid prototype id")
        started = time.perf_counter()
        directory = self.root / prototype_id
        # This directory is never the existing working tree and must be new.
        directory.mkdir(mode=0o700)
        if self.settings["provider"] == "template":
            result = ModelResult(template_bundle(payload["decision"]["brief"]),
                                 measured(self.settings, time.perf_counter() - started, known_free=True))
        else:
            result = self.models.call("coder", payload)
        bundle = validate_bundle(result.output, self.settings)
        hashes = {}
        for item in bundle["files"]:
            path = directory / item["path"]
            # Exclusive write also prevents accidental overwrites.
            with path.open("x", encoding="utf-8") as stream:
                stream.write(item["content"])
            hashes[item["path"]] = hashlib.sha256(path.read_bytes()).hexdigest()
        source = payload["utterance"]
        provenance = (f"# この発言から作りました\n\n- 発言ID: {source['id']}\n- 時刻: {source['start_at']} — {source['end_at']}\n"
                      f"- 元の発言: {source['text']}\n- 判断役: {payload['detection']['reason']}\n"
                      f"- 文脈解釈: {payload['decision']['context_interpretation']}\n"
                      f"- 実行理由: {payload['gate']['reason']}\n- 監査イベント: {payload['request_event_id']}\n"
                      f"- 生成方式: {self.settings['provider']}\n\nこの試作は公開・本番反映されていません。\n")
        (directory / "provenance.md").write_text(provenance, encoding="utf-8")
        manifest = {"prototype_id": prototype_id, "trigger_utterance": source,
                    "detection": payload["detection"], "decision": payload["decision"], "gate": payload["gate"],
                    "request_event_id": payload["request_event_id"], "files_sha256": hashes,
                    "summary": bundle["summary"], "generation": result.metrics,
                    "execution": "file-only; no generated code executed on host"}
        manifest_path = directory / "manifest.json"
        with manifest_path.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        return {"prototype_id": prototype_id, "path": str(directory), "entrypoint": str(directory / "index.html"),
                "manifest": str(manifest_path), "summary": bundle["summary"],
                "files_sha256": hashes, "metrics": result.metrics,
                "total_latency_ms": round((time.perf_counter() - started) * 1000, 3)}
