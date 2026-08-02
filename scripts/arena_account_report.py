"""Read-only Arena wallet, exposure, order, and settlement report."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
ENV_PATH = ROOT / ".env"
API_PREFIX = "/api/v1"
FILLED_STATUSES = {"completed", "filled"}
SETTLED_STATUSES = {"settled", "closed"}
REJECTED_STATUSES = {"rejected", "unfilled"}
NAME_KEYS = {
    "match",
    "fixture",
    "sportmonks_match_name",
    "home_team",
    "away_team",
    "home_country",
    "away_country",
    "home_short_code",
    "away_short_code",
}


def safe_float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def money(value: Any) -> str:
    number = safe_float(value, None)
    return "N/A" if number is None else f"${number:,.2f}"


def pct(value: Any) -> str:
    number = safe_float(value, None)
    return "N/A" if number is None else f"{number * 100:.1f}%"


def short_id(value: Any) -> str:
    text = str(value or "")
    return text if len(text) <= 12 else f"{text[:8]}..."


def status_of(row: dict[str, Any]) -> str:
    return str(row.get("status") or "unknown").strip().lower()


def value_from(data: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return [row for row in data[key] if isinstance(row, dict)]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


class ArenaClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"x-api-key": api_key}
        self.timeout = timeout

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RuntimeError(f"GET {path} failed: {exc}") from exc
        if not response.ok:
            body = response.text[:500].strip()
            raise RuntimeError(f"GET {path} returned HTTP {response.status_code}: {body}")
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"GET {path} returned non-JSON data") from exc


def read_json(path: Path, warnings: list[str]) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        warnings.append(f"Skipped malformed local file {path.name}: {exc}")
        return None


def text_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "display_name", "country_name", "code", "short_code"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def fixture_id_of(node: dict[str, Any]) -> str | None:
    for key in ("fixture_id", "fixture_code", "id"):
        value = node.get(key)
        if value is not None and str(value).isdigit() and len(str(value)) >= 6:
            return str(value)
    nested = node.get("fixture")
    return fixture_id_of(nested) if isinstance(nested, dict) else None


def fixture_info_from_node(node: dict[str, Any]) -> dict[str, str] | None:
    fixture_id = fixture_id_of(node)
    if not fixture_id:
        return None
    match = text_value(node.get("match")) or text_value(node.get("sportmonks_match_name"))
    fixture = node.get("fixture")
    if not match and isinstance(fixture, str):
        match = fixture.strip()
    home = text_value(node.get("home_team")) or text_value(node.get("home_country"))
    away = text_value(node.get("away_team")) or text_value(node.get("away_country"))
    home_code = text_value(node.get("home_short_code")) or text_value(node.get("home_code"))
    away_code = text_value(node.get("away_short_code")) or text_value(node.get("away_code"))
    if isinstance(node.get("home_team"), dict):
        home_code = home_code or text_value(node["home_team"].get("code"))
    if isinstance(node.get("away_team"), dict):
        away_code = away_code or text_value(node["away_team"].get("code"))
    if not match and home and away:
        match = f"{home} vs {away}"
    if not any((match, home, away, home_code, away_code)):
        return None
    return {
        "fixture_id": fixture_id,
        "match": match or fixture_id,
        "home_code": home_code or "",
        "away_code": away_code or "",
        "home_name": home or "",
        "away_name": away or "",
    }


def walk_fixture_nodes(value: Any):
    if isinstance(value, dict):
        if "fixture_id" in value or "fixture_code" in value:
            yield value
        for child in value.values():
            yield from walk_fixture_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_fixture_nodes(child)


class FixtureResolver:
    PRIORITY_FILES = (
        "mazo_strategy_analysis.json",
        "dashboard_status_latest.json",
        "qualification_ladder_candidates.json",
        "post_qualification_report.json",
    )

    def __init__(self, output_dir: Path, warnings: list[str]) -> None:
        self.output_dir = output_dir
        self.warnings = warnings
        self.cache: dict[str, dict[str, str]] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.output_dir.exists():
            return
        paths: list[Path] = []
        for name in self.PRIORITY_FILES:
            path = self.output_dir / name
            if path.exists():
                paths.append(path)
        paths.extend(sorted(self.output_dir.glob("prematch_*.json")))
        seen = {path.resolve() for path in paths}
        paths.extend(path for path in sorted(self.output_dir.glob("*.json")) if path.resolve() not in seen)
        for path in paths:
            data = read_json(path, self.warnings)
            if data is None:
                continue
            for node in walk_fixture_nodes(data):
                if not NAME_KEYS.intersection(node):
                    continue
                info = fixture_info_from_node(node)
                if info:
                    current = self.cache.get(info["fixture_id"])
                    if current is None or current["match"] == info["fixture_id"]:
                        self.cache[info["fixture_id"]] = info

    def resolve(self, fixture_id: Any) -> dict[str, str]:
        key = str(fixture_id or "")
        self._load()
        return self.cache.get(
            key,
            {"fixture_id": key, "match": key, "home_code": "", "away_code": "", "home_name": "", "away_name": ""},
        )


def settlement_payload(client: ArenaClient, fixture_id: str, warnings: list[str]) -> dict[str, Any]:
    try:
        data = client.get(f"{API_PREFIX}/data/polymarket/markets/{fixture_id}/settlement")
        return data if isinstance(data, dict) else {}
    except RuntimeError as exc:
        warnings.append(str(exc))
        return {}


def infer_settled_result(
    order: dict[str, Any],
    positions_by_key: dict[tuple[str, str], dict[str, Any]],
    settlement: dict[str, Any],
) -> tuple[str, float | None, str]:
    fixture_id = str(order.get("fixture_id") or "")
    team_code = str(order.get("team_code") or "")
    settled_outcome = settlement.get("settled_outcome") or settlement.get("winning_outcome") or settlement.get("winner")
    if isinstance(settled_outcome, dict):
        settled_outcome = settled_outcome.get("team_code") or settled_outcome.get("outcome") or settled_outcome.get("code")
    if settled_outcome:
        won = str(settled_outcome).lower() == team_code.lower()
        return ("WIN" if won else "LOSS", 1.0 if won else 0.0, "settlement endpoint")
    position = positions_by_key.get((fixture_id, team_code.lower()))
    if position:
        mark = safe_float(position.get("mark_price"), None)
        value = safe_float(position.get("value_usdc"), 0.0) or 0.0
        if mark is not None and mark >= 0.995:
            return "WIN", 1.0, "settled exposure mark"
        if mark is not None and mark <= 0.005 and value <= 0.005:
            return "LOSS", 0.0, "settled exposure mark"
    return "UNKNOWN", None, "insufficient settlement evidence"


def quantity_for(order: dict[str, Any]) -> float | None:
    quantity = safe_float(order.get("quantity"), None)
    if quantity is not None:
        return quantity
    stake = safe_float(order.get("usd_size_filled"), None)
    price = safe_float(order.get("open_avg_fill_price"), None)
    if stake is None or price is None or price <= 0:
        return None
    return stake / price


def age_seconds(order: dict[str, Any], now: datetime) -> float | None:
    raw = order.get("updated_at") or order.get("created_at") or order.get("submitted_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (now - parsed.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return None


def build_report(client: ArenaClient, initial_bankroll: float | None) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    resolver = FixtureResolver(OUTPUT, warnings)

    def fetch(path: str, fallback: Any) -> Any:
        try:
            return client.get(path)
        except RuntimeError as exc:
            errors.append(str(exc))
            return fallback

    agent_data = fetch(f"{API_PREFIX}/arena/agents/me", {})
    exposure_data = fetch(f"{API_PREFIX}/arena/exposure", {"positions": []})
    orders_data = fetch(f"{API_PREFIX}/arena/orders", {"orders": []})
    wallet = agent_data.get("wallet") if isinstance(agent_data, dict) else {}
    wallet = wallet if isinstance(wallet, dict) else {}
    positions = value_from(exposure_data, "positions")
    orders = value_from(orders_data, "orders")
    open_positions = [row for row in positions if (safe_float(row.get("value_usdc"), 0.0) or 0.0) > 0]
    positions_by_key = {
        (str(row.get("fixture_id") or ""), str(row.get("team_code") or "").lower()): row for row in positions
    }

    settlement_cache: dict[str, dict[str, Any]] = {}
    settled_orders: list[dict[str, Any]] = []
    rejected_orders: list[dict[str, Any]] = []
    filled_orders: list[dict[str, Any]] = []
    other_orders: list[dict[str, Any]] = []
    realized_known = 0.0
    realized_unknown_count = 0

    for original in orders:
        row = dict(original)
        fixture_id = str(row.get("fixture_id") or "")
        row["fixture_id"] = fixture_id
        row["match"] = resolver.resolve(fixture_id)["match"]
        status = status_of(row)
        if status in SETTLED_STATUSES:
            if fixture_id not in settlement_cache:
                settlement_cache[fixture_id] = settlement_payload(client, fixture_id, warnings)
            result, payout_mark, evidence = infer_settled_result(row, positions_by_key, settlement_cache[fixture_id])
            stake = safe_float(row.get("usd_size_filled"), 0.0) or 0.0
            quantity = quantity_for(row)
            estimated_payout = quantity if result == "WIN" and quantity is not None else 0.0 if result == "LOSS" else None
            realized = estimated_payout - stake if estimated_payout is not None else None
            row.update(
                {
                    "inferred_result": result,
                    "result_evidence": evidence,
                    "settled_outcome": settlement_cache[fixture_id].get("settled_outcome"),
                    "quantity_inferred": quantity,
                    "estimated_payout_usdc": estimated_payout,
                    "realized_pnl_usdc": realized,
                    "result_mark": payout_mark,
                }
            )
            if realized is None:
                realized_unknown_count += 1
            else:
                realized_known += realized
            settled_orders.append(row)
        elif status in REJECTED_STATUSES:
            rejected_orders.append(row)
        elif status in FILLED_STATUSES:
            filled_orders.append(row)
        else:
            other_orders.append(row)

    enriched_positions: list[dict[str, Any]] = []
    for original in open_positions:
        row = dict(original)
        fixture_id = str(row.get("fixture_id") or "")
        mark = safe_float(row.get("mark_price"), None)
        avg_cost = safe_float(row.get("avg_cost_usdc"), None)
        quantity = safe_float(row.get("quantity"), None)
        cost_basis = avg_cost * quantity if avg_cost is not None and quantity is not None else None
        if mark is not None and mark >= 0.95:
            label = "LIKELY WINNING"
        elif mark is not None and mark <= 0.05:
            label = "LIKELY LOSING"
        else:
            label = "LIVE / OPEN"
        row.update(
            {
                "fixture_id": fixture_id,
                "match": resolver.resolve(fixture_id)["match"],
                "cost_basis_usdc": cost_basis,
                "status_label": label,
            }
        )
        enriched_positions.append(row)
    enriched_positions.sort(
        key=lambda row: (
            -(safe_float(row.get("value_usdc"), 0.0) or 0.0),
            safe_float(row.get("unrealized_pnl_usdc"), 0.0) or 0.0,
        )
    )

    available = safe_float(wallet.get("available_balance_usdc"), 0.0) or 0.0
    locked = safe_float(wallet.get("locked_balance_usdc"), 0.0) or 0.0
    live_value = sum(safe_float(row.get("value_usdc"), 0.0) or 0.0 for row in enriched_positions)
    unrealized = sum(safe_float(row.get("unrealized_pnl_usdc"), 0.0) or 0.0 for row in enriched_positions)
    estimated_value = available + locked + live_value

    filled_positive = [row for row in orders if status_of(row) in FILLED_STATUSES | SETTLED_STATUSES and (safe_float(row.get("usd_size_filled"), 0.0) or 0.0) > 0]
    duplicate_counts: dict[tuple[str, str], int] = {}
    fixture_teams: dict[str, set[str]] = {}
    for row in filled_positive:
        fixture_id = str(row.get("fixture_id") or "")
        team = str(row.get("team_code") or "")
        duplicate_counts[(fixture_id, team)] = duplicate_counts.get((fixture_id, team), 0) + 1
        fixture_teams.setdefault(fixture_id, set()).add(team)
        stake = safe_float(row.get("usd_size_filled"), 0.0) or 0.0
        if stake > 5:
            warnings.append(f"Large historical position: {team} fixture {fixture_id}, {money(stake)}")
        wallet_reference = available + locked
        if wallet_reference > 0 and stake > wallet_reference * 0.25:
            warnings.append(f"Order {short_id(row.get('order_id'))} stake {money(stake)} exceeds 25% of current wallet cash")
    for (fixture_id, team), count in duplicate_counts.items():
        if count > 1:
            warnings.append(f"Duplicate filled orders: fixture {fixture_id} has {count} {team} orders")
    for fixture_id, teams in fixture_teams.items():
        if len(teams) > 1:
            warnings.append(f"Multiple filled outcomes on fixture {fixture_id}: {', '.join(sorted(teams))}")
    if realized_unknown_count:
        warnings.append(f"{realized_unknown_count} settled orders have unknown win/loss because no reliable final outcome was found")
    exposure_keys = {(str(row.get("fixture_id") or ""), str(row.get("team_code") or "").lower()) for row in positions}
    for row in filled_orders:
        key = (str(row.get("fixture_id") or ""), str(row.get("team_code") or "").lower())
        if key not in exposure_keys:
            warnings.append(f"Completed order {short_id(row.get('order_id'))} is absent from exposure and not marked settled")
    if locked > 0:
        warnings.append(f"Locked balance is nonzero: {money(locked)}")
    now = datetime.now(timezone.utc)
    for row in other_orders:
        age = age_seconds(row, now)
        if age is not None and age > 3600:
            warnings.append(f"Old pending order {short_id(row.get('order_id'))}: status {status_of(row)}, age {age / 3600:.1f}h")

    summary: dict[str, Any] = {
        "open_positions": len(enriched_positions),
        "completed_open_orders": len(filled_orders),
        "settled_orders": len(settled_orders),
        "rejected_orders": len(rejected_orders),
        "other_orders": len(other_orders),
        "live_exposure_value_usdc": round(live_value, 6),
        "unrealized_pnl_usdc": round(unrealized, 6),
        "realized_pnl_known_usdc": round(realized_known, 6),
        "realized_pnl_unknown_count": realized_unknown_count,
        "estimated_account_value_usdc": round(estimated_value, 6),
        "initial_bankroll_usdc": initial_bankroll,
        "estimated_total_pnl_usdc": round(estimated_value - initial_bankroll, 6) if initial_bankroll is not None else None,
        "estimated_roi": ((estimated_value - initial_bankroll) / initial_bankroll) if initial_bankroll else None,
    }
    return {
        "generated_at": now.isoformat(),
        "read_only": True,
        "wallet": {
            "agent_name": agent_data.get("display_name") or agent_data.get("name") if isinstance(agent_data, dict) else None,
            "agent_id": agent_data.get("agent_id") if isinstance(agent_data, dict) else None,
            "wallet_address": wallet.get("address") or wallet.get("wallet_address"),
            "available_balance_usdc": available,
            "locked_balance_usdc": locked,
            "live_exposure_value_usdc": live_value,
            "estimated_account_value_usdc": estimated_value,
        },
        "open_positions": enriched_positions,
        "filled_open_orders": filled_orders,
        "settled_orders": settled_orders,
        "rejected_orders": rejected_orders,
        "other_orders": other_orders,
        "summary": summary,
        "warnings": list(dict.fromkeys(warnings)),
        "errors": errors,
    }


def filter_report(report: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    filtered = dict(report)
    fixture = str(args.fixture) if args.fixture else None
    list_keys = ("open_positions", "filled_open_orders", "settled_orders", "rejected_orders", "other_orders")
    for key in list_keys:
        rows = report.get(key, [])
        if fixture:
            rows = [row for row in rows if str(row.get("fixture_id")) == fixture]
        filtered[key] = rows
    if args.open_only:
        filtered["settled_orders"] = []
        filtered["rejected_orders"] = []
        filtered["other_orders"] = []
    elif args.settled_only:
        filtered["open_positions"] = []
        filtered["filled_open_orders"] = []
        filtered["rejected_orders"] = []
        filtered["other_orders"] = []
    return filtered


def table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "(none)"
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], min(len(cell), 34))
    def line(row: list[str]) -> str:
        values = [(cell[:31] + "...") if len(cell) > 34 else cell for cell in row]
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(values)).rstrip()
    return "\n".join([line(headers), line(["-" * width for width in widths]), *(line(row) for row in rows)])


def render_text(report: dict[str, Any], show_rejected: bool, compact: bool = False) -> str:
    wallet = report["wallet"]
    summary = report["summary"]
    if compact:
        return (
            f"Cash {money(wallet.get('available_balance_usdc'))} | Open {money(summary.get('live_exposure_value_usdc'))} | "
            f"Total {money(summary.get('estimated_account_value_usdc'))} | Open Bets {summary.get('open_positions', 0)} | "
            f"Settled {summary.get('settled_orders', 0)} | Rejected {summary.get('rejected_orders', 0)}"
        )
    lines = [
        "POLYSTAIR ARENA ACCOUNT REPORT",
        f"Generated: {report['generated_at']} UTC" if not report["generated_at"].endswith("+00:00") else f"Generated: {report['generated_at']}",
        "Read-only: YES (GET requests only)",
        "",
        "WALLET",
        f"Agent: {wallet.get('agent_name') or 'N/A'}",
        f"Agent ID: {wallet.get('agent_id') or 'N/A'}",
        f"Wallet: {wallet.get('wallet_address') or 'N/A'}",
        f"Available: {money(wallet.get('available_balance_usdc'))}",
        f"Locked: {money(wallet.get('locked_balance_usdc'))}",
        f"Live Exposure Value: {money(wallet.get('live_exposure_value_usdc'))}",
        f"Estimated Account Value: {money(wallet.get('estimated_account_value_usdc'))}",
        "",
        "OPEN POSITIONS",
        table(
            ["fixture", "match", "pick", "avg", "mark", "cost", "value", "PnL", "status"],
            [
                [
                    str(row.get("fixture_id") or ""), str(row.get("match") or ""), str(row.get("team_code") or ""),
                    f"{safe_float(row.get('avg_cost_usdc'), 0.0):.3f}", f"{safe_float(row.get('mark_price'), 0.0):.3f}",
                    money(row.get("cost_basis_usdc")), money(row.get("value_usdc")), money(row.get("unrealized_pnl_usdc")),
                    str(row.get("status_label") or ""),
                ] for row in report["open_positions"]
            ],
        ),
        "",
        "ORDERS",
        f"Completed/Open: {summary.get('completed_open_orders', 0)}",
        f"Settled: {summary.get('settled_orders', 0)}",
        f"Rejected: {summary.get('rejected_orders', 0)}",
        f"Other/Pending: {summary.get('other_orders', 0)}",
        "",
        "SETTLED ORDERS",
        table(
            ["fixture", "match", "pick", "stake", "price", "payout", "status", "result"],
            [
                [
                    str(row.get("fixture_id") or ""), str(row.get("match") or ""), str(row.get("team_code") or ""),
                    money(row.get("usd_size_filled")), f"{safe_float(row.get('open_avg_fill_price'), 0.0):.3f}",
                    money(row.get("estimated_payout_usdc")), str(row.get("status") or ""), str(row.get("inferred_result") or "UNKNOWN"),
                ] for row in report["settled_orders"]
            ],
        ),
    ]
    if show_rejected:
        lines.extend(
            [
                "", "REJECTED / UNFILLED ORDERS",
                table(
                    ["fixture", "pick", "order", "reason", "fill", "filled USD"],
                    [
                        [
                            str(row.get("fixture_id") or ""), str(row.get("team_code") or ""), short_id(row.get("order_id")),
                            str(row.get("rejection_reason") or row.get("reason") or "N/A"),
                            str(row.get("open_avg_fill_price") or "null"), money(row.get("usd_size_filled")),
                        ] for row in report["rejected_orders"]
                    ],
                ),
            ]
        )
    elif summary.get("rejected_orders"):
        lines.extend(["", f"Rejected orders hidden: {summary['rejected_orders']}", "Use --show-rejected to display them."])
    lines.extend(
        [
            "", "SUMMARY",
            f"Open positions: {summary.get('open_positions', 0)}",
            f"Settled orders: {summary.get('settled_orders', 0)}",
            f"Known realized PnL: {money(summary.get('realized_pnl_known_usdc'))}",
            f"Unknown settled outcomes: {summary.get('realized_pnl_unknown_count', 0)}",
            f"Unrealized PnL: {money(summary.get('unrealized_pnl_usdc'))}",
        ]
    )
    if summary.get("initial_bankroll_usdc") is not None:
        lines.extend(
            [
                f"Initial bankroll: {money(summary['initial_bankroll_usdc'])}",
                f"Estimated total PnL: {money(summary.get('estimated_total_pnl_usdc'))}",
                f"Estimated ROI: {pct(summary.get('estimated_roi'))}",
            ]
        )
    if report["warnings"]:
        lines.extend(["", "WARNINGS", *(f"- {warning}" for warning in report["warnings"])])
    if report["errors"]:
        lines.extend(["", "API ERRORS", *(f"- {error}" for error in report["errors"])])
    return "\n".join(lines)


def markdown_report(report: dict[str, Any], show_rejected: bool) -> str:
    return "```text\n" + render_text(report, show_rejected) + "\n```\n"


def save_reports(report: dict[str, Any], text_report: str) -> list[Path]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    paths = [
        OUTPUT / "arena_account_report_latest.json",
        OUTPUT / "arena_account_report_latest.md",
        OUTPUT / f"arena_account_report_{stamp}.json",
        OUTPUT / f"arena_account_report_{stamp}.md",
    ]
    payload = json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    for path in paths:
        path.write_text(markdown_report(report, True) if path.suffix == ".md" else payload, encoding="utf-8")
    return paths


def save_csv(report: dict[str, Any]) -> list[Path]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    orders_path = OUTPUT / "arena_orders_latest.csv"
    positions_path = OUTPUT / "arena_positions_latest.csv"
    order_rows = report["filled_open_orders"] + report["settled_orders"] + report["rejected_orders"] + report["other_orders"]
    write_csv(orders_path, order_rows)
    write_csv(positions_path, report["open_positions"])
    return [orders_path, positions_path]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        if not fields:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value for key, value in row.items()})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only PolyStair Arena account report")
    parser.add_argument("--json", action="store_true", help="Print structured JSON")
    parser.add_argument("--save", action="store_true", help="Save latest and timestamped JSON/Markdown reports")
    parser.add_argument("--show-rejected", action="store_true", help="Display rejected and unfilled order details")
    parser.add_argument("--fixture", help="Filter displayed rows to one fixture ID")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--open-only", action="store_true", help="Display only open positions/orders")
    group.add_argument("--settled-only", action="store_true", help="Display only settled orders")
    parser.add_argument("--refresh", action="store_true", help="Explicitly fetch fresh live data (live fetch is always used)")
    parser.add_argument("--initial-bankroll", type=float, help="Initial bankroll for account-level PnL/ROI")
    parser.add_argument("--watch", type=float, metavar="SECONDS", help="Refresh continuously at this interval")
    parser.add_argument("--csv", action="store_true", help="Save latest orders and positions CSV files")
    parser.add_argument("--compact", action="store_true", help="Print one-line summary")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.watch is not None and args.watch <= 0:
        print("ERROR: --watch must be greater than zero", file=sys.stderr)
        return 2
    if not ENV_PATH.exists():
        print(f"ERROR: missing .env at {ENV_PATH}", file=sys.stderr)
        return 2
    load_dotenv(ENV_PATH, override=False)
    api_key = os.getenv("ARENA_KEY", "").strip()
    base_url = os.getenv("ARENA_BASE_URL", "").strip()
    if not api_key:
        print("ERROR: ARENA_KEY is missing", file=sys.stderr)
        return 2
    if not base_url:
        print("ERROR: ARENA_BASE_URL is missing", file=sys.stderr)
        return 2
    initial_bankroll = args.initial_bankroll
    if initial_bankroll is None and os.getenv("ARENA_INITIAL_BANKROLL"):
        initial_bankroll = safe_float(os.getenv("ARENA_INITIAL_BANKROLL"), None)
    client = ArenaClient(base_url, api_key)

    try:
        while True:
            report = filter_report(build_report(client, initial_bankroll), args)
            rendered = render_text(report, args.show_rejected, args.compact)
            print(json.dumps(report, indent=2, sort_keys=True, default=str) if args.json else rendered)
            saved: list[Path] = []
            if args.save:
                saved.extend(save_reports(report, rendered))
            if args.csv:
                saved.extend(save_csv(report))
            if saved and not args.json:
                print("\nSaved:")
                for path in saved:
                    print(f"- {path.relative_to(ROOT)}")
            if args.watch is None:
                return 1 if report["errors"] else 0
            time.sleep(args.watch)
            if not args.json:
                print("\n" + "=" * 90 + "\n")
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
