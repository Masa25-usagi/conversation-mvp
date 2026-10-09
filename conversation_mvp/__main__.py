import argparse
import json
import shutil
import sys
from pathlib import Path

from .audit import verify
from .config import load
from .contracts import ROOT
from .core import Core
from .evaluation import build, evaluate, extract
from .server import replay, serve


def main():
    parser = argparse.ArgumentParser(description="会話から個人向け提案と隔離試作を作るMVP")
    parser.add_argument("--config", default=str(ROOT / "config.toml"))
    parser.add_argument("--runtime", help="isolated output directory override")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="start the private UI")
    replay_parser = commands.add_parser("replay", help="replay a transcript without recording")
    replay_parser.add_argument("path", nargs="?", default=str(ROOT / "fixtures/demo.jsonl"))
    replay_parser.add_argument("--acknowledge-input-requirements", action="store_true")
    eval_parser = commands.add_parser("evaluate", help="compare detector or pipeline on labeled JSONL")
    eval_parser.add_argument("path")
    eval_parser.add_argument("--candidate", action="append", metavar="NAME=CONFIG", required=True)
    eval_parser.add_argument("--output", default="../conversation-mvp-runtime/evaluation.json")
    eval_parser.add_argument("--location", default="未指定")
    eval_parser.add_argument("--stage", choices=["detector", "pipeline"], default="detector")
    eval_parser.add_argument("--repeat", type=int, default=1)
    eval_parser.add_argument("--require-real", action="store_true")
    eval_parser.add_argument("--balanced", action="store_true")
    extract_parser = commands.add_parser("dataset-extract", help="extract unlabeled cases from an audit log")
    extract_parser.add_argument("audit")
    extract_parser.add_argument("output")
    build_parser = commands.add_parser("dataset-build", help="build real 10x4 data from human annotations")
    build_parser.add_argument("candidates")
    build_parser.add_argument("labels")
    build_parser.add_argument("output")
    verify_parser = commands.add_parser("audit-verify", help="verify the persistent audit hash chain")
    verify_parser.add_argument("path")
    commands.add_parser("doctor", help="show configured adapters and local requirements without API calls")
    args = parser.parse_args()
    try:
        if args.command == "audit-verify":
            print(json.dumps({"valid": True, "events": len(verify(args.path))}, ensure_ascii=False))
        elif args.command == "dataset-extract":
            print(json.dumps({"candidates": extract(args.audit, args.output), "output": args.output}, ensure_ascii=False))
        elif args.command == "dataset-build":
            print(json.dumps({"real_cases": build(args.candidates, args.labels, args.output), "output": args.output}, ensure_ascii=False))
        elif args.command == "evaluate":
            candidates = []
            for value in args.candidate:
                name, sep, path = value.partition("=")
                if not sep or not name or not path:
                    raise ValueError("candidate must be NAME=CONFIG")
                candidates.append((name, path))
            result = evaluate(args.path, candidates, args.output, args.location, args.stage, args.repeat, args.require_real, args.balanced)
            print(json.dumps({"output": args.output, "provenance": result["provenance_counts"],
                              "candidates": [{k:v for k,v in c.items() if k not in {"rows", "confusion"}} for c in result["candidates"]]}, ensure_ascii=False, indent=2))
        else:
            config = load(args.config)
            if args.runtime:
                config["app"]["runtime_dir"] = str(Path(args.runtime).resolve())
            if args.command == "doctor":
                print(json.dumps({"python": sys.version.split()[0], "ffmpeg": bool(shutil.which("ffmpeg")),
                                  "input": config["input"]["source"], "adapters": {k:config[k]["provider"] for k in ("trigger","main","coder","voice")},
                                  "whisper_server": "local server/model must be installed separately; not probed",
                                  "external_model_data_allowed": config["permissions"]["external_model_data"],
                                  "paid_model_calls_allowed": config["permissions"]["paid_model_calls"]}, ensure_ascii=False, indent=2))
            elif args.command == "serve":
                serve(Core(config))
            elif args.command == "replay":
                core = Core(config)
                try:
                    if args.acknowledge_input_requirements:
                        core.acknowledge_input("jsonl", True)
                    replay(core, Path(args.path))
                    state = core.state()
                    print(json.dumps({"timeline_count": len(state["timeline"]),
                                      "prototypes": state["prototypes"], "voice": state["voice"],
                                      "audit": str(core.audit.path)}, ensure_ascii=False, indent=2))
                finally:
                    core.close()
    except (ValueError, OSError, TimeoutError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
