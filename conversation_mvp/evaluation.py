"""Balanced JSONL data, real-conversation extraction and honest model comparison."""

import json
import platform
import statistics
import tempfile
import time
from collections import Counter
from pathlib import Path

from .audit import verify
from .config import load
from .contracts import strict_json, utterance, utc_now
from .models import Models
from .policy import enforce

CLASSES = {"wanted_suggestion": "SUGGEST_PRIVATE", "wanted_silence": "IGNORE",
           "good_prototype": "TRIGGER_PROTOTYPE", "should_ignore": "IGNORE"}
ACTION_TRIGGER = {"START_PROTOTYPE": "TRIGGER_PROTOTYPE", "STUB": "REQUIRE_PERMISSION"}


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    yield strict_json(line)
                except ValueError as exc:
                    raise ValueError(f"invalid JSONL at line {number}: {exc}") from None


def write_jsonl(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")


def dataset(path, require_real=False, require_balanced=False):
    result = list(read_jsonl(path))
    ids = set()
    for item in result:
        if set(item) != {"id", "class", "expected_trigger", "utterance", "preceding", "following", "provenance", "source_reference", "annotator"}:
            raise ValueError("evaluation case keys do not match the documented contract")
        if not isinstance(item["id"], str) or not item["id"] or item["id"] in ids:
            raise ValueError("evaluation ids must be unique strings")
        ids.add(item["id"])
        if item["class"] not in CLASSES or item["expected_trigger"] != CLASSES[item["class"]]:
            raise ValueError("class and expected trigger disagree; human annotation is required")
        utterance(item["utterance"])
        if not isinstance(item["preceding"], list) or not isinstance(item["following"], list):
            raise ValueError("context must be an utterance array")
        for u in item["preceding"] + item["following"]:
            utterance(u)
        if item["provenance"] not in {"real", "synthetic"} or item["provenance"] != item["utterance"]["provenance"]:
            raise ValueError("dataset provenance must match its utterance")
        if require_real and item["provenance"] != "real":
            raise ValueError("synthetic examples cannot be scored as real-conversation evaluation")
        if not isinstance(item["source_reference"], str) or not item["source_reference"]:
            raise ValueError("source_reference required")
        if not isinstance(item["annotator"], str) or not item["annotator"]:
            raise ValueError("annotator required")
    counts = Counter(item["class"] for item in result)
    if not result:
        raise ValueError("empty evaluation dataset")
    if require_balanced and any(counts[name] != 10 for name in CLASSES):
        raise ValueError("balanced evaluation requires exactly ten cases in each of the four classes")
    return result


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    position = (len(values)-1)*fraction
    lower, upper = int(position), min(int(position)+1, len(values)-1)
    return round(values[lower] + (values[upper]-values[lower])*(position-lower), 3)


def evaluate(path, candidates, output, location="未指定", stage="detector", repeat=1, require_real=False, require_balanced=False):
    cases = dataset(path, require_real, require_balanced)
    if not candidates or not 1 <= repeat <= 20:
        raise ValueError("provide candidates and repeat 1..20")
    report = {"created_at": utc_now(), "dataset": str(Path(path).resolve()),
              "provenance_counts": dict(Counter(x["provenance"] for x in cases)),
              "class_counts": dict(Counter(x["class"] for x in cases)), "stage": stage, "repeat": repeat,
              "execution_location": location, "location_basis": "operator supplied, not inferred",
              "host_platform": platform.platform(),
              "definitions": {"miss_rate": "desired trigger differs from prediction among wanted_suggestion/good_prototype, including errors",
                              "overreaction_rate": "non-IGNORE prediction among wanted_silence/should_ignore; errors counted separately",
                              "api_cost": "configured token rates only; unknown is null; local electricity/hardware excluded"},
              "candidates": []}
    for name, config_path in candidates:
        config = load(config_path, read_env=False)
        models, rows = Models(config), []
        for iteration in range(repeat):
            for case in cases:
                started = time.perf_counter()
                metrics, prediction, error = [], "ERROR", None
                try:
                    detection = models.call("trigger", {"utterance": case["utterance"], "preceding": case["preceding"], "aliases": config["aliases"]})
                    metrics.append(detection.metrics)
                    prediction = detection.output["trigger"]
                    if stage == "pipeline" and prediction != "IGNORE":
                        decision = models.call("decision", {"utterance": case["utterance"], "detection": detection.output,
                                               "preceding": case["preceding"], "following": case["following"],
                                               "meeting_goal": config["app"]["meeting_goal"], "prior_policy": config["app"]["prior_policy"]})
                        metrics.append(decision.metrics)
                        gate = enforce(detection.output, decision.output)
                        prediction = ACTION_TRIGGER.get(gate.action, gate.action)
                except Exception as exc:
                    prediction, error = "ERROR", {"type": type(exc).__name__, "detail": str(exc)[:200]}
                elapsed = (time.perf_counter()-started)*1000
                costs = [m["api_cost"] for m in metrics]
                currencies = {m["currency"] for m in metrics}
                known = bool(metrics) and error is None and None not in costs and len(currencies) == 1
                rows.append({"case_id": case["id"], "class": case["class"], "iteration": iteration+1,
                             "expected": case["expected_trigger"], "prediction": prediction,
                             "latency_ms": round(elapsed, 3), "error": error, "model_calls": metrics,
                             "api_cost": sum(costs) if known else None,
                             "currency": next(iter(currencies)) if len(currencies)==1 else None})
        positives = [r for r in rows if r["class"] in {"wanted_suggestion", "good_prototype"}]
        negatives = [r for r in rows if r["class"] in {"wanted_silence", "should_ignore"}]
        misses = sum(r["prediction"] != r["expected"] for r in positives)
        extra = sum(r["prediction"] not in {"IGNORE", "ERROR"} for r in negatives)
        failures = sum(r["error"] is not None for r in rows)
        confusion = Counter((r["expected"], r["prediction"]) for r in rows)
        costs = [r["api_cost"] for r in rows]
        currencies = {r["currency"] for r in rows}
        known = None not in costs and len(currencies) == 1 and None not in currencies
        successful_latencies = [r["latency_ms"] for r in rows if not r["error"]]
        report["candidates"].append({"name": name, "provider": config["trigger"]["provider"], "model": config["trigger"]["model"],
            "status": "completed" if not failures else ("failed_all" if failures == len(rows) else "partial"),
            "misses": misses, "positive_cases": len(positives), "miss_rate": misses/len(positives) if positives else None,
            "overreactions": extra, "negative_cases": len(negatives), "overreaction_rate": extra/len(negatives) if negatives else None,
            "error_rate": failures/len(rows), "latency_p50_ms": percentile(successful_latencies, .5),
            "latency_p95_ms": percentile(successful_latencies, .95), "total_api_cost": sum(costs) if known else None,
            "currency": next(iter(currencies)) if known else None,
            "confusion": [{"expected": a, "predicted": b, "count": count} for (a,b), count in sorted(confusion.items())], "rows": rows})
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return report


def extract(audit_path, output, window=4):
    events = verify(audit_path)
    sessions = {}
    for event in events:
        if event["kind"] == "utterance_received" and event["data"]["utterance"]["source"] != "button":
            sessions.setdefault(event["session_id"], []).append(event["data"]["utterance"])
    candidates = []
    for session, items in sessions.items():
        for index, u in enumerate(items):
            candidates.append({"id": u["id"], "class": None, "expected_trigger": None, "utterance": u,
                               "preceding": items[max(0,index-window):index], "following": items[index+1:index+1+window],
                               "provenance": u["provenance"], "source_reference": f"audit:{session}:{u['id']}", "annotator": None})
    write_jsonl(output, candidates)
    return len(candidates)


def build(candidates_path, labels_path, output, require_real=True):
    labels = strict_json(Path(labels_path).read_text(encoding="utf-8"))
    if not isinstance(labels, dict):
        raise ValueError("labels must map utterance ids to class and annotator")
    selected, found = [], set()
    for case in read_jsonl(candidates_path):
        label = labels.get(case["id"])
        if not label:
            continue
        if set(label) != {"class", "annotator"} or label["class"] not in CLASSES:
            raise ValueError("each human label must contain class and annotator")
        case.update(label)
        case["expected_trigger"] = CLASSES[label["class"]]
        selected.append(case)
        found.add(case["id"])
    if set(labels) != found:
        raise ValueError("labels contain unknown candidate ids")
    # Validate before replacing an existing output.
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Own a new temporary file; never overwrite/delete a caller's .pending file.
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix="evaluation-", suffix=".jsonl", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        write_jsonl(temporary, selected)
        dataset(temporary, require_real=require_real, require_balanced=True)
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return len(selected)
