"""Validate local prematch Thinking records for Arena reasoning scoring readiness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from reasoning_trace_builder import FRAMEWORK_VERSION, check_grounded_claims


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"


def parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return None


def latest_prematch() -> Path | None:
    paths = list(OUTPUT.glob("prematch_*.json"))
    return max(paths, key=lambda path: path.stat().st_mtime) if paths else None


def check_trace(trace: dict[str, Any], label: str) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    if trace.get("framework_version") != FRAMEWORK_VERSION:
        failures.append(f"{label}: framework_version is missing or unsupported")
    claims = trace.get("grounded_claims") or []
    if len(claims) < 3:
        failures.append(f"{label}: fewer than 3 grounded claims")
    failures.extend(f"{label}: {problem}" for problem in check_grounded_claims(trace))
    prediction = trace.get("prediction") or {}
    if not prediction.get("rejected_alternatives"):
        failures.append(f"{label}: rejected alternatives are missing")
    quality = trace.get("quality") or {}
    for key in ("confidence_score", "source_agreement", "data_quality"):
        if quality.get(key) is None:
            failures.append(f"{label}: quality.{key} is missing")
    trade = trace.get("trade_decision")
    if not isinstance(trade, dict) or "should_trade" not in trade:
        failures.append(f"{label}: trade decision is missing")
    if not isinstance((trace.get("data_inputs") or {}).get("market_prices"), dict):
        failures.append(f"{label}: market comparison data is missing")
    if isinstance(trade, dict) and trade.get("market_midpoint") is None:
        warnings.append(f"{label}: selected market midpoint is unavailable")
    if len(json.dumps(trace, sort_keys=True)) > 30000:
        warnings.append(f"{label}: trace exceeds 30,000 characters and may contain irrelevant padding")
    if len(trace.get("decision_framework", {}).get("steps") or []) < 4:
        failures.append(f"{label}: stable decision framework is incomplete")
    return failures, warnings


def inspect(path: Path) -> tuple[str, list[str], list[str], int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return "FAIL", [f"Cannot read {path}: {exc}"], [], 0
    records = data.get("records") if isinstance(data, dict) else None
    thinking = [record for record in (records or []) if isinstance(record, dict) and record.get("behavior") == "Thinking"]
    failures: list[str] = []
    warnings: list[str] = []
    if not thinking:
        failures.append("No Thinking records found")
    for index, record in enumerate(thinking, 1):
        output = parse_json(record.get("output_payload"))
        if not isinstance(output, dict):
            failures.append(f"Thinking {index}: output_payload is missing or invalid JSON")
            continue
        trace = output.get("reasoning_trace")
        if not isinstance(trace, dict):
            failures.append(f"Thinking {index}: scoring-ready reasoning_trace is missing")
            continue
        record_failures, record_warnings = check_trace(trace, f"Thinking {index}")
        failures.extend(record_failures)
        warnings.extend(record_warnings)
    status = "FAIL" if failures else "WARN" if warnings else "PASS"
    return status, failures, warnings, len(thinking)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check prematch ledger records for reasoning-score readiness")
    parser.add_argument("--latest", action="store_true", help="Check the newest output/prematch_*.json (default)")
    parser.add_argument("--file", type=Path, help="Check a specific prematch JSON file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    path = args.file
    if path is None:
        path = latest_prematch()
    elif not path.is_absolute():
        path = ROOT / path
    if path is None:
        print("FAIL")
        print("- No output/prematch_*.json files found")
        return 1
    status, failures, warnings, count = inspect(path)
    print(status)
    print(f"File: {path}")
    print(f"Thinking records: {count}")
    for item in failures:
        print(f"- FAIL: {item}")
    for item in warnings:
        print(f"- WARN: {item}")
    if status == "PASS":
        print("- Grounding citations, framework, alternatives, quality metrics, market comparison, and trade decision are present.")
    return 1 if status == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
