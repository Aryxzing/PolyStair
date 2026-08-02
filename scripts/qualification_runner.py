import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from agent_prematch import (
    ARENA,
    ARENA_V1,
    SUPABASE,
    arena_headers,
    arena_supabase_headers,
    blended_prediction,
    fetch_country_ids,
    fetch_fixture_detail,
    fetch_supabase_priors,
    get_agent_me,
    get_exposure,
    get_order_history,
    select_strategy,
)


QUALIFICATION_TARGET = 10
OUTPUT = Path("output")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def is_filled_order(order):
    return str(order.get("status", "")).lower() in {"filled", "settled", "closed"} and safe_float(order.get("usd_size_filled")) > 0


def is_rejected_order(order):
    return str(order.get("status", "")).lower() == "rejected"


def fetch_polymarket_listings(arena_key):
    response = requests.get(
        f"{ARENA}{ARENA_V1}/data/polymarket/listings",
        headers=arena_headers(arena_key),
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("fixtures") or []


def fetch_market_digest(arena_key, fixture_id, mapping):
    response = requests.get(
        f"{ARENA}{ARENA_V1}/data/polymarket/markets/{fixture_id}",
        headers=arena_headers(arena_key),
        timeout=20,
    )
    response.raise_for_status()
    market = response.json()
    mids = {}
    handles = {}
    for outcome in market.get("outcomes") or []:
        code = outcome.get("name")
        if code not in {mapping.get("home_short_code"), "draw", mapping.get("away_short_code")}:
            continue
        mid_price = outcome.get("mid_price")
        mids[code] = float(mid_price) if mid_price is not None else None
        handles[code] = {
            "team_code": code,
            "condition_id": outcome.get("condition_id"),
            "token_yes": outcome.get("token_id"),
            "current_mid_yes": mids[code],
        }
    return {
        "fixture": mapping.get("sportmonks_match_name"),
        "polymarket_event_slug": market.get("polymarket_event_slug") or mapping.get("polymarket_event_slug"),
        "ticker": market.get("polymarket_event_ticker"),
        "implied_win_prob": mids,
        "sum_implied_prob": round(sum(value for value in mids.values() if value is not None), 4),
        "execution_handles": {
            "home": handles.get(mapping.get("home_short_code")),
            "draw": handles.get("draw"),
            "away": handles.get(mapping.get("away_short_code")),
        },
        "summary": "Polymarket market and YES-token midpoints fetched from Arena production market endpoint.",
    }


def fetch_mappings_for_fixtures(fixture_ids):
    if not fixture_ids:
        return []
    ids = ",".join(str(fixture_id) for fixture_id in fixture_ids)
    response = requests.get(
        f"{SUPABASE}/rest/v1/data_sportmonks_polymarket_mapping",
        headers=arena_supabase_headers(),
        params={
            "sportmonks_fixture_id": f"in.({ids})",
            "select": (
                "sportmonks_fixture_id,polymarket_event_slug,home_country,home_short_code,"
                "away_country,away_short_code,sportmonks_match_name,sportmonks_kickoff_utc"
            ),
            "order": "sportmonks_kickoff_utc.asc",
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def filled_trade_keys(orders):
    keys = set()
    for order in orders:
        if not is_filled_order(order):
            continue
        fixture_id = str(order.get("fixture_id") or "")
        team_code = str(order.get("team_code") or "")
        if fixture_id and team_code:
            keys.add((fixture_id, team_code))
    return keys


def open_position_fixtures(positions):
    return {
        str(position.get("fixture_id"))
        for position in positions
        if position.get("fixture_id")
    }


def market_favorite(market):
    if not market:
        return None
    valid = [(key, value) for key, value in market.items() if value is not None]
    if not valid:
        return None
    return max(valid, key=lambda item: safe_float(item[1]))[0]


def matchup_cache_key(mapping):
    return tuple(sorted([mapping["home_country"], mapping["away_country"]]))


def evaluate_fixture(arena_key, mapping, model_variant, strategy_mode, priors_cache):
    fixture_id = str(mapping["sportmonks_fixture_id"])
    fixture, _home, _away = fetch_fixture_detail(arena_key, fixture_id)
    cache_key = matchup_cache_key(mapping)
    if cache_key not in priors_cache:
        country_rows = fetch_country_ids(mapping["home_country"], mapping["away_country"])
        country_ids = [row["country_id"] for row in country_rows.values()]
        priors_cache[cache_key] = {
            "country_rows": country_rows,
            "priors": fetch_supabase_priors(country_ids),
        }
    country_rows = priors_cache[cache_key]["country_rows"]
    priors = priors_cache[cache_key]["priors"]
    prediction = blended_prediction(fixture, mapping, priors, country_rows, model_variant=model_variant)
    poly_digest = fetch_market_digest(arena_key, fixture_id, mapping)
    if not any(value is not None for value in (poly_digest.get("implied_win_prob") or {}).values()):
        raise RuntimeError("market midpoints missing for all outcomes")
    strategy = select_strategy(prediction, poly_digest, mapping, strategy_mode)
    return {
        "fixture_id": fixture_id,
        "match": mapping.get("sportmonks_match_name") or fixture.get("name") or poly_digest.get("fixture"),
        "kickoff_utc": mapping.get("sportmonks_kickoff_utc") or fixture.get("starting_at"),
        "selected_outcome": strategy.get("outcome"),
        "model_winner": prediction.get("outcome"),
        "selected_value_outcome": strategy.get("selected_value_outcome"),
        "execution_outcome": strategy.get("execution_outcome") or strategy.get("outcome"),
        "execution_selector": strategy.get("execution_selector"),
        "execution_reason": strategy.get("execution_reason"),
        "market_favorite": market_favorite(poly_digest.get("implied_win_prob")),
        "edge_pp": safe_float(strategy.get("edge_pp")),
        "confidence": prediction.get("confidence_level"),
        "confidence_score": int(prediction.get("confidence_score") or 0),
        "data_quality": safe_float(prediction.get("data_quality")),
        "source_agreement": int(prediction.get("source_agreement") or 0),
        "market_midpoint": strategy.get("market_midpoint"),
        "size_usdc": safe_float(strategy.get("size_usdc")),
        "should_trade": bool(strategy.get("should_trade")),
        "opportunity_class": strategy.get("opportunity_class"),
        "no_trade_reason": strategy.get("no_trade_reason"),
        "recommended_action": strategy.get("recommended_action"),
        "strategy": strategy,
        "prediction": prediction,
    }


def rank_key(row):
    return (
        1 if row.get("should_trade") else 0,
        safe_float(row.get("edge_pp")),
        int(row.get("confidence_score") or 0),
        safe_float(row.get("data_quality")),
        int(row.get("source_agreement") or 0),
    )


def print_candidate_table(candidates):
    if not candidates:
        print("No candidates available after exclusions.")
        return
    headers = ["#", "fixture", "match", "exec", "winner", "value", "selector", "edge", "conf", "mid", "size"]
    print("\nCandidate table")
    print("-" * 156)
    print(
        f"{headers[0]:>2}  {headers[1]:<10}  {headers[2]:<28}  {headers[3]:<8}  "
        f"{headers[4]:<8}  {headers[5]:<8}  {headers[6]:<15}  {headers[7]:>7}  "
        f"{headers[8]:<11}  {headers[9]:>7}  {headers[10]:>6}"
    )
    print("-" * 156)
    for index, row in enumerate(candidates, start=1):
        match = (row.get("match") or "N/A")[:28]
        confidence = f"{row.get('confidence')} {row.get('confidence_score')}"
        midpoint = row.get("market_midpoint")
        midpoint_text = "N/A" if midpoint is None else f"{safe_float(midpoint):.3f}"
        print(
            f"{index:>2}  {row['fixture_id']:<10}  {match:<28}  {row.get('execution_outcome') or row.get('selected_outcome') or 'N/A':<8}  "
            f"{row.get('model_winner') or 'N/A':<8}  {row.get('selected_value_outcome') or 'N/A':<8}  "
            f"{row.get('execution_selector') or 'N/A':<15}  {safe_float(row.get('edge_pp')):>+7.2f}  "
            f"{confidence:<11}  {midpoint_text:>7}  {safe_float(row.get('size_usdc')):>6.2f}"
        )
    print("-" * 156)


def write_reports(report):
    OUTPUT.mkdir(exist_ok=True)
    json_path = OUTPUT / "qualification_progress.json"
    md_path = OUTPUT / "qualification_progress.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        "# Qualification Progress",
        "",
        f"Generated: {report['generated_at']}",
        f"Progress: {report['filled_trades']}/{report['qualification_target']}",
        f"Attempted orders: {report.get('attempted_orders', 0)}",
        f"Rejected orders: {report.get('rejected_trades', 0)}",
        f"Remaining: {report['remaining']}",
        f"Wallet available: {report.get('wallet', {}).get('available_balance_usdc', 'N/A')}",
        f"Mode: model `{report['settings']['model_variant']}`, strategy `{report['settings']['strategy_mode']}`",
        f"Min edge: {report['settings']['min_edge_pp']} pp",
        "",
        "## Safety Status",
        "",
        report.get("safety_warning") or "No rejected-order safety warning.",
        "",
        "## Candidate Table",
        "",
        "| # | Fixture | Match | Executed outcome | Model winner | Value candidate | Hybrid selector | Edge pp | Confidence | Market mid | Size USDC | Decision | Reason |",
        "|---:|---|---|---|---|---|---|---:|---|---:|---:|---|---|",
    ]
    for index, row in enumerate(report["candidate_table"], start=1):
        midpoint = row.get("market_midpoint")
        lines.append(
            f"| {index} | {row['fixture_id']} | {row.get('match') or 'N/A'} | {row.get('execution_outcome') or row.get('selected_outcome') or 'N/A'} | "
            f"{row.get('model_winner') or 'N/A'} | {row.get('selected_value_outcome') or 'N/A'} | {row.get('execution_selector') or 'N/A'} | {safe_float(row.get('edge_pp')):.2f} | "
            f"{row.get('confidence')} {row.get('confidence_score')} | {safe_float(midpoint):.4f} | "
            f"{safe_float(row.get('size_usdc')):.2f} | {'TRADE' if row.get('should_trade') else row.get('no_trade_reason') or 'no trade'} | "
            f"{row.get('execution_reason') or row.get('no_trade_reason') or 'N/A'} |"
        )
    lines.extend(
        [
            "",
            "## Rejected Order History",
            "",
        ]
    )
    if report.get("rejected_order_history"):
        lines.extend(
            [
                "| Order | Fixture | Team | Requested | Filled | Limit | Reason |",
                "|---|---|---|---:|---:|---:|---|",
            ]
        )
        for order in report["rejected_order_history"]:
            lines.append(
                f"| {order.get('order_id') or order.get('id') or 'N/A'} | {order.get('fixture_id') or 'N/A'} | "
                f"{order.get('team_code') or 'N/A'} | {order.get('usd_size_requested') or order.get('usd_size') or 'N/A'} | "
                f"{order.get('usd_size_filled') or '0'} | {order.get('open_limit_price') or order.get('limit_price') or 'N/A'} | "
                f"{order.get('rejection_reason') or order.get('failure_reason') or order.get('error') or 'N/A'} |"
            )
    else:
        lines.append("Rejected order history hidden. Re-run with `--include-rejected-history` to include it in this report.")
    lines.extend(
        [
            "",
            "## Last Attempt",
            "",
            "```json",
            json.dumps(report.get("last_attempt_result"), indent=2, sort_keys=True),
            "```",
        ]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(json_path), str(md_path)


def run_agent_for_candidate(candidate, args, env):
    cmd = [
        sys.executable,
        "scripts/agent_prematch.py",
        "--fixture",
        str(candidate["fixture_id"]),
        "--model-variant",
        args.model_variant,
        "--strategy-mode",
        args.strategy_mode,
    ]
    child_env = dict(env)
    if args.dry_run:
        child_env["DRY_RUN"] = "1"
    child_env["USE_OPENAI"] = "0"
    print("\nLaunching existing agent flow:")
    print(" ".join(cmd))
    print("Existing REQUIRE_LIVE_CONFIRM behavior remains active.")
    completed = subprocess.run(cmd, env=child_env, cwd=Path.cwd())
    return {"command": cmd, "returncode": completed.returncode}


def parse_args():
    parser = argparse.ArgumentParser(description="Safe one-trade-at-a-time qualification helper.")
    parser.add_argument("--dry-run", action="store_true", help="Force DRY_RUN=1 when launching agent_prematch.")
    parser.add_argument("--max-candidates", type=int, default=10)
    parser.add_argument("--scan-all", action="store_true", help="Evaluate every available fixture instead of the first max-candidates fixtures.")
    parser.add_argument("--min-edge", type=float, default=None)
    parser.add_argument("--fixture", type=str, default=None, help="Evaluate one fixture only.")
    parser.add_argument("--no-order", action="store_true", help="Analysis only. Never launch the agent order flow.")
    parser.add_argument("--exclude-open-position-fixtures", dest="exclude_open_position_fixtures", action="store_true", default=True)
    parser.add_argument("--include-open-position-fixtures", dest="exclude_open_position_fixtures", action="store_false")
    parser.add_argument("--include-rejected-history", action="store_true", help="Include rejected order rows in the progress report.")
    parser.add_argument("--model-variant", default=os.getenv("MODEL_VARIANT", "research_602020"))
    parser.add_argument("--strategy-mode", default=os.getenv("STRATEGY_MODE", "three_outcome_value"))
    return parser.parse_args()


def main():
    load_dotenv()
    args = parse_args()
    arena_key = os.getenv("ARENA_KEY")
    if not arena_key:
        print("ARENA_KEY is missing. Add it to .env first.", file=sys.stderr)
        return 2

    min_edge = args.min_edge if args.min_edge is not None else safe_float(os.getenv("TRADE_EDGE_THRESHOLD_PP", "6"), 6.0)
    max_trade_usdc = safe_float(os.getenv("MAX_TRADE_USDC", "1"), 1.0)
    if max_trade_usdc > 1:
        print("Safety stop: MAX_TRADE_USDC is above 1. Lower it before using qualification_runner.", file=sys.stderr)
        return 2

    orders_data = get_order_history(arena_key)
    orders = orders_data.get("orders") or []
    filled_orders = [order for order in orders if is_filled_order(order)]
    rejected_orders = [order for order in orders if is_rejected_order(order)]
    exposure_data = get_exposure(arena_key, include_closing=True)
    positions = exposure_data.get("positions") or []
    wallet = (get_agent_me(arena_key).get("wallet") or {})
    filled_keys = filled_trade_keys(orders)
    blocked_fixtures = open_position_fixtures(positions) if args.exclude_open_position_fixtures else set()

    print(f"Qualification progress: {len(filled_orders)}/{QUALIFICATION_TARGET}")
    print(f"Attempted orders: {len(orders)}")
    print(f"Rejected orders: {len(rejected_orders)}")
    print(f"Open positions: {len(positions)}")
    print(f"Wallet available: {wallet.get('available_balance_usdc', 'N/A')}")
    if rejected_orders:
        print("SAFETY WARNING: previous rejected orders exist. Do not rerun without explicit confirmation.")
    print(f"Mode: model_variant={args.model_variant}, strategy_mode={args.strategy_mode}, min_edge={min_edge} pp")
    if args.no_order:
        print("No-order mode: analysis only. No agent execution or ledger submission will be launched.")

    if args.fixture:
        fixture_ids = [str(args.fixture)]
    else:
        listings = fetch_polymarket_listings(arena_key)
        fixture_ids = [str(row.get("fixture_id")) for row in listings if row.get("fixture_id")]
    mappings = fetch_mappings_for_fixtures(fixture_ids)
    if not args.fixture and not args.scan_all:
        mappings = mappings[: max(1, args.max_candidates)]
        print(f"Evaluating first {len(mappings)} fixtures. Use --scan-all for a full slower pass.", flush=True)

    candidates = []
    errors = []
    priors_cache = {}
    for index, mapping in enumerate(mappings, start=1):
        fixture_id = str(mapping["sportmonks_fixture_id"])
        if fixture_id in blocked_fixtures:
            continue
        try:
            row = evaluate_fixture(arena_key, mapping, args.model_variant, args.strategy_mode, priors_cache)
            if (row["fixture_id"], str(row.get("selected_outcome"))) in filled_keys:
                row["already_filled_same_fixture_outcome"] = True
                continue
            if row["edge_pp"] >= min_edge or row["should_trade"]:
                candidates.append(row)
        except Exception as exc:
            errors.append({"fixture_id": fixture_id, "error": str(exc)})
            print(f"Skipping fixture {fixture_id}: {exc}", file=sys.stderr, flush=True)
        if index % 10 == 0:
            print(f"Evaluated {index}/{len(mappings)} fixtures...", flush=True)

    ranked = sorted(candidates, key=rank_key, reverse=True)
    display_candidates = ranked[: max(1, args.max_candidates)]
    print_candidate_table(display_candidates)

    last_attempt = {"attempted": False, "reason": "analysis only"}
    if not args.no_order and display_candidates:
        choice = input("\nChoose candidate number to run one existing agent flow, or press Enter to skip: ").strip()
        if choice:
            try:
                selected = display_candidates[int(choice) - 1]
            except (ValueError, IndexError):
                last_attempt = {"attempted": False, "reason": f"invalid selection {choice!r}"}
            else:
                last_attempt = {"attempted": True, "fixture_id": selected["fixture_id"], **run_agent_for_candidate(selected, args, os.environ)}
        else:
            last_attempt = {"attempted": False, "reason": "user skipped selection"}

    refresh_errors = []
    try:
        refreshed_orders = get_order_history(arena_key).get("orders") or []
    except requests.RequestException as exc:
        refresh_errors.append({"source": "orders_refresh", "error": str(exc)})
        refreshed_orders = orders
    try:
        refreshed_positions = get_exposure(arena_key, include_closing=True).get("positions") or []
    except requests.RequestException as exc:
        refresh_errors.append({"source": "exposure_refresh", "error": str(exc)})
        refreshed_positions = positions
    refreshed_filled = [order for order in refreshed_orders if is_filled_order(order)]
    refreshed_rejected = [order for order in refreshed_orders if is_rejected_order(order)]
    try:
        refreshed_wallet = (get_agent_me(arena_key).get("wallet") or {})
    except requests.RequestException as exc:
        refresh_errors.append({"source": "wallet_refresh", "error": str(exc)})
        refreshed_wallet = wallet
    safety_warning = None
    if refreshed_rejected:
        safety_warning = (
            f"{len(refreshed_rejected)} rejected orders are present. Rejected orders do not count toward qualification; "
            "do not rerun live submissions without explicit confirmation."
        )

    report = {
        "generated_at": utc_now(),
        "filled_trades": len(refreshed_filled),
        "attempted_orders": len(refreshed_orders),
        "rejected_trades": len(refreshed_rejected),
        "qualification_target": QUALIFICATION_TARGET,
        "remaining": max(0, QUALIFICATION_TARGET - len(refreshed_filled)),
        "orders": refreshed_orders,
        "wallet": {
            "available_balance_usdc": refreshed_wallet.get("available_balance_usdc"),
            "locked_balance_usdc": refreshed_wallet.get("locked_balance_usdc"),
        },
        "open_positions": refreshed_positions,
        "rejected_order_history": refreshed_rejected if args.include_rejected_history else [],
        "safety_warning": safety_warning,
        "candidate_table": display_candidates,
        "candidate_count_total_after_filters": len(ranked),
        "errors": errors + refresh_errors,
        "last_attempt_result": last_attempt,
        "settings": {
            "model_variant": args.model_variant,
            "strategy_mode": args.strategy_mode,
            "min_edge_pp": min_edge,
            "max_trade_usdc": max_trade_usdc,
            "dry_run_for_agent_launch": bool(args.dry_run),
            "no_order": bool(args.no_order),
            "exclude_open_position_fixtures": bool(args.exclude_open_position_fixtures),
        },
    }
    json_path, md_path = write_reports(report)
    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")
    print(f"Refreshed progress: {len(refreshed_filled)}/{QUALIFICATION_TARGET}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        body = getattr(response, "text", "")
        print(f"Network/API error: HTTP {status if status is not None else 'unknown'} - {exc}", file=sys.stderr)
        if body:
            print(body[:2000], file=sys.stderr)
        sys.exit(1)
