import copy
import os
import tomllib
from pathlib import Path

from .contracts import ROOT


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip().replace("_", "").isalnum():
            raise ValueError("invalid .env line")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def load(path=None, read_env=True):
    path = Path(path or ROOT / "config.toml").resolve()
    if read_env:
        load_env(ROOT / ".env")
        if path.parent != ROOT:
            load_env(path.parent / ".env")
    config = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    custom = tomllib.loads(path.read_text(encoding="utf-8"))
    for section, values in custom.items():
        config.setdefault(section, {}).update(values)
    mapping = {"MVP_INPUT_SOURCE": ("input", "source"), "MVP_RUNTIME_DIR": ("app", "runtime_dir"),
               "MVP_WHISPER_URL": ("input", "whisper_url")}
    for role in ("trigger", "main", "coder", "voice"):
        for key in ("provider", "model", "url"):
            mapping[f"MVP_{role.upper()}_{key.upper()}"] = (role, key)
    if read_env:
        for env, (section, key) in mapping.items():
            if env in os.environ:
                config[section][key] = os.environ[env]
    runtime = Path(config["app"]["runtime_dir"]).expanduser()
    if not runtime.is_absolute():
        runtime = ROOT / runtime
    config["app"]["runtime_dir"] = str(runtime.resolve())
    if config["app"]["host"] != "127.0.0.1":
        raise ValueError("MVP server is restricted to 127.0.0.1")
    if config["input"]["source"] not in {"demo", "browser", "ffmpeg", "jsonl"}:
        raise ValueError("unknown input source")
    if config["input"]["sample_rate"] != 16000 or not 1 <= config["input"]["chunk_seconds"] <= 15:
        raise ValueError("audio requires 16000 Hz and 1..15 second chunks")
    if not 0 <= config["app"]["lookahead_seconds"] <= 30:
        raise ValueError("lookahead_seconds must be 0..30")
    if not 0 < config["voice"]["session_seconds"] <= 60:
        raise ValueError("voice session must be 0..60 seconds")
    if not config["aliases"]["assistant"] or any(not a.strip() for a in config["aliases"]["assistant"]):
        raise ValueError("configure at least one nonempty wake alias")
    if any(not k.strip() for k in config["voice"]["auto_keywords"]):
        raise ValueError("voice auto keywords must be nonempty")
    for key in ("context_seconds", "context_max_utterances", "queue_limit", "prototype_limit_per_session"):
        if type(config["app"][key]) is not int or config["app"][key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    if not 0 <= config["app"]["prototype_cooldown_seconds"] <= 3600:
        raise ValueError("invalid prototype cooldown")
    for role in ("trigger", "main", "coder", "voice"):
        if not 0 < config[role]["timeout_seconds"] <= 300:
            raise ValueError("model timeout must be 0..300 seconds")
        if type(config[role]["billable"]) is not bool:
            raise ValueError("billable must be boolean")
        for key, maximum in (("num_ctx", 32768), ("num_predict", 8192)):
            if key in config[role] and (type(config[role][key]) is not int or not 1 <= config[role][key] <= maximum):
                raise ValueError(f"{key} must be a positive integer <= {maximum}")
    for value in config["permissions"].values():
        if type(value) is not bool:
            raise ValueError("permissions must be boolean")
    return copy.deepcopy(config)
