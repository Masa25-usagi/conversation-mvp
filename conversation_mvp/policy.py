"""Capabilities are enforced after, and independently of, model judgments."""

from dataclasses import dataclass

RESTRICTED = {"external_send", "publish", "charge", "production_write", "data_delete"}
STUB_ONLY = {"meeting_speech", "external_post"}
REQUIRED = {"IGNORE": set(), "SUGGEST_PRIVATE": {"show_private"},
            "START_PROTOTYPE": {"isolated_prototype", "show_private"},
            "CALL_VOICE_AI": {"voice_session"}, "REQUIRE_PERMISSION": set()}
ALLOWED_BY_TRIGGER = {
    "IGNORE": {"IGNORE"},
    "SUGGEST_PRIVATE": {"IGNORE", "SUGGEST_PRIVATE", "REQUIRE_PERMISSION"},
    "TRIGGER_PROTOTYPE": {"IGNORE", "SUGGEST_PRIVATE", "START_PROTOTYPE", "REQUIRE_PERMISSION"},
    "CALL_VOICE_AI": {"IGNORE", "SUGGEST_PRIVATE", "CALL_VOICE_AI", "REQUIRE_PERMISSION"},
    "REQUIRE_PERMISSION": {"IGNORE", "REQUIRE_PERMISSION"},
}


@dataclass
class GateResult:
    action: str
    reason: str
    operations: list


def enforce(detection, decision):
    operations = set(decision["operations"]) | REQUIRED[decision["action"]]
    if operations & RESTRICTED:
        return GateResult("REQUIRE_PERMISSION", "明示的許可が必要な操作を実行前の権限検査で止めました。", sorted(operations))
    if operations & STUB_ONLY:
        return GateResult("STUB", "会議への発話と外部投稿はMVPのスタブとして記録するだけです。", sorted(operations))
    if decision["action"] not in ALLOWED_BY_TRIGGER[detection["trigger"]]:
        return GateResult("IGNORE", "判断役が検出していない種類の実行をメインモデルが要求したため見送りました。", sorted(operations))
    action = decision["action"]
    permitted = REQUIRED[action] | ({"write_note"} if action == "SUGGEST_PRIVATE" else set())
    if action != "REQUIRE_PERMISSION" and operations - permitted:
        return GateResult("IGNORE", "決定した行動に不要な権限が要求されたため見送りました。", sorted(operations))
    return GateResult(action, decision["reason"], sorted(operations))
