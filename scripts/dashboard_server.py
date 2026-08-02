import json
import os
import subprocess
import sys
import requests
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import dotenv_values
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
OUTPUT = ROOT / "output"
ENV_PATH = ROOT / ".env"
load_dotenv(ENV_PATH, override=True)
ARENA = os.getenv("ARENA_BASE_URL", "https://stair-ai.com").rstrip("/")
ARENA_V1 = os.getenv("ARENA_API_PREFIX", "/api/v1").rstrip("/")


def read_json(path):
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def mask(value):
    if not value:
        return None
    if len(value) <= 12:
        return "***"
    return f"{value[:6]}...{value[-4:]}"


def latest_prematch():
    files = sorted(OUTPUT.glob("prematch_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    data = read_json(files[0])
    if data:
        data["_file"] = files[0].name
        data["_updated_at"] = datetime.fromtimestamp(files[0].stat().st_mtime, timezone.utc).isoformat()
    return data


def scan_report():
    data = read_json(OUTPUT / "scan_report.json")
    return data or {"ranked": [], "errors": [], "incomplete_market": [], "fixtures_scanned": 0}


def qualification_ladder_report():
    return read_json(OUTPUT / "qualification_ladder_candidates.json") or {
        "selected_batch": [],
        "candidate_count": 0,
        "winner_ladder_count": 0,
        "value_slot_count": 0,
        "total_planned_spend": 0,
    }


def token_usage(report, prematch):
    usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}

    def add_meta(meta):
        if not isinstance(meta, dict):
            return
        if "tokens_in" in meta or "tokens_out" in meta:
            usage["input_tokens"] += meta.get("tokens_in") or 0
            usage["output_tokens"] += meta.get("tokens_out") or 0
            usage["requests"] += 1
        for value in meta.values():
            if isinstance(value, dict):
                add_meta(value)

    for result in report.get("ranked", []):
        for record in result.get("records", []):
            add_meta(record.get("model_invocation"))
    if prematch:
        for record in prematch.get("records", []):
            add_meta(record.get("model_invocation"))

    # Rough display estimate. Real billing comes from OpenAI/Stair dashboards.
    est_usd = (usage["input_tokens"] * 0.00000015) + (usage["output_tokens"] * 0.0000006)
    usage["estimated_openai_usd"] = round(est_usd, 6)
    return usage


def ledger_file_stats():
    files = list(OUTPUT.glob("ledger_trace_*.json")) + list(OUTPUT.glob("ledger_failed_*.json"))
    records = 0
    submitted = 0
    for path in files:
        data = read_json(path) or {}
        batch_records = data.get("records") or []
        records += len(batch_records)
        if data.get("submitted") or data.get("response", {}).get("errors") == []:
            submitted += 1
    return {"files": len(files), "records": records, "submissions": submitted}


def arena_headers(env):
    return {"x-api-key": env.get("ARENA_KEY") or ""}


def safe_get_arena(env, path, params=None, require_key=True):
    if require_key and not env.get("ARENA_KEY"):
        return {"ok": False, "error": "ARENA_KEY missing"}
    started = datetime.now(timezone.utc)
    try:
        headers = arena_headers(env) if require_key else {}
        response = requests.get(f"{ARENA}{path}", headers=headers, params=params, timeout=15)
        latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        response.raise_for_status()
        return {"ok": True, "data": response.json(), "status_code": response.status_code, "latency_ms": latency_ms}
    except Exception as exc:
        response = getattr(exc, "response", None)
        latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        return {
            "ok": False,
            "error": str(exc),
            "status_code": getattr(response, "status_code", None),
            "latency_ms": latency_ms,
        }


def list_from_response(response_data, key):
    if isinstance(response_data, dict):
        value = response_data.get(key)
        if isinstance(value, list):
            return value
    if isinstance(response_data, list):
        return response_data
    return []


def flatten_bracket_fixtures(data):
    fixtures = []
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return fixtures
    for key in ("fixtures", "matches"):
        value = data.get(key)
        if isinstance(value, list):
            fixtures.extend(value)
    for round_item in data.get("rounds") or []:
        if not isinstance(round_item, dict):
            continue
        round_label = round_item.get("label") or round_item.get("name") or round_item.get("round")
        groups = round_item.get("groups") or []
        for match in round_item.get("matches") or round_item.get("fixtures") or []:
            if isinstance(match, dict):
                fixtures.append({**match, "_round_label": round_label})
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_label = group.get("label") or group.get("name")
            for match in group.get("matches") or group.get("fixtures") or []:
                if isinstance(match, dict):
                    fixtures.append({**match, "_round_label": round_label, "_group_label": group_label})
    return fixtures


def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def is_filled_order(order):
    return str(order.get("status", "")).lower() in {"filled", "settled", "closed"} and as_float(order.get("usd_size_filled")) > 0


def is_rejected_order(order):
    return str(order.get("status", "")).lower() == "rejected"


def money_string(value):
    if value is None:
        return None
    number = as_float(value, None)
    if number is None:
        return value
    return f"{number:g}"


def order_id_for(row):
    return row.get("order_id") or row.get("id")


def settlement_stage(order, settlement):
    status = str(order.get("status") or "").lower()
    settlement_status = str((settlement or {}).get("status") or "").lower()
    if status in {"settled", "refunded"} or settlement_status == "settled":
        return "settled"
    if status in {"filled", "closed", "closing"} or settlement_status in {"pending", "open"}:
        return "pending settlement"
    return "open"


def settlement_winner(order, settlement):
    settled_outcome = (settlement or {}).get("settled_outcome")
    if not settled_outcome:
        return None
    return str(settled_outcome) == str(order.get("team_code"))


def fetch_order_details(env, order_rows):
    details = {}
    for row in order_rows:
        order_id = order_id_for(row)
        if order_id:
            details[order_id] = safe_get_arena(env, f"{ARENA_V1}/arena/orders/{order_id}")
    return details


def fetch_settlements(env, order_rows):
    settlements = {}
    fixture_ids = sorted({str(row.get("fixture_id")) for row in order_rows if row.get("fixture_id")})
    for fixture_id in fixture_ids:
        settlements[fixture_id] = safe_get_arena(env, f"{ARENA_V1}/data/polymarket/markets/{fixture_id}/settlement")
    return settlements


def enrich_orders(order_rows, order_detail_responses, settlement_responses):
    enriched = []
    for row in order_rows:
        order_id = order_id_for(row)
        fixture_id = str(row.get("fixture_id") or "")
        detail_response = order_detail_responses.get(order_id) if order_id else None
        settlement_response = settlement_responses.get(fixture_id) if fixture_id else None
        detail = detail_response.get("data") if detail_response and detail_response.get("ok") else {}
        settlement = settlement_response.get("data") if settlement_response and settlement_response.get("ok") else {}
        merged = {**row, **(detail or {})}
        merged["settlement_status"] = (settlement or {}).get("status")
        merged["settled_outcome"] = (settlement or {}).get("settled_outcome")
        merged["settled_at"] = (settlement or {}).get("settled_at")
        merged["settlement_stage"] = settlement_stage(merged, settlement)
        merged["is_win"] = settlement_winner(merged, settlement)
        enriched.append(merged)
    return enriched


def build_status():
    env = dotenv_values(ENV_PATH)
    report = scan_report()
    ladder_report = qualification_ladder_report()
    prematch = latest_prematch()
    agent_me = safe_get_arena(env, f"{ARENA_V1}/arena/agents/me")
    exposure = safe_get_arena(env, f"{ARENA_V1}/arena/exposure", {"include_closing": "true"})
    orders = safe_get_arena(env, f"{ARENA_V1}/arena/orders")
    public_agent = safe_get_arena(env, f"{ARENA_V1}/web/agents/polystair", require_key=False)
    tournament = safe_get_arena(env, f"{ARENA_V1}/web/tournament", require_key=False)
    bracket = safe_get_arena(env, f"{ARENA_V1}/web/bracket", require_key=False)
    leaderboard_stats = safe_get_arena(env, f"{ARENA_V1}/web/leaderboard/stats", require_key=False)
    leaderboard = safe_get_arena(env, f"{ARENA_V1}/web/leaderboard", require_key=False)
    recent_predictions = safe_get_arena(env, f"{ARENA_V1}/web/agents/polystair/recent-predictions", require_key=False)
    confidence_series = safe_get_arena(env, f"{ARENA_V1}/web/agents/polystair/confidence-series", require_key=False)
    wallet = ((agent_me.get("data") or {}).get("wallet") or {}) if agent_me.get("ok") else {}
    positions = list_from_response(exposure.get("data"), "positions") if exposure.get("ok") else []
    order_rows = list_from_response(orders.get("data"), "orders") if orders.get("ok") else []
    order_detail_responses = fetch_order_details(env, order_rows) if orders.get("ok") else {}
    settlement_responses = fetch_settlements(env, order_rows) if orders.get("ok") else {}
    order_rows = enrich_orders(order_rows, order_detail_responses, settlement_responses)
    order_rows = sorted(order_rows, key=lambda row: row.get("created_at") or row.get("updated_at") or "", reverse=True)
    bracket_rows = flatten_bracket_fixtures(bracket.get("data"))
    ranked = report.get("ranked", [])
    trade_candidates = [r for r in ranked if r.get("strategy", {}).get("should_trade")]
    positive_edges = [r for r in ranked if float(r.get("strategy", {}).get("edge_pp") or 0) > 0]
    best = ranked[0] if ranked else None
    live_arena_ok = bool(agent_me.get("ok") and exposure.get("ok") and orders.get("ok"))
    raw_available = wallet.get("available_balance_usdc")
    position_value_usdc = sum(as_float(position.get("value_usdc")) for position in positions)
    net_available = max(0.0, as_float(raw_available) - position_value_usdc) if agent_me.get("ok") and exposure.get("ok") and raw_available is not None else raw_available
    locked_usdc = as_float(wallet.get("locked_balance_usdc"))
    unrealized_pnl = sum(as_float(p.get("unrealized_pnl_usdc")) for p in positions)
    filled_orders = [order for order in order_rows if is_filled_order(order)]
    rejected_orders = [order for order in order_rows if is_rejected_order(order)]
    settled_orders = [order for order in order_rows if order.get("settlement_stage") == "settled"]
    wins = [order for order in settled_orders if order.get("is_win") is True]
    losses = [order for order in settled_orders if order.get("is_win") is False]
    settled_trade_count = len(settled_orders)
    win_rate = (len(wins) / settled_trade_count) if settled_trade_count else None
    realized_pnl = sum(as_float(order.get("realized_pnl_usdc")) for order in order_rows if order.get("realized_pnl_usdc") is not None)
    total_filled_usd = sum(as_float(order.get("usd_size_filled")) for order in filled_orders)
    filled_trade_count = len(filled_orders)
    latest_order = order_rows[0] if order_rows else None
    latest_trade = filled_orders[0] if filled_orders else None
    rejected_reconciliation = bool(order_rows and len(rejected_orders) == len(order_rows) and not positions)
    starting_bankroll = as_float(env.get("STARTING_BANKROLL_USDC"), 100.0)
    current_equity = as_float(net_available) + position_value_usdc
    roi = ((current_equity - starting_bankroll) / starting_bankroll) if starting_bankroll else None
    ledger_stats = ledger_file_stats()
    latest_scan = report.get("ranked", [None])[0] if report.get("ranked") else None
    edges = [as_float(r.get("strategy", {}).get("edge_pp"), None) for r in ranked]
    edges = [edge for edge in edges if edge is not None]
    confidences = [as_float(r.get("prediction", {}).get("confidence_score"), None) for r in ranked]
    confidences = [score for score in confidences if score is not None]
    qualification_target = 10
    qualification_progress = filled_trade_count

    checks = {
        "arena_key": bool(env.get("ARENA_KEY")),
        "model_available": bool(env.get("OPENAI_API_KEY")) or env.get("USE_OPENAI") == "0",
        "scan_report": bool(ranked),
        "dry_run": env.get("DRY_RUN", "1") == "1",
        "ledger_live": bool(prematch and prematch.get("ledger", {}).get("submitted")),
        "order_live": bool(order_rows),
        "live_arena_data": live_arena_ok,
    }
    readiness = 0
    readiness += 20 if checks["arena_key"] else 0
    readiness += 20 if checks["model_available"] else 0
    readiness += 25 if checks["scan_report"] else 0
    readiness += 15 if trade_candidates else 5 if positive_edges else 0
    readiness += 10 if checks["dry_run"] else 5
    readiness += 10 if checks["ledger_live"] or prematch else 0

    return {
        "ok": live_arena_ok,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "env": {
            "Arena Auth": "CONNECTED" if env.get("ARENA_KEY") else "MISSING",
            "ARENA_BASE_URL": env.get("ARENA_BASE_URL", ARENA),
            "OpenAI Key": "PRESENT" if env.get("OPENAI_API_KEY") else "MISSING",
            "Supabase": "CONNECTED",
            "USE_OPENAI": env.get("USE_OPENAI", "1"),
            "OPENAI_MODEL": env.get("OPENAI_MODEL"),
            "DRY_RUN": env.get("DRY_RUN", "1"),
            "ORDER_EXECUTION_MODE": env.get("ORDER_EXECUTION_MODE", "limit"),
            "FOK_PRICE_BUFFER_PP": env.get("FOK_PRICE_BUFFER_PP", "1.0"),
            "ORDER_POLL_SECONDS": env.get("ORDER_POLL_SECONDS", "30"),
            "ORDER_POLL_INTERVAL_SECONDS": env.get("ORDER_POLL_INTERVAL_SECONDS", "2"),
            "MODEL_VARIANT": env.get("MODEL_VARIANT", "baseline"),
            "STRATEGY_MODE": env.get("STRATEGY_MODE", "winner_only"),
            "TRADE_EDGE_THRESHOLD_PP": env.get("TRADE_EDGE_THRESHOLD_PP", "6"),
            "MAX_TRADE_USDC": env.get("MAX_TRADE_USDC", "2"),
            "SUBMIT_LEDGER": env.get("SUBMIT_LEDGER", "0"),
            "LEDGER_BATCH_ENDPOINT": env.get("LEDGER_BATCH_ENDPOINT", f"{ARENA_V1}/arena/ledger/records/batch"),
            "LEDGER_VALIDATE_ENDPOINT": env.get("LEDGER_VALIDATE_ENDPOINT", f"{ARENA_V1}/arena/ledger/records/validate"),
            "REQUIRE_LIVE_CONFIRM": env.get("REQUIRE_LIVE_CONFIRM", "1"),
        },
        "checks": checks,
        "readiness_score": min(100, readiness),
        "pnl": {
            "realized_usd": round(realized_pnl, 6),
            "unrealized_usd": sum(float(p.get("unrealized_pnl_usdc") or 0) for p in positions),
            "available_usd": money_string(net_available),
            "locked_usd": wallet.get("locked_balance_usdc"),
            "note": exposure.get("error") if not exposure.get("ok") else "Exposure loaded from /api/v1/arena/exposure.",
        },
        "wallet": {
            "ok": agent_me.get("ok"),
            "error": agent_me.get("error"),
            "agent_id": (agent_me.get("data") or {}).get("agent_id") if agent_me.get("ok") else None,
            "display_name": (agent_me.get("data") or {}).get("display_name") if agent_me.get("ok") else None,
            "lifecycle_phase": (agent_me.get("data") or {}).get("lifecycle_phase") if agent_me.get("ok") else None,
            "address": wallet.get("address"),
            "available_balance_usdc": money_string(net_available),
            "reported_available_balance_usdc": raw_available,
            "open_position_value_usdc": money_string(position_value_usdc),
            "locked_balance_usdc": wallet.get("locked_balance_usdc"),
            "polymarket_profile_url": wallet.get("polymarket_profile_url"),
            "polyscan_url": wallet.get("polyscan_url"),
        },
        "live_arena": {
            "ok": live_arena_ok,
            "badge": "Rejected / Endpoint Reconciliation" if rejected_reconciliation else "Live Arena data" if live_arena_ok else "Arena data warning",
            "last_refreshed": datetime.now(timezone.utc).isoformat(),
            "base_url": env.get("ARENA_BASE_URL", ARENA),
            "wallet_endpoint": f"{ARENA_V1}/arena/agents/me",
            "exposure_endpoint": f"{ARENA_V1}/arena/exposure",
            "orders_endpoint": f"{ARENA_V1}/arena/orders",
        },
        "web": {
            "agent": public_agent,
            "tournament": tournament,
            "bracket": {"ok": bracket.get("ok"), "error": bracket.get("error"), "status_code": bracket.get("status_code"), "latency_ms": bracket.get("latency_ms"), "fixtures": bracket_rows, "data": bracket.get("data")},
            "leaderboard_stats": leaderboard_stats,
            "leaderboard": leaderboard,
            "recent_predictions": recent_predictions,
            "confidence_series": confidence_series,
        },
        "exposure": {
            "ok": exposure.get("ok"),
            "error": exposure.get("error"),
            "positions": positions,
        },
        "orders": {
            "ok": orders.get("ok"),
            "error": orders.get("error"),
            "orders": order_rows,
            "detail_ok": sum(1 for response in order_detail_responses.values() if response.get("ok")),
            "settlement_ok": sum(1 for response in settlement_responses.values() if response.get("ok")),
            "settlements": {
                fixture_id: response.get("data") if response.get("ok") else {"error": response.get("error")}
                for fixture_id, response in settlement_responses.items()
            },
            "data": orders.get("data") if orders.get("ok") else None,
        },
        "arena": {
            "wallet_ok": agent_me.get("ok"),
            "exposure_ok": exposure.get("ok"),
            "orders_ok": orders.get("ok"),
            "agent": {key: value for key, value in (agent_me.get("data") or {}).items() if key != "wallet"} if agent_me.get("ok") else None,
            "wallet": {
                **wallet,
                "available_balance_usdc": money_string(net_available),
                "reported_available_balance_usdc": raw_available,
                "open_position_value_usdc": money_string(position_value_usdc),
            },
            "positions": positions,
            "orders": order_rows,
        },
        "runtime": {
            "dry_run": env.get("DRY_RUN", "1") == "1",
            "mode": "DRY RUN" if env.get("DRY_RUN", "1") == "1" else "LIVE",
            "order_execution_mode": env.get("ORDER_EXECUTION_MODE", "limit"),
            "strategy_mode": env.get("STRATEGY_MODE", "winner_only"),
            "model_variant": env.get("MODEL_VARIANT", "baseline"),
            "submit_ledger": env.get("SUBMIT_LEDGER", "0") == "1",
            "ledger": "CONNECTED" if env.get("SUBMIT_LEDGER", "0") == "1" else "LOCAL ONLY",
            "use_openai": env.get("USE_OPENAI", "1") == "1",
            "max_trade_usdc": as_float(env.get("MAX_TRADE_USDC"), 1.0),
            "trade_edge_threshold_pp": as_float(env.get("TRADE_EDGE_THRESHOLD_PP"), 6.0),
        },
        "metrics": {
            "attempted_orders": len(order_rows),
            "filled_trades": filled_trade_count,
            "rejected_trades": len(rejected_orders),
            "total_orders": len(order_rows),
            "open_trades": sum(1 for order in order_rows if str(order.get("status", "")).lower() not in {"filled", "cancelled", "rejected"}),
            "open_positions": len(positions),
            "open_position_value_usdc": round(position_value_usdc, 6),
            "net_available_usdc": round(as_float(net_available), 6),
            "arena_reported_available_usdc": as_float(raw_available),
            "locked_usdc": locked_usdc,
            "unrealized_pnl_usdc": round(unrealized_pnl, 6),
            "realized_pnl_usdc": round(realized_pnl, 6),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": win_rate,
            "settled_trade_count": settled_trade_count,
            "starting_bankroll_usdc": starting_bankroll,
            "current_equity_usdc": round(current_equity, 6),
            "roi": roi,
            "average_fill_price": round(sum(as_float(order.get("open_avg_fill_price")) for order in filled_orders) / len(filled_orders), 6) if filled_orders else None,
            "average_trade_size": round(total_filled_usd / len(filled_orders), 6) if filled_orders else None,
            "average_edge_pp": round(sum(edges) / len(edges), 3) if edges else None,
            "average_confidence": round(sum(confidences) / len(confidences), 3) if confidences else None,
            "ledger_submissions": ledger_stats["submissions"],
            "reasoning_records": ledger_stats["records"],
            "latest_order_timestamp": latest_trade.get("created_at") or latest_trade.get("updated_at") if latest_trade else None,
            "qualification_target": qualification_target,
            "qualification_progress": qualification_progress,
            "qualification_remaining": max(0, qualification_target - qualification_progress),
            "qualification_status": "not currently filled" if rejected_reconciliation else "complete" if qualification_progress >= qualification_target else "in progress",
            "last_order_final_status": (latest_order or {}).get("status"),
            "last_order_rejection_reason": (latest_order or {}).get("rejection_reason") or (latest_order or {}).get("failure_reason") or (latest_order or {}).get("error"),
            "ladder_candidate_count": ladder_report.get("candidate_count", 0),
            "ladder_selected_count": len(ladder_report.get("selected_batch") or []),
            "ladder_planned_spend_usdc": ladder_report.get("total_planned_spend", 0),
            "ladder_winner_count": ladder_report.get("winner_ladder_count", 0),
            "ladder_value_slot_count": ladder_report.get("value_slot_count", 0),
            "ladder_latest_strategy": ladder_report.get("strategy_mode"),
            "rejected_reconciliation_warning": (
                "All attempted orders are rejected and exposure is empty. Do not count stale local fills toward qualification."
                if rejected_reconciliation else None
            ),
        },
        "qualification_ladder": ladder_report,
        "latest": {
            "latest_prematch": prematch,
            "latest_scan": latest_scan,
            "latest_order": latest_order,
            "latest_trade": latest_trade,
        },
        "health": {
            "arena": {"ok": agent_me.get("ok"), "latency_ms": agent_me.get("latency_ms"), "error": agent_me.get("error")},
            "exposure": {"ok": exposure.get("ok"), "latency_ms": exposure.get("latency_ms"), "error": exposure.get("error")},
            "orders": {"ok": orders.get("ok"), "latency_ms": orders.get("latency_ms"), "error": orders.get("error")},
            "order_details": {"ok": all(response.get("ok") for response in order_detail_responses.values()) if order_detail_responses else orders.get("ok"), "latency_ms": None, "error": None},
            "settlements": {"ok": all(response.get("ok") for response in settlement_responses.values()) if settlement_responses else orders.get("ok"), "latency_ms": None, "error": None},
            "ledger": {"ok": env.get("SUBMIT_LEDGER", "0") == "1", "latency_ms": None, "error": None if env.get("SUBMIT_LEDGER", "0") == "1" else "SUBMIT_LEDGER=0"},
            "tournament": {"ok": tournament.get("ok"), "latency_ms": tournament.get("latency_ms"), "error": tournament.get("error")},
            "public_agent": {"ok": public_agent.get("ok"), "latency_ms": public_agent.get("latency_ms"), "error": public_agent.get("error")},
            "bracket": {"ok": bracket.get("ok"), "latency_ms": bracket.get("latency_ms"), "error": bracket.get("error")},
            "leaderboard": {"ok": leaderboard.get("ok"), "latency_ms": leaderboard.get("latency_ms"), "error": leaderboard.get("error")},
            "leaderboard_stats": {"ok": leaderboard_stats.get("ok"), "latency_ms": leaderboard_stats.get("latency_ms"), "error": leaderboard_stats.get("error")},
        },
        "stats": {
            "fixtures_scanned": report.get("fixtures_scanned", 0),
            "ranked_count": len(ranked),
            "positive_edges": len(positive_edges),
            "trade_candidates": len(trade_candidates),
            "open_positions": len(positions),
            "orders": len(order_rows),
            "rejected_orders": len(rejected_orders),
            "errors": len(report.get("errors", [])),
            "settled_trades": settled_trade_count,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "win_rate_note": "Pending settlement." if not settled_trade_count else None,
        },
        "usage": token_usage(report, prematch),
        "best": best,
        "scan_report": report,
        "latest_prematch": prematch,
        "files": [
            {"name": p.name, "size": p.stat().st_size, "updated_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
            for p in sorted(OUTPUT.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
            if p.is_file()
        ],
    }


def run_command(args, extra_env=None, timeout=260):
    env = os.environ.copy()
    env.update(dotenv_values(ENV_PATH))
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD), **kwargs)

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self.send_json(build_status())
            return
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/api/run-scan":
            limit = params.get("limit", ["12"])[0]
            payload = run_command(
                [str(ROOT / ".venv" / "Scripts" / "python.exe"), "scripts\\scan_fixtures.py"],
                {"SCAN_LIMIT": limit, "USE_OPENAI": "0"},
            )
            self.send_json(payload)
            return
        if parsed.path == "/api/run-agent":
            fixture = params.get("fixture", ["19609156"])[0]
            payload = run_command(
                [str(ROOT / ".venv" / "Scripts" / "python.exe"), "scripts\\agent_prematch.py"],
                {"FIXTURE_ID": fixture, "USE_OPENAI": "0", "DRY_RUN": "1"},
                timeout=140,
            )
            self.send_json(payload)
            return
        self.send_json({"error": "not found"}, status=404)


def main():
    port = int(os.getenv("DASHBOARD_PORT", "8787"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Dashboard running at http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
