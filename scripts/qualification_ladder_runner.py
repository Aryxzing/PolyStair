import argparse
import json
import os
import sys
import time
import uuid
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
    build_records,
    fetch_country_ids,
    fetch_fixture_detail,
    fetch_supabase_priors,
    get_agent_me,
    get_match_timing,
    get_order_history,
    maybe_submit_ledger,
    poll_order_until_terminal,
    post_json,
    select_strategy,
    sportmonks_digest,
)


OUTPUT = Path("output")
TARGET_TRADES = 10
CONFIRMATION = "CONFIRM LIVE FOK QUALIFICATION BATCH"
TERMINAL_FILLED = {"completed", "filled", "settled", "closed"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def retry_scan_request(label, fn):
    delays = [2, 5, 10]
    last_exc = None
    for attempt, delay in enumerate(delays, start=1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt == len(delays):
                break
            print(f"{label} failed on attempt {attempt}/3: {exc}. Retrying in {delay}s...", file=sys.stderr, flush=True)
            time.sleep(delay)
    raise last_exc


def fetch_polymarket_listings(arena_key):
    response = requests.get(
        f"{ARENA}{ARENA_V1}/data/polymarket/listings",
        headers=arena_headers(arena_key),
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("fixtures") or []


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


def fetch_market_digest(arena_key, fixture_id, mapping):
    response = requests.get(
        f"{ARENA}{ARENA_V1}/data/polymarket/markets/{fixture_id}",
        headers=arena_headers(arena_key),
        timeout=20,
    )
    response.raise_for_status()
    market = response.json()
    mids = {}
    for outcome in market.get("outcomes") or []:
        code = outcome.get("name")
        if code not in {mapping.get("home_short_code"), "draw", mapping.get("away_short_code")}:
            continue
        mid_price = outcome.get("mid_price")
        mids[code] = float(mid_price) if mid_price is not None else None
    return {
        "fixture": mapping.get("sportmonks_match_name"),
        "polymarket_event_slug": market.get("polymarket_event_slug") or mapping.get("polymarket_event_slug"),
        "ticker": market.get("polymarket_event_ticker"),
        "implied_win_prob": mids,
        "sum_implied_prob": round(sum(value for value in mids.values() if value is not None), 4),
        "summary": "Polymarket market and YES-token midpoints fetched from Arena production market endpoint.",
    }


def matchup_cache_key(mapping):
    return tuple(sorted([mapping["home_country"], mapping["away_country"]]))


def is_filled_order(order):
    return str(order.get("status", "")).lower() in TERMINAL_FILLED and safe_float(order.get("usd_size_filled")) > 0


def already_filled_fixture_ids(orders):
    return {
        str(order.get("fixture_id"))
        for order in orders
        if order.get("fixture_id") and is_filled_order(order)
    }


def is_rejected_order(order):
    return str(order.get("status", "")).lower() == "rejected"


def rejected_history_for(orders, fixture_id, outcome):
    return [
        order for order in orders
        if is_rejected_order(order)
        and str(order.get("fixture_id")) == str(fixture_id)
        and str(order.get("team_code")) == str(outcome)
    ]


def winner_bucket(probability, market_midpoint):
    if probability >= 0.70 and market_midpoint <= 0.85:
        return {"name": "70_plus", "rank": 3, "suggested_size": 5.0, "max_market_price": 0.85}
    if 0.60 <= probability < 0.70 and market_midpoint <= 0.75:
        return {"name": "60_70", "rank": 2, "suggested_size": 2.0, "max_market_price": 0.75}
    if 0.50 <= probability < 0.60 and market_midpoint <= 0.65:
        return {"name": "50_60", "rank": 1, "suggested_size": 1.0, "max_market_price": 0.65}
    return None


def fok_worst_price(market_midpoint, candidate_type):
    buffer_pp = 1.0 if candidate_type == "value_slot" else 1.5
    return round(min(0.99, float(market_midpoint) + buffer_pp / 100.0), 4)


def value_sort_key(row):
    # Prefer a non-draw underdog if it is within roughly 2 pp of a draw's value score.
    return (
        safe_float(row.get("edge_pp")) + (2.0 if row.get("outcome") != "draw" else 0.0),
        safe_float(row.get("edge_pp")),
        -safe_float(row.get("market_midpoint"), 1.0),
    )


def evaluate_fixture(arena_key, mapping, priors_cache, orders):
    fixture_id = str(mapping["sportmonks_fixture_id"])
    timing = get_match_timing(arena_key, fixture_id)
    current_window = timing.get("current_window")
    if current_window != "PRE_MATCH":
        return {"fixture_id": fixture_id, "match": mapping.get("sportmonks_match_name"), "skipped": True, "reason": f"window is {current_window}"}

    fixture, home, away = fetch_fixture_detail(arena_key, fixture_id)
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
    market_digest = fetch_market_digest(arena_key, fixture_id, mapping)
    prediction = blended_prediction(fixture, mapping, priors, country_rows, model_variant=os.getenv("MODEL_VARIANT", "research_602020"))
    strategy = select_strategy(prediction, market_digest, mapping, "qualification_probability_ladder")
    all_edges = strategy.get("all_outcome_edges") or []

    model_winner = prediction.get("outcome")
    model_probability = safe_float(prediction.get("probability"))
    confidence_score = int(prediction.get("confidence_score") or 0)
    source_agreement = int(prediction.get("source_agreement") or 0)
    winner_market = market_digest.get("implied_win_prob", {}).get(model_winner)
    winner_market_float = safe_float(winner_market, None)
    winner_edge_pp = round((model_probability - winner_market_float) * 100, 2) if winner_market_float is not None else None
    bucket = winner_bucket(model_probability, winner_market_float) if winner_market_float is not None else None

    value_rows = [
        row for row in all_edges
        if row.get("outcome") != model_winner
        and row.get("market_midpoint") is not None
    ]
    value_rows.sort(key=value_sort_key, reverse=True)
    value_candidate = value_rows[0] if value_rows else None

    winner_candidate = None
    if (
        model_winner != "draw"
        and bucket
        and confidence_score >= 65
        and source_agreement >= 2
        and winner_market_float is not None
        and 0 < winner_market_float < 1
    ):
        price_reasonableness = max(0.0, 100.0 - abs(winner_edge_pp or 0.0))
        winner_candidate = {
            "rank": None,
            "fixture_id": fixture_id,
            "match": mapping.get("sportmonks_match_name") or fixture.get("name") or market_digest.get("fixture"),
            "kickoff_utc": mapping.get("sportmonks_kickoff_utc") or fixture.get("starting_at"),
            "execution_outcome": model_winner,
            "model_winner": model_winner,
            "model_probability": round(model_probability, 4),
            "market_midpoint": round(winner_market_float, 4),
            "winner_edge_pp": winner_edge_pp,
            "confidence_score": confidence_score,
            "confidence_level": prediction.get("confidence_level"),
            "source_agreement": source_agreement,
            "component_disagreement": safe_float(prediction.get("component_disagreement")),
            "all_outcome_edges": all_edges,
            "best_value_candidate": value_candidate.get("outcome") if value_candidate else None,
            "value_candidate_edge_pp": round(safe_float(value_candidate.get("edge_pp")), 2) if value_candidate else None,
            "value_candidate_market_price": round(safe_float(value_candidate.get("market_midpoint")), 4) if value_candidate else None,
            "suggested_size": bucket["suggested_size"],
            "candidate_type": "winner_ladder",
            "ladder_bucket": bucket["name"],
            "bucket_rank": bucket["rank"],
            "fok_buffer_pp": 1.5,
            "fok_worst_price": fok_worst_price(winner_market_float, "winner_ladder"),
            "price_reasonableness_score": round(price_reasonableness, 2),
            "reason": (
                f"Model winner {model_winner} is {model_probability:.2%}, price {winner_market_float:.2%}, "
                f"bucket {bucket['name']}, confidence {confidence_score}, source agreement {source_agreement}."
            ),
            "risk_notes": prediction.get("risk_factors", []),
            "prior_rejected_count": len(rejected_history_for(orders, fixture_id, model_winner)),
            "strategy": strategy,
            "prediction": prediction,
            "data": {
                "mapping": mapping,
                "match_timing": {"current_window": current_window, "server_ts_utc": timing.get("server_ts_utc"), "raw": timing},
                "sportmonks_digest": sportmonks_digest(fixture, home, away),
                "polymarket_digest": market_digest,
                "supabase_digest": {
                    "countries": country_rows,
                    "tables": {table: {"rows": len(rows), "sample": rows[:1]} for table, rows in priors.items()},
                    "summary": "StatsBomb-derived country priors fetched from the world_cup_arena Supabase schema.",
                },
            },
        }

    value_candidate_row = None
    if value_candidate:
        value_outcome = value_candidate.get("outcome")
        value_market = safe_float(value_candidate.get("market_midpoint"), None)
        value_edge = safe_float(value_candidate.get("edge_pp"))
        if (
            value_outcome != model_winner
            and value_edge >= 15.0
            and confidence_score >= 85
            and source_agreement >= 3
            and value_market is not None
            and value_market <= 0.20
        ):
            value_candidate_row = {
                "rank": None,
                "fixture_id": fixture_id,
                "match": mapping.get("sportmonks_match_name") or fixture.get("name") or market_digest.get("fixture"),
                "kickoff_utc": mapping.get("sportmonks_kickoff_utc") or fixture.get("starting_at"),
                "execution_outcome": value_outcome,
                "model_winner": model_winner,
                "model_probability": round(safe_float(value_candidate.get("model_probability")), 4),
                "winner_model_probability": round(model_probability, 4),
                "market_midpoint": round(value_market, 4),
                "winner_market_midpoint": round(winner_market_float, 4) if winner_market_float is not None else None,
                "edge_pp": round(value_edge, 2),
                "confidence_score": confidence_score,
                "confidence_level": prediction.get("confidence_level"),
                "source_agreement": source_agreement,
                "component_disagreement": safe_float(prediction.get("component_disagreement")),
                "all_outcome_edges": all_edges,
                "best_value_candidate": value_outcome,
                "value_candidate_edge_pp": round(value_edge, 2),
                "value_candidate_market_price": round(value_market, 4),
                "suggested_size": 1.0,
                "candidate_type": "value_slot",
                "ladder_bucket": "value_slot",
                "bucket_rank": 0,
                "fok_buffer_pp": 1.0,
                "fok_worst_price": fok_worst_price(value_market, "value_slot"),
                "price_reasonableness_score": round(max(0.0, 100.0 - abs(value_edge)), 2),
                "reason": (
                    f"Strict value slot: {value_outcome} edge {value_edge:.2f} pp, price {value_market:.2%}, "
                    f"confidence {confidence_score}, source agreement {source_agreement}."
                ),
                "risk_notes": prediction.get("risk_factors", []),
                "prior_rejected_count": len(rejected_history_for(orders, fixture_id, value_outcome)),
                "strategy": {
                    **strategy,
                    "outcome": value_outcome,
                    "execution_outcome": value_outcome,
                    "candidate_type": "value_slot",
                    "ladder_bucket": "value_slot",
                    "size_usdc": 1.0,
                    "suggested_size": 1.0,
                    "limit_price": round(value_market, 4),
                    "market_midpoint": round(value_market, 4),
                    "edge_pp": round(value_edge, 2),
                    "should_trade": True,
                    "orders": [{"outcome": value_outcome, "side": "buy_yes", "size_usdc": 1.0, "limit_price": round(value_market, 4)}],
                    "execution_reason": f"Strict value slot selected {value_outcome} with edge {value_edge:.2f} pp.",
                },
                "prediction": prediction,
                "data": {
                    "mapping": mapping,
                    "match_timing": {"current_window": current_window, "server_ts_utc": timing.get("server_ts_utc"), "raw": timing},
                    "sportmonks_digest": sportmonks_digest(fixture, home, away),
                    "polymarket_digest": market_digest,
                    "supabase_digest": {
                        "countries": country_rows,
                        "tables": {table: {"rows": len(rows), "sample": rows[:1]} for table, rows in priors.items()},
                        "summary": "StatsBomb-derived country priors fetched from the world_cup_arena Supabase schema.",
                    },
                },
            }

    return {
        "fixture_id": fixture_id,
        "match": mapping.get("sportmonks_match_name") or fixture.get("name"),
        "current_window": current_window,
        "winner_candidate": winner_candidate,
        "value_candidate": value_candidate_row,
        "model_winner": model_winner,
        "winner_market_midpoint": winner_market_float,
        "confidence_score": confidence_score,
        "source_agreement": source_agreement,
    }


def winner_rank_key(candidate):
    return (
        int(candidate.get("bucket_rank") or 0),
        int(candidate.get("confidence_score") or 0),
        int(candidate.get("source_agreement") or 0),
        safe_float(candidate.get("price_reasonableness_score")),
        -safe_float(candidate.get("component_disagreement")),
    )


def value_rank_key(candidate):
    return (
        safe_float(candidate.get("edge_pp") or candidate.get("value_candidate_edge_pp")),
        1 if candidate.get("execution_outcome") != "draw" else 0,
        int(candidate.get("confidence_score") or 0),
        -safe_float(candidate.get("market_midpoint"), 1.0),
    )


def build_batch(winner_candidates, value_candidates):
    selected_value = sorted(value_candidates, key=value_rank_key, reverse=True)[:1]
    winner_limit = TARGET_TRADES - len(selected_value)
    selected = sorted(winner_candidates, key=winner_rank_key, reverse=True)[:winner_limit]
    if selected_value:
        selected.append(selected_value[0])
    selected = selected[:TARGET_TRADES]
    selected.sort(
        key=lambda candidate: (
            0 if candidate.get("candidate_type") == "winner_ladder" else 1,
            -safe_float(candidate.get("bucket_rank")),
            -safe_float(candidate.get("confidence_score")),
            -safe_float(candidate.get("model_probability")),
        )
    )
    for index, candidate in enumerate(selected, start=1):
        candidate["rank"] = index
    return selected


def load_extra_fallback_candidates(selected):
    extra_path = OUTPUT / "qualification_extra_candidates.json"
    if not extra_path.exists() or len(selected) >= TARGET_TRADES:
        return []
    try:
        extra_report = json.loads(extra_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    selected_keys = {
        (str(candidate.get("fixture_id")), str(candidate.get("execution_outcome")))
        for candidate in selected
    }
    extras = []
    for row in extra_report.get("recommended_best_2") or []:
        key = (str(row.get("fixture_id")), str(row.get("model_winner")))
        if key in selected_keys or not row.get("passes_filter"):
            continue
        market_midpoint = safe_float(row.get("winner_market_midpoint"))
        candidate = {
            "rank": None,
            "fixture_id": str(row["fixture_id"]),
            "match": row.get("match"),
            "kickoff_utc": row.get("kickoff_utc"),
            "execution_outcome": row.get("model_winner"),
            "model_winner": row.get("model_winner"),
            "model_probability": safe_float(row.get("model_probability")),
            "market_midpoint": market_midpoint,
            "winner_edge_pp": safe_float(row.get("winner_edge_pp")),
            "confidence_score": int(row.get("confidence_score") or 0),
            "confidence_level": row.get("confidence_level"),
            "source_agreement": int(row.get("source_agreement") or 0),
            "component_disagreement": safe_float(row.get("component_disagreement")),
            "all_outcome_edges": [],
            "best_value_candidate": None,
            "value_candidate_edge_pp": None,
            "value_candidate_market_price": None,
            "suggested_size": 1.0,
            "candidate_type": "winner_ladder",
            "ladder_bucket": "fallback_42_50",
            "bucket_rank": 0,
            "fok_buffer_pp": 1.5,
            "fok_worst_price": fok_worst_price(market_midpoint, "winner_ladder"),
            "price_reasonableness_score": round(max(0.0, 100.0 - abs(safe_float(row.get("winner_edge_pp")))), 2),
            "reason": (
                f"Fallback winner ladder: model winner {row.get('model_winner')} is "
                f"{safe_float(row.get('model_probability')):.2%}, price {market_midpoint:.2%}, "
                f"edge {safe_float(row.get('winner_edge_pp')):.2f} pp, confidence {row.get('confidence_score')}, "
                f"source agreement {row.get('source_agreement')}."
            ),
            "risk_notes": row.get("risk_factors", []),
            "prior_rejected_count": int(row.get("prior_rejected_same_fixture_count") or 0),
            "fallback_source": "output/qualification_extra_candidates.json",
        }
        extras.append(candidate)
        if len(selected) + len(extras) >= TARGET_TRADES:
            break
    return extras


def build_fok_payload(candidate):
    return {
        "fixture_id": str(candidate["fixture_id"]),
        "team_code": str(candidate["execution_outcome"]),
        "usd_size": f"{safe_float(candidate['suggested_size']):.2f}",
        "worst_price": candidate["fok_worst_price"],
        "idempotency_key": str(uuid.uuid4()),
    }


def refresh_selected_candidate(arena_key, candidate, priors_cache=None):
    priors_cache = priors_cache if priors_cache is not None else {}
    current_orders = get_order_history(arena_key).get("orders") or []
    mapping = fetch_mappings_for_fixtures([candidate["fixture_id"]])[0]
    refreshed = evaluate_fixture(arena_key, mapping, priors_cache, current_orders)
    if candidate.get("ladder_bucket") == "fallback_42_50":
        return evaluate_fallback_candidate(arena_key, mapping, priors_cache, current_orders)
    if candidate["candidate_type"] == "winner_ladder":
        return refreshed.get("winner_candidate")
    return refreshed.get("value_candidate")


def public_candidate(candidate):
    hidden = dict(candidate)
    hidden.pop("data", None)
    hidden.pop("prediction", None)
    hidden.pop("strategy", None)
    return hidden


def evaluate_fallback_candidate(arena_key, mapping, priors_cache, orders):
    fixture_id = str(mapping["sportmonks_fixture_id"])
    timing = get_match_timing(arena_key, fixture_id)
    if timing.get("current_window") != "PRE_MATCH":
        return None
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
    market_digest = fetch_market_digest(arena_key, fixture_id, mapping)
    prediction = blended_prediction(fixture, mapping, priors, country_rows, model_variant=os.getenv("MODEL_VARIANT", "research_602020"))
    model_winner = prediction.get("outcome")
    winner_market = (market_digest.get("implied_win_prob") or {}).get(model_winner)
    if model_winner == "draw" or winner_market is None:
        return None
    model_probability = safe_float(prediction.get("probability"))
    market_midpoint = safe_float(winner_market)
    edge_pp = round((model_probability - market_midpoint) * 100, 2)
    confidence_score = int(prediction.get("confidence_score") or 0)
    source_agreement = int(prediction.get("source_agreement") or 0)
    if not (
        0 < market_midpoint < 1
        and market_midpoint <= 0.80
        and confidence_score >= 63
        and source_agreement >= 2
        and model_probability >= 0.42
        and edge_pp >= -18
    ):
        return None
    return {
        "rank": None,
        "fixture_id": fixture_id,
        "match": mapping.get("sportmonks_match_name") or fixture.get("name"),
        "kickoff_utc": mapping.get("sportmonks_kickoff_utc") or fixture.get("starting_at"),
        "execution_outcome": model_winner,
        "model_winner": model_winner,
        "model_probability": round(model_probability, 4),
        "market_midpoint": round(market_midpoint, 4),
        "winner_edge_pp": edge_pp,
        "confidence_score": confidence_score,
        "confidence_level": prediction.get("confidence_level"),
        "source_agreement": source_agreement,
        "component_disagreement": safe_float(prediction.get("component_disagreement")),
        "all_outcome_edges": [],
        "best_value_candidate": None,
        "value_candidate_edge_pp": None,
        "value_candidate_market_price": None,
        "suggested_size": 1.0,
        "candidate_type": "winner_ladder",
        "ladder_bucket": "fallback_42_50",
        "bucket_rank": 0,
        "fok_buffer_pp": 1.5,
        "fok_worst_price": fok_worst_price(market_midpoint, "winner_ladder"),
        "price_reasonableness_score": round(max(0.0, 100.0 - abs(edge_pp)), 2),
        "reason": (
            f"Fallback winner ladder: model winner {model_winner} is {model_probability:.2%}, "
            f"price {market_midpoint:.2%}, edge {edge_pp:.2f} pp, confidence {confidence_score}, "
            f"source agreement {source_agreement}."
        ),
        "risk_notes": prediction.get("risk_factors", []),
        "prior_rejected_count": len(rejected_history_for(orders, fixture_id, model_winner)),
        "fallback_source": "live refresh",
    }


def print_batch(selected):
    if not selected:
        print("No safe ladder candidates selected.")
        return
    print("\nSelected qualification ladder batch")
    print("-" * 168)
    print(f"{'#':>2}  {'fixture':<10}  {'match':<28}  {'outcome':<8}  {'type':<14}  {'bucket':<9}  {'prob':>7}  {'mid':>7}  {'conf':>5}  {'src':>3}  {'size':>6}  {'worst':>7}  reason")
    print("-" * 168)
    for candidate in selected:
        match = (candidate.get("match") or "N/A")[:28]
        print(
            f"{candidate['rank']:>2}  {candidate['fixture_id']:<10}  {match:<28}  {candidate['execution_outcome']:<8}  "
            f"{candidate['candidate_type']:<14}  {candidate['ladder_bucket']:<9}  {safe_float(candidate.get('model_probability')):>7.2%}  "
            f"{safe_float(candidate.get('market_midpoint')):>7.2%}  {candidate['confidence_score']:>5}  {candidate['source_agreement']:>3}  "
            f"{safe_float(candidate['suggested_size']):>6.2f}  {safe_float(candidate['fok_worst_price']):>7.2%}  {candidate['reason']}"
        )
    print("-" * 168)


def write_candidate_reports(report):
    OUTPUT.mkdir(exist_ok=True)
    json_path = OUTPUT / "qualification_ladder_candidates.json"
    md_path = OUTPUT / "qualification_ladder_candidates.md"
    final_path = OUTPUT / "qualification_probability_ladder_report.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        "# Qualification Probability Ladder Candidates",
        "",
        f"Generated: {report['generated_at']}",
        f"Fixtures scanned: {report['fixtures_scanned']}",
        f"Candidate count: {report['candidate_count']}",
        f"Selected batch count: {len(report['selected_batch'])}",
        f"Total planned spend: ${report['total_planned_spend']:.2f}",
        "",
        "| Rank | Fixture | Match | Outcome | Type | Bucket | Model Prob | Market | Confidence | Sources | Size | Worst Price | Prior Rejected |",
        "|---:|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for candidate in report["selected_batch"]:
        lines.append(
            f"| {candidate['rank']} | {candidate['fixture_id']} | {candidate['match']} | {candidate['execution_outcome']} | "
            f"{candidate['candidate_type']} | {candidate['ladder_bucket']} | {candidate['model_probability']:.2%} | "
            f"{candidate['market_midpoint']:.2%} | {candidate['confidence_score']} | {candidate['source_agreement']} | "
            f"${candidate['suggested_size']:.2f} | {candidate['fok_worst_price']:.2%} | {candidate['prior_rejected_count']} |"
        )
    if report["errors"]:
        lines.extend(["", "## Errors", ""])
        for error in report["errors"]:
            lines.append(f"- {error['fixture_id']}: {error['error']}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    final_lines = [
        "# Qualification Probability Ladder Report",
        "",
        f"Generated: {report['generated_at']}",
        f"Candidate count: {report['candidate_count']}",
        f"Selected batch size: {len(report['selected_batch'])}",
        f"Total planned spend: ${report['total_planned_spend']:.2f}",
        f"Winner ladder count: {report['winner_ladder_count']}",
        f"Value slot count: {report['value_slot_count']}",
        "",
        "## Rejected-History Warning",
        "",
        report["rejected_history_warning"],
        "",
        "## Selected Batch",
        "",
        "| Rank | Fixture | Match | Outcome | Type | Size | Worst Price | Reason |",
        "|---:|---:|---|---|---|---:|---:|---|",
    ]
    for candidate in report["selected_batch"]:
        final_lines.append(
            f"| {candidate['rank']} | {candidate['fixture_id']} | {candidate['match']} | {candidate['execution_outcome']} | "
            f"{candidate['candidate_type']} | ${candidate['suggested_size']:.2f} | {candidate['fok_worst_price']:.2%} | {candidate['reason']} |"
        )
    final_lines.extend(
        [
            "",
            "## Live Execution Command",
            "",
            "```powershell",
            "python scripts\\qualification_ladder_runner.py --execute",
            "```",
            "",
            "Exact confirmation phrase:",
            "",
            f"```text\n{CONFIRMATION}\n```",
            "",
            "## Safety",
            "",
            report["safe_to_proceed"],
        ]
    )
    final_path.write_text("\n".join(final_lines) + "\n", encoding="utf-8")
    return str(json_path), str(md_path), str(final_path)


def scan_candidates(arena_key):
    orders = get_order_history(arena_key).get("orders") or []
    listings = fetch_polymarket_listings(arena_key)
    fixture_ids = [str(row.get("fixture_id")) for row in listings if row.get("fixture_id")]
    mappings = fetch_mappings_for_fixtures(fixture_ids)
    priors_cache = {}
    winner_candidates = []
    value_candidates = []
    errors = []
    skipped = []
    for index, mapping in enumerate(mappings, start=1):
        fixture_id = str(mapping["sportmonks_fixture_id"])
        try:
            evaluated = retry_scan_request(
                f"fixture {fixture_id} candidate scan",
                lambda: evaluate_fixture(arena_key, mapping, priors_cache, orders),
            )
            if evaluated.get("skipped"):
                skipped.append(evaluated)
                continue
            if evaluated.get("winner_candidate"):
                winner_candidates.append(evaluated["winner_candidate"])
            if evaluated.get("value_candidate"):
                value_candidates.append(evaluated["value_candidate"])
        except Exception as exc:
            errors.append({"fixture_id": fixture_id, "match": mapping.get("sportmonks_match_name"), "error": str(exc)})
            print(f"Skipping {fixture_id}: {exc}", file=sys.stderr, flush=True)
        if index % 10 == 0:
            print(f"Scanned {index}/{len(mappings)} fixtures...", flush=True)

    selected = build_batch(winner_candidates, value_candidates)
    selected.extend(load_extra_fallback_candidates(selected))
    selected = selected[:TARGET_TRADES]
    for index, candidate in enumerate(selected, start=1):
        candidate["rank"] = index
    rejected_orders = [order for order in orders if is_rejected_order(order)]
    filled_orders = [order for order in orders if is_filled_order(order)]
    total_spend = round(sum(safe_float(candidate.get("suggested_size")) for candidate in selected), 2)
    selected_public = [public_candidate(candidate) for candidate in selected]
    report = {
        "generated_at": utc_now(),
        "strategy_mode": "qualification_probability_ladder",
        "fixtures_available": len(mappings),
        "fixtures_scanned": len(mappings) - len(errors),
        "skipped_count": len(skipped),
        "candidate_count": len(winner_candidates) + len(value_candidates),
        "winner_candidate_count": len(winner_candidates),
        "value_candidate_count": len(value_candidates),
        "selected_batch": selected_public,
        "winner_ladder_count": sum(1 for candidate in selected_public if candidate["candidate_type"] == "winner_ladder"),
        "value_slot_count": sum(1 for candidate in selected_public if candidate["candidate_type"] == "value_slot"),
        "total_planned_spend": total_spend,
        "qualification_progress": len(filled_orders),
        "qualification_target": TARGET_TRADES,
        "rejected_order_count": len(rejected_orders),
        "rejected_history_warning": (
            f"{len(rejected_orders)} rejected orders exist. They do not count toward qualification and old order IDs will not be reused."
            if rejected_orders else "No rejected orders returned by Arena."
        ),
        "safe_to_proceed": (
            "Scan-only completed without POST calls. Review candidates before live execution."
            if len(selected_public) >= TARGET_TRADES
            else "Qualification cannot safely complete yet because fewer than 10 valid ladder trades were selected."
        ),
        "execution_command": "python scripts\\qualification_ladder_runner.py --execute",
        "confirmation_phrase": CONFIRMATION,
        "winner_candidates": [public_candidate(candidate) for candidate in sorted(winner_candidates, key=winner_rank_key, reverse=True)],
        "value_candidates": [public_candidate(candidate) for candidate in sorted(value_candidates, key=value_rank_key, reverse=True)],
        "errors": errors,
        "skipped": skipped[:20],
        "safety": {"scan_only_posts": False, "orders_placed": False, "ledger_submitted": False},
    }
    paths = write_candidate_reports(report)
    return report, selected, paths


def execute_batch(arena_key, selected):
    orders = get_order_history(arena_key).get("orders") or []
    filled_fixture_ids = already_filled_fixture_ids(orders)
    skipped_filled = [
        candidate for candidate in selected
        if str(candidate.get("fixture_id")) in filled_fixture_ids
    ]
    submit_list = [
        candidate for candidate in selected
        if str(candidate.get("fixture_id")) not in filled_fixture_ids
    ]
    current_filled_count = len(filled_fixture_ids)
    remaining_to_submit = max(0, TARGET_TRADES - current_filled_count)
    print(f"Current filled qualification count: {current_filled_count}/{TARGET_TRADES}")
    if skipped_filled:
        print("Skipped already-filled fixtures:")
        for candidate in skipped_filled:
            print(f"- {candidate.get('fixture_id')} {candidate.get('execution_outcome')} {candidate.get('match')}")
    print(f"Remaining trades to submit: {min(len(submit_list), remaining_to_submit)}")
    if current_filled_count + len(submit_list) < TARGET_TRADES:
        print(
            f"Execution blocked: effective qualification count would be "
            f"{current_filled_count + len(submit_list)}/{TARGET_TRADES}."
        )
        return {
            "submitted": False,
            "reason": "insufficient unfilled selected fixtures after counting filled fixtures",
            "current_filled_count": current_filled_count,
            "selected_count": len(selected),
            "submit_count": len(submit_list),
            "skipped_filled": [public_candidate(candidate) for candidate in skipped_filled],
            "results": [],
        }
    if not submit_list or remaining_to_submit <= 0:
        return {
            "submitted": False,
            "reason": "no remaining unfilled selected fixtures",
            "current_filled_count": current_filled_count,
            "skipped_filled": [public_candidate(candidate) for candidate in skipped_filled],
            "results": [],
        }
    selected = submit_list[:remaining_to_submit]
    print_batch(selected)
    total = sum(safe_float(candidate["suggested_size"]) for candidate in selected)
    print(f"\nTotal max spend: ${total:.2f}")
    print("\nFOK payloads that would be submitted after confirmation:")
    for candidate in selected:
        print(json.dumps(build_fok_payload(candidate), sort_keys=True))
    confirmation = input(f"\nType {CONFIRMATION} to submit live FOK batch: ").strip()
    if confirmation != CONFIRMATION:
        print("Confirmation mismatch. Aborting without submitting orders.")
        return {"submitted": False, "reason": "confirmation mismatch", "results": []}

    results = []
    consecutive_rejects = 0
    priors_cache = {}
    for candidate in selected:
        original_fixture_id = candidate.get("fixture_id")
        try:
            candidate = refresh_selected_candidate(arena_key, candidate, priors_cache)
            if not candidate:
                results.append({"fixture_id": original_fixture_id, "status": "skipped", "reason": "candidate no longer passes latest live gates"})
                print(f"Skipping {original_fixture_id}: candidate no longer passes latest live gates.")
                continue
        except Exception as exc:
            print(f"Stopping on latest-market refresh error: {exc}", file=sys.stderr)
            results.append({"fixture_id": candidate.get("fixture_id"), "outcome": candidate.get("execution_outcome"), "error": str(exc)})
            break
        payload = build_fok_payload(candidate)
        print(f"\nSubmitting {candidate['fixture_id']} {candidate['execution_outcome']} ${candidate['suggested_size']:.2f} worst={candidate['fok_worst_price']}")
        try:
            response = post_json(
                f"{ARENA}{ARENA_V1}/arena/orders_fok",
                headers=arena_headers(arena_key),
                payload=payload,
                timeout=20,
            )
            order_id = response.get("order_id") or response.get("id") or (response.get("order") or {}).get("order_id")
            poll = poll_order_until_terminal(arena_key, order_id) if order_id else {"timed_out": False, "attempts": [], "final": response}
            final = poll.get("final") or response
            status = str(final.get("status") or response.get("status") or "").lower()
            summary = {
                "order_id": order_id,
                "fixture_id": candidate["fixture_id"],
                "outcome": candidate["execution_outcome"],
                "size": payload["usd_size"],
                "worst_price": payload["worst_price"],
                "final_status": status,
                "usd_size_filled": final.get("usd_size_filled"),
                "open_avg_fill_price": final.get("open_avg_fill_price"),
                "rejection_reason": final.get("rejection_reason") or final.get("failure_reason") or final.get("error"),
                "response": response,
                "poll": poll,
            }
            print(json.dumps({key: summary[key] for key in ("order_id", "fixture_id", "outcome", "size", "worst_price", "final_status", "usd_size_filled", "open_avg_fill_price", "rejection_reason")}, indent=2))
            results.append(summary)
            consecutive_rejects = consecutive_rejects + 1 if status == "rejected" else 0

            records = build_records(
                f"qualification_ladder:{candidate['fixture_id']}:{uuid.uuid4()}",
                candidate.get("data") or {},
                candidate.get("prediction") or {},
                candidate.get("strategy") or {},
                {"provider": "rules", "model_name": os.getenv("MODEL_VARIANT", "research_602020")},
            )
            ledger = maybe_submit_ledger(arena_key, records)
            summary["ledger"] = ledger
            if consecutive_rejects >= 3:
                print("Stopping after 3 consecutive FOK rejections.")
                break
        except Exception as exc:
            print(f"Stopping on wallet/API/order error: {exc}", file=sys.stderr)
            results.append({"fixture_id": candidate["fixture_id"], "outcome": candidate["execution_outcome"], "error": str(exc)})
            break
    return {"submitted": True, "results": results}


def execute_one(arena_key, selected, fixture_id):
    orders = get_order_history(arena_key).get("orders") or []
    filled_fixture_ids = already_filled_fixture_ids(orders)
    current_filled_count = len(filled_fixture_ids)
    print(f"Current filled qualification count: {current_filled_count}/{TARGET_TRADES}")
    if str(fixture_id) in filled_fixture_ids:
        print(f"Fixture {fixture_id} already has a positive filled/completed order.")
        print("ALREADY_FILLED_ABORT")
        return {
            "submitted": False,
            "reason": "already filled",
            "fixture_id": str(fixture_id),
            "current_filled_count": current_filled_count,
        }
    matches = [candidate for candidate in selected if str(candidate.get("fixture_id")) == str(fixture_id)]
    if not matches:
        print(f"Fixture {fixture_id} is not in the selected qualification ladder batch.")
        return {"submitted": False, "reason": "fixture not selected", "fixture_id": str(fixture_id)}
    candidate = refresh_selected_candidate(arena_key, matches[0], {})
    if not candidate:
        print(f"Fixture {fixture_id} no longer passes latest live gates.")
        return {"submitted": False, "reason": "candidate no longer passes latest live gates", "fixture_id": str(fixture_id)}
    payload = build_fok_payload(candidate)
    print("SINGLE ORDER LIVE FOK")
    print(f"fixture_id={candidate['fixture_id']}")
    print(f"match={candidate.get('match')}")
    print(f"team_code={payload['team_code']}")
    print(f"stake={payload['usd_size']}")
    print(f"worst_price={payload['worst_price']}")
    print(f"strategy_type={candidate.get('candidate_type')}:{candidate.get('ladder_bucket')}")
    response = post_json(
        f"{ARENA}{ARENA_V1}/arena/orders_fok",
        headers=arena_headers(arena_key),
        payload=payload,
        timeout=20,
    )
    order_id = response.get("order_id") or response.get("id") or (response.get("order") or {}).get("order_id")
    poll = poll_order_until_terminal(arena_key, order_id) if order_id else {"timed_out": False, "attempts": [], "final": response}
    final = poll.get("final") or response
    status = str(final.get("status") or response.get("status") or "").lower()
    summary = {
        "order_id": order_id,
        "status": status,
        "usd_size_filled": final.get("usd_size_filled"),
        "team_code": payload["team_code"],
        "fixture_id": payload["fixture_id"],
        "payload": payload,
        "response": response,
        "poll": poll,
    }
    print(f"order_id={summary['order_id']}")
    print(f"status={summary['status']}")
    print(f"usd_size_filled={summary['usd_size_filled']}")
    print(f"team_code={summary['team_code']}")
    print(f"fixture_id={summary['fixture_id']}")
    if status in {"completed", "filled", "settled", "closed"}:
        print("SINGLE_ORDER_SUCCESS")
    elif status in {"rejected", "unfilled"}:
        print("SINGLE_ORDER_FAILED")
    return {"submitted": True, "result": summary}


def parse_args():
    parser = argparse.ArgumentParser(description="Qualification probability ladder scanner/executor.")
    parser.add_argument("--scan-only", action="store_true", help="Scan and report only. This is the default.")
    parser.add_argument("--execute", action="store_true", help="Execute the selected live FOK batch after exact typed confirmation.")
    parser.add_argument("--execute-one", metavar="FIXTURE_ID", help="Execute exactly one selected fixture as a live FOK order.")
    return parser.parse_args()


def main():
    load_dotenv()
    args = parse_args()
    arena_key = os.getenv("ARENA_KEY")
    if not arena_key:
        print("ARENA_KEY is missing.", file=sys.stderr)
        return 2
    os.environ["ORDER_EXECUTION_MODE"] = "fok"
    report, selected, paths = scan_candidates(arena_key)
    print_batch(selected)
    print(f"\nWrote {paths[0]}")
    print(f"Wrote {paths[1]}")
    print(f"Wrote {paths[2]}")
    print(f"Selected batch: {len(selected)} | planned spend ${report['total_planned_spend']:.2f}")
    if len(selected) < TARGET_TRADES:
        print("Qualification cannot safely complete yet: fewer than 10 valid ladder trades selected.")
    if args.execute_one:
        result = execute_one(arena_key, selected, args.execute_one)
        OUTPUT.mkdir(exist_ok=True)
        (OUTPUT / "qualification_ladder_single_order_result.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        return 0
    if not args.execute:
        print("Scan-only mode. No orders placed. No ledger submitted.")
        return 0
    result = execute_batch(arena_key, selected)
    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT / "qualification_ladder_execution_result.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
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
