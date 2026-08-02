"""Audit local PolyStair reasoning artifacts without calling any API."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
JSON_OUT = OUTPUT / "reasoning_trace_audit_latest.json"
MD_OUT = OUTPUT / "reasoning_trace_audit_latest.md"
PATTERNS = (
    "prematch_*.json",
    "ledger_trace_*.json",
    "dashboard_status_latest.json",
    "ledger_validity*.json",
    "post_qualification_report*.json",
    "qualification_ladder_candidates*.json",
)


def parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def read_json(path: Path, warnings: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        warnings.append(f"Skipped {path.name}: {exc}")
        return None


def walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def find_records(data: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in walk(data):
        if node.get("behavior") not in {"Observing", "ToolCalling", "Thinking", "Acting"}:
            continue
        key = str(node.get("record_id") or id(node))
        if key not in seen:
            seen.add(key)
            records.append(node)
    return records


def first_dict(data: Any, key: str) -> dict[str, Any]:
    if isinstance(data, dict) and isinstance(data.get(key), dict):
        return data[key]
    for node in walk(data):
        if isinstance(node.get(key), dict):
            return node[key]
    return {}


def fixture_id(data: Any, records: list[dict[str, Any]]) -> str:
    if isinstance(data, dict) and data.get("fixture_id") is not None:
        return str(data["fixture_id"])
    for node in walk(data):
        if node.get("fixture_id") is not None:
            return str(node["fixture_id"])
    for record in records:
        session = str(record.get("session_id") or "")
        parts = session.split(":")
        if len(parts) > 1 and parts[1].isdigit():
            return parts[1]
    return "unknown"


def match_name(data: Any, fixture: str) -> str:
    candidates = []
    for node in walk(data):
        for key in ("match", "fixture", "sportmonks_match_name"):
            value = node.get(key)
            if isinstance(value, str) and " vs" in value.lower():
                candidates.append(value)
    return candidates[0] if candidates else fixture


def submission_state(data: Any) -> tuple[bool, bool, Any]:
    ledger = data.get("ledger") if isinstance(data, dict) else None
    candidates = [ledger] if isinstance(ledger, dict) else []
    candidates.extend(node for node in walk(data) if "submitted" in node and isinstance(node, dict))
    for candidate in candidates:
        submitted = bool(candidate.get("submitted"))
        response = candidate.get("response") or candidate.get("response_body") or candidate.get("body")
        response = parse_json(response)
        errors = response.get("errors") if isinstance(response, dict) else None
        succeeded = submitted and (errors == [] or candidate.get("success") is True)
        return submitted, succeeded, response
    return False, False, None


def output_quality(record: dict[str, Any]) -> tuple[bool, bool, Any, int]:
    payload = parse_json(record.get("output_payload"))
    if not isinstance(payload, dict):
        return False, True, payload, 0
    encoded = json.dumps(payload, sort_keys=True)
    trace = payload.get("reasoning_trace")
    reasoning = payload.get("reasoning")
    meaningful = bool(
        isinstance(trace, dict) and len(trace.get("grounded_claims") or []) >= 3
        or isinstance(reasoning, dict) and len(encoded) >= 350
        or isinstance(payload.get("prediction"), dict) and len(encoded) >= 500
        or isinstance(payload.get("strategy"), dict) and len(encoded) >= 500
    )
    thin = not meaningful or len(encoded) < 350
    return meaningful, thin, payload, len(encoded)


def extract_artifact(path: Path, data: Any) -> list[dict[str, Any]]:
    records = find_records(data)
    if not records:
        return []
    prediction = first_dict(data, "prediction")
    strategy = first_dict(data, "strategy")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        session = str(record.get("session_id") or f"file:{path.name}")
        grouped.setdefault(session, []).append(record)
    submitted, submission_succeeded, response = submission_state(data)
    results = []
    for session, session_records in grouped.items():
        thinking = [record for record in session_records if record.get("behavior") == "Thinking"]
        acting = [record for record in session_records if record.get("behavior") == "Acting"]
        qualities = [output_quality(record) for record in thinking]
        usable = bool(thinking) and all(item[0] for item in qualities)
        thin_count = sum(1 for item in qualities if item[1])
        traces = [item[2].get("reasoning_trace") for item in qualities if isinstance(item[2], dict) and isinstance(item[2].get("reasoning_trace"), dict)]
        acting_trade = any(record.get("action_type") in {"open_order_intent", "order", "trade"} for record in acting)
        fixture = fixture_id(data, session_records)
        score = (
            (3 if usable else 0)
            + min(3, len(thinking))
            + (2 if traces else 0)
            + (1 if acting_trade else 0)
            + (1 if submission_succeeded else 0)
        )
        results.append(
            {
                "source_file": path.name,
                "session_id": session,
                "fixture_id": fixture,
                "match": match_name(data, fixture),
                "prediction_outcome": prediction.get("outcome"),
                "probabilities": prediction.get("probabilities"),
                "source_agreement": prediction.get("source_agreement"),
                "confidence_score": prediction.get("confidence_score"),
                "data_quality": prediction.get("data_quality"),
                "bookmaker_consensus": (prediction.get("components") or {}).get("bookmaker_consensus"),
                "sportmonks_probabilities": (prediction.get("components") or {}).get("sportmonks_1x2"),
                "supabase_priors": (prediction.get("components") or {}).get("supabase_priors"),
                "market_prices": (first_dict(data, "polymarket_digest").get("implied_win_prob")),
                "selected_trade": strategy.get("execution_outcome") or strategy.get("outcome"),
                "edge_pp": strategy.get("edge_pp"),
                "thinking_record_count": len(thinking),
                "acting_record_count": len(acting),
                "acting_has_trade_decision": acting_trade,
                "usable_reasoning": usable,
                "thin_thinking_count": thin_count,
                "scoring_ready_trace_count": len(traces),
                "thinking_output_payloads": [item[2] for item in qualities],
                "ledger_submitted": submitted,
                "ledger_submission_succeeded": submission_succeeded,
                "ledger_response": response,
                "example_score": score,
            }
        )
    return results


def markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# PolyStair Reasoning Trace Audit",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "## Summary",
        "",
        f"- Total traces found: **{summary['total_traces']}**",
        f"- Traces with usable reasoning: **{summary['usable_reasoning']}**",
        f"- Traces missing reasoning: **{summary['missing_reasoning']}**",
        f"- Traces submitted to ledger: **{summary['submitted_to_ledger']}**",
        f"- Successful ledger submissions: **{summary['successful_submissions']}**",
        f"- Submitted traces with usable legacy reasoning: **{summary['submitted_with_usable_reasoning']}**",
        f"- Submitted traces with scoring-ready v1 reasoning: **{summary['submitted_with_scoring_ready_reasoning']}**",
        f"- Traces with empty/thin Thinking records: **{summary['thin_traces']}**",
        f"- Scoring-ready v1 traces: **{summary['scoring_ready_traces']}**",
        "",
        "## Top 10 Examples To Share",
        "",
        "| Rank | Fixture | Match | File | Thinking | Scoring-ready | Submitted | Score |",
        "|---:|---|---|---|---:|---:|---:|---:|",
    ]
    for index, row in enumerate(report["top_examples"], 1):
        lines.append(
            f"| {index} | {row['fixture_id']} | {str(row['match']).replace('|', '/')} | {row['source_file']} | "
            f"{row['thinking_record_count']} | {row['scoring_ready_trace_count']} | "
            f"{'yes' if row['ledger_submitted'] else 'no'} | {row['example_score']}/10 |"
        )
    lines.extend(["", "## Missing Or Thin", ""])
    weak = [row for row in report["traces"] if not row["usable_reasoning"] or row["thin_thinking_count"]]
    if not weak:
        lines.append("No empty or thin traces found under the audit heuristic.")
    else:
        for row in weak[:30]:
            lines.append(
                f"- `{row['source_file']}` fixture `{row['fixture_id']}`: "
                f"Thinking={row['thinking_record_count']}, thin={row['thin_thinking_count']}, "
                f"scoring-ready={row['scoring_ready_trace_count']}."
            )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Historical traces may contain useful JSON while still lacking Arena's new explicit grounding structure. "
            "A local trace does not prove that Arena accepted or scored it; only successful ledger submission evidence is counted above.",
        ]
    )
    if report["warnings"]:
        lines.extend(["", "## Warnings", "", *(f"- {item}" for item in report["warnings"])])
    return "\n".join(lines) + "\n"


def main() -> int:
    warnings: list[str] = []
    paths: list[Path] = []
    seen: set[Path] = set()
    for pattern in PATTERNS:
        for path in OUTPUT.glob(pattern):
            if path not in seen:
                seen.add(path)
                paths.append(path)
    discovered: list[dict[str, Any]] = []
    for path in sorted(paths):
        data = read_json(path, warnings)
        if data is not None:
            discovered.extend(extract_artifact(path, data))
    by_session: dict[str, dict[str, Any]] = {}
    for row in discovered:
        session = row["session_id"]
        existing = by_session.get(session)
        if existing is None:
            row["source_files"] = [row["source_file"]]
            by_session[session] = row
            continue
        source_files = list(dict.fromkeys(existing.get("source_files", []) + [row["source_file"]]))
        preferred = row if row["example_score"] > existing["example_score"] else existing
        preferred["source_files"] = source_files
        preferred["ledger_submitted"] = bool(existing["ledger_submitted"] or row["ledger_submitted"])
        preferred["ledger_submission_succeeded"] = bool(
            existing["ledger_submission_succeeded"] or row["ledger_submission_succeeded"]
        )
        if preferred.get("ledger_response") is None:
            preferred["ledger_response"] = existing.get("ledger_response") or row.get("ledger_response")
        by_session[session] = preferred
    traces = list(by_session.values())
    ranked = sorted(traces, key=lambda row: (row["example_score"], row["thinking_record_count"]), reverse=True)
    top: list[dict[str, Any]] = []
    used_fixtures: set[str] = set()
    for row in ranked:
        if row["fixture_id"] not in used_fixtures:
            top.append(row)
            used_fixtures.add(row["fixture_id"])
        if len(top) == 10:
            break
    if len(top) < 10:
        top.extend(row for row in ranked if row not in top)
        top = top[:10]
    summary = {
        "files_scanned": len(paths),
        "record_copies_found": len(discovered),
        "total_traces": len(traces),
        "usable_reasoning": sum(bool(row["usable_reasoning"]) for row in traces),
        "missing_reasoning": sum(not row["thinking_record_count"] for row in traces),
        "submitted_to_ledger": sum(bool(row["ledger_submitted"]) for row in traces),
        "successful_submissions": sum(bool(row["ledger_submission_succeeded"]) for row in traces),
        "submitted_with_usable_reasoning": sum(
            bool(row["ledger_submitted"] and row["usable_reasoning"]) for row in traces
        ),
        "submitted_with_scoring_ready_reasoning": sum(
            bool(row["ledger_submitted"] and row["scoring_ready_trace_count"]) for row in traces
        ),
        "thin_traces": sum(bool(row["thin_thinking_count"]) for row in traces),
        "scoring_ready_traces": sum(bool(row["scoring_ready_trace_count"]) for row in traces),
    }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "top_examples": top,
        "traces": traces,
        "warnings": warnings,
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    MD_OUT.write_text(markdown(report), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Wrote {JSON_OUT.relative_to(ROOT)}")
    print(f"Wrote {MD_OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
