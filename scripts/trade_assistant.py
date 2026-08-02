"""Safe, read-mostly planner and manually confirmed FOK trade assistant."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import requests
from dotenv import load_dotenv

from arena_account_report import ArenaClient, FixtureResolver, safe_float
from discretionary_fok_order import (
    build_payload,
    extract_order_id,
    order_is_filled,
    poll_order,
    post_json,
    summarize_order,
)
from agent_prematch import (
    blended_prediction,
    fetch_country_ids,
    fetch_fixture_detail,
    fetch_supabase_priors,
)
from qualification_ladder_runner import (
    fetch_market_digest,
    fetch_mappings_for_fixtures,
    fok_worst_price,
    matchup_cache_key,
    winner_bucket,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
ENV_PATH = ROOT / ".env"
API_PREFIX = "/api/v1"
DEFAULT_CONFIRMATION = "EXECUTE POLYSTAIR"
FILLED_STATUSES = {"completed", "filled", "settled", "closed"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def money(value: Any) -> str:
    number = safe_float(value, None)
    return "N/A" if number is None else f"${number:,.2f}"


def as_list(payload: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return [row for row in payload[key] if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def read_json(path: Path, warnings: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        warnings.append(f"Could not read {path.relative_to(ROOT)}: {exc}")
        return None


def normalize_epoch(value: Any) -> float | None:
    number = safe_float(value, None)
    if number is None:
        return None
    return number / 1000.0 if number > 10_000_000_000 else number


def kickoff_epoch(row: dict[str, Any]) -> float | None:
    for key in ("pre_match_lock_utc", "kickoff_utc", "kickoff_at", "start_at", "starting_at"):
        value = row.get(key)
        epoch = normalize_epoch(value)
        if epoch is not None:
            return epoch
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except ValueError:
                pass
    return None


def live_match_index(payload: Any) -> dict[str, dict[str, Any]]:
    rows = as_list(payload, "matches")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        fixture_id = row.get("fixture_id") or row.get("id")
        if fixture_id is not None:
            result[str(fixture_id)] = row
    return result


def is_current_prematch(row: dict[str, Any]) -> tuple[bool, str]:
    window = str(row.get("current_window") or "")
    if window != "PRE_MATCH":
        return False, f"current_window is {window or 'null'}"
    status = str(row.get("status") or "").lower()
    if status in {"final", "finished", "settled", "cancelled", "canceled", "live", "in_play"}:
        return False, f"match status is {status}"
    lock = kickoff_epoch(row)
    server = normalize_epoch(row.get("server_ts_utc")) or datetime.now(timezone.utc).timestamp()
    if lock is not None and server >= lock:
        return False, "prematch lock/kickoff has elapsed"
    return True, "PRE_MATCH"


def positive_filled(order: dict[str, Any]) -> bool:
    return (
        str(order.get("status") or "").lower() in FILLED_STATUSES
        and (safe_float(order.get("usd_size_filled"), 0.0) or 0.0) > 0
    )


def duplicate_sets(orders: list[dict[str, Any]], positions: list[dict[str, Any]]) -> tuple[set[str], set[tuple[str, str]]]:
    fixtures: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for order in orders:
        if not positive_filled(order):
            continue
        fixture = str(order.get("fixture_id") or "")
        team = str(order.get("team_code") or "").lower()
        fixtures.add(fixture)
        pairs.add((fixture, team))
    for position in positions:
        if (safe_float(position.get("value_usdc"), 0.0) or 0.0) <= 0:
            continue
        fixture = str(position.get("fixture_id") or "")
        team = str(position.get("team_code") or "").lower()
        pairs.add((fixture, team))
    return fixtures, pairs


def settled_fixture_ids(orders: list[dict[str, Any]]) -> set[str]:
    return {
        str(order.get("fixture_id"))
        for order in orders
        if order.get("fixture_id") and str(order.get("status") or "").lower() in {"settled", "closed"}
    }


def recent_closed_rejections(orders: list[dict[str, Any]]) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    now = datetime.now(timezone.utc).timestamp()
    for order in orders:
        if str(order.get("status") or "").lower() not in {"rejected", "unfilled"}:
            continue
        reason = str(order.get("rejection_reason") or order.get("failure_reason") or order.get("reason") or "").lower()
        if not any(term in reason for term in ("closed", "window", "kickoff", "lock")):
            continue
        raw_time = order.get("updated_at") or order.get("created_at") or order.get("submitted_at")
        event_time = normalize_epoch(raw_time)
        if event_time is None and isinstance(raw_time, str):
            try:
                event_time = datetime.fromisoformat(raw_time.replace("Z", "+00:00")).timestamp()
            except ValueError:
                event_time = None
        if event_time is None or now - event_time <= 86400:
            result.add((str(order.get("fixture_id") or ""), str(order.get("team_code") or "").lower()))
    return result


def mapping_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("sportmonks_fixture_id")): row
        for row in rows
        if row.get("sportmonks_fixture_id") is not None
    }


def resolved_match(fixture_id: str, resolver: FixtureResolver, mappings: dict[str, dict[str, Any]]) -> str:
    mapping = mappings.get(fixture_id) or {}
    name = mapping.get("sportmonks_match_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    info = resolver.resolve(fixture_id)
    return info["match"] if info["match"] != fixture_id else "UNKNOWN"


def fetch_live_market_data(
    client: ArenaClient,
    matches: dict[str, dict[str, Any]],
    warnings: list[str],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    not_listed: list[str] = []
    for fixture_id, timing in matches.items():
        open_now, _reason = is_current_prematch(timing)
        if not open_now:
            continue
        try:
            payload = client.get(f"{API_PREFIX}/data/polymarket/markets/{fixture_id}")
            outcomes = payload.get("outcomes") if isinstance(payload, dict) else []
            prices = {
                str(row.get("name")): safe_float(row.get("mid_price"), None)
                for row in outcomes or []
                if isinstance(row, dict) and row.get("name") is not None
            }
            result[fixture_id] = {"prices": prices, "raw": payload}
        except RuntimeError as exc:
            if "HTTP 404" in str(exc) and "fixture_not_listed" in str(exc):
                not_listed.append(fixture_id)
            else:
                warnings.append(f"Live market unavailable for fixture {fixture_id}: {exc}")
    if not_listed:
        warnings.append(
            f"{len(not_listed)} PRE_MATCH fixtures have no Polymarket market mapping yet: {', '.join(not_listed)}"
        )
    return result


def refresh_scan(warnings: list[str]) -> dict[str, Any]:
    command = [sys.executable, str(ROOT / "scripts" / "qualification_ladder_runner.py"), "--scan-only"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=600, check=False)
    details = {"command": command, "returncode": result.returncode}
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-1000:].strip()
        warnings.append(f"Scan refresh failed with exit {result.returncode}: {tail}")
    return details


def retry_read(label: str, operation, warnings: list[str]) -> Any:
    delays = (2, 5, 10)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(delays[attempt])
    warnings.append(f"{label} failed after 3 attempts: {last_error}")
    return None


def direct_live_candidates(
    arena_key: str,
    mappings: list[dict[str, Any]],
    matches: dict[str, dict[str, Any]],
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Recompute model-winner candidates with the qualification runner's inputs and gates."""
    candidates: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    priors_cache: dict[tuple[str, str], dict[str, Any]] = {}
    model_variant = os.getenv("MODEL_VARIANT", "research_602020")
    for mapping in mappings:
        fixture_id = str(mapping.get("sportmonks_fixture_id") or "")
        timing = matches.get(fixture_id)
        open_now, window_reason = is_current_prematch(timing or {})
        if not open_now:
            continue

        def evaluate() -> dict[str, Any] | None:
            fixture, _home, _away = fetch_fixture_detail(arena_key, fixture_id)
            key = matchup_cache_key(mapping)
            if key not in priors_cache:
                countries = fetch_country_ids(mapping["home_country"], mapping["away_country"])
                country_ids = [row["country_id"] for row in countries.values()]
                priors_cache[key] = {"countries": countries, "priors": fetch_supabase_priors(country_ids)}
            market = fetch_market_digest(arena_key, fixture_id, mapping)
            prediction = blended_prediction(
                fixture,
                mapping,
                priors_cache[key]["priors"],
                priors_cache[key]["countries"],
                model_variant=model_variant,
            )
            model_winner = prediction.get("outcome")
            market_midpoint = safe_float((market.get("implied_win_prob") or {}).get(model_winner), None)
            model_probability = safe_float(prediction.get("probability"), None)
            if model_winner == "draw" or market_midpoint is None or model_probability is None:
                return None
            edge_pp = round((model_probability - market_midpoint) * 100, 2)
            confidence = int(prediction.get("confidence_score") or 0)
            sources = int(prediction.get("source_agreement") or 0)
            bucket = winner_bucket(model_probability, market_midpoint)
            strict = bool(bucket and confidence >= 65 and sources >= 2 and 0 < market_midpoint < 1)
            fallback = bool(
                0 < market_midpoint <= 0.80
                and confidence >= 63
                and sources >= 2
                and model_probability >= 0.42
                and edge_pp >= -18
            )
            if not strict and not fallback:
                return None
            bucket_name = bucket["name"] if strict else "fallback_42_50"
            price_quality = round(max(0.0, 100.0 - abs(edge_pp)), 2)
            return {
                "fixture_id": fixture_id,
                "match": mapping.get("sportmonks_match_name") or fixture.get("name"),
                "kickoff_utc": mapping.get("sportmonks_kickoff_utc") or fixture.get("starting_at"),
                "execution_outcome": model_winner,
                "model_winner": model_winner,
                "model_probability": round(model_probability, 4),
                "market_midpoint": round(market_midpoint, 4),
                "winner_edge_pp": edge_pp,
                "confidence_score": confidence,
                "confidence_level": prediction.get("confidence_level"),
                "source_agreement": sources,
                "component_disagreement": safe_float(prediction.get("component_disagreement"), None),
                "suggested_size": 1.0,
                "candidate_type": "winner_ladder",
                "ladder_bucket": bucket_name,
                "bucket_rank": bucket["rank"] if strict else 0,
                "fok_worst_price": fok_worst_price(market_midpoint, "winner_ladder"),
                "price_reasonableness_score": price_quality,
                "risk_notes": prediction.get("risk_factors") or [],
                "reason": (
                    f"Fresh direct model: {model_winner} model {model_probability:.2%}, market {market_midpoint:.2%}, "
                    f"edge {edge_pp:+.2f} pp, confidence {confidence}, source agreement {sources}; "
                    f"{'qualification ladder gate' if strict else 'live fallback gate'}."
                ),
                "candidate_source": "trade_assistant_live_engine",
                "model_variant": model_variant,
            }

        candidate = retry_read(f"Live candidate {fixture_id}", evaluate, warnings)
        if candidate:
            candidates.append(candidate)
        else:
            failures.append({"fixture_id": fixture_id, "match": mapping.get("sportmonks_match_name"), "reason": "no eligible model-winner candidate or data fetch failed"})
    candidates.sort(
        key=lambda row: (
            int(row.get("confidence_score") or 0),
            int(row.get("source_agreement") or 0),
            safe_float(row.get("winner_edge_pp"), -999.0),
            safe_float(row.get("price_reasonableness_score"), 0.0),
            -safe_float(row.get("component_disagreement"), 1.0),
        ),
        reverse=True,
    )
    return candidates, failures


def count_eligible_calculated(
    candidates: list[dict[str, Any]],
    matches: dict[str, dict[str, Any]],
    orders: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    allow_duplicates: bool,
) -> int:
    filled_fixtures, owned_pairs = duplicate_sets(orders, positions)
    settled = settled_fixture_ids(orders)
    recent_closed = recent_closed_rejections(orders)
    count = 0
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        fixture = str(candidate.get("fixture_id") or "")
        outcome = str(candidate.get("execution_outcome") or candidate.get("outcome") or "")
        key = (fixture, outcome.lower())
        if not fixture or not outcome or key in seen:
            continue
        open_now, _reason = is_current_prematch(matches.get(fixture) or {})
        if not open_now or fixture in settled or key in recent_closed:
            continue
        if not allow_duplicates and (fixture in filled_fixtures or key in owned_pairs):
            continue
        if safe_float(candidate.get("model_probability"), None) is None or safe_float(candidate.get("market_midpoint"), None) is None:
            continue
        seen.add(key)
        count += 1
    return count


def merge_candidates(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for row in group:
            key = (
                str(row.get("fixture_id") or ""),
                str(row.get("execution_outcome") or row.get("outcome") or "").lower(),
            )
            if not all(key) or key in seen:
                continue
            seen.add(key)
            merged.append(row)
    return merged


def local_prediction_candidates(matches: dict[str, dict[str, Any]], warnings: list[str]) -> list[dict[str, Any]]:
    """Recover model-backed rows from prematch bundles, but only for fixtures still live PRE_MATCH."""
    rows: list[dict[str, Any]] = []
    for path in sorted(OUTPUT.glob("prematch_*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        payload = read_json(path, warnings)
        if not isinstance(payload, dict):
            continue
        fixture_id = str(payload.get("fixture_id") or (payload.get("data", {}).get("discovery", {}) or {}).get("fixture_id") or "")
        open_now, _reason = is_current_prematch(matches.get(fixture_id) or {})
        if not open_now:
            continue
        prediction = payload.get("prediction") or {}
        data = payload.get("data") or {}
        outcome = prediction.get("outcome")
        model_probability = safe_float(prediction.get("probability"), None)
        market_midpoint = safe_float(((data.get("polymarket_digest") or {}).get("implied_win_prob") or {}).get(outcome), None)
        if not outcome or outcome == "draw" or model_probability is None or market_midpoint is None:
            continue
        edge_pp = round((model_probability - market_midpoint) * 100, 2)
        rows.append(
            {
                "fixture_id": fixture_id,
                "match": (data.get("sportmonks_digest") or {}).get("fixture"),
                "execution_outcome": outcome,
                "model_winner": outcome,
                "model_probability": round(model_probability, 4),
                "market_midpoint": round(market_midpoint, 4),
                "winner_edge_pp": edge_pp,
                "confidence_score": prediction.get("confidence_score"),
                "confidence_level": prediction.get("confidence_level"),
                "source_agreement": prediction.get("source_agreement"),
                "component_disagreement": prediction.get("component_disagreement"),
                "suggested_size": 1.0,
                "candidate_type": "winner_ladder",
                "ladder_bucket": "local_prematch_recovery",
                "bucket_rank": 0,
                "fok_worst_price": fok_worst_price(market_midpoint, "winner_ladder"),
                "price_reasonableness_score": round(max(0.0, 100.0 - abs(edge_pp)), 2),
                "reason": (
                    f"Current local prematch model: {outcome} model {model_probability:.2%}, market {market_midpoint:.2%}, "
                    f"edge {edge_pp:+.2f} pp, confidence {prediction.get('confidence_score')}, "
                    f"source agreement {prediction.get('source_agreement')}."
                ),
                "candidate_source": path.name,
            }
        )
    return rows


def local_draw_prices(live_markets: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    prices: dict[str, dict[str, Any]] = {}
    for fixture_id, market in (live_markets or {}).items():
        price = safe_float((market.get("prices") or {}).get("draw"), None)
        if price is not None:
            prices[fixture_id] = {"price": price, "source": "live Arena market endpoint"}
    paths = sorted(OUTPUT.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)

    def walk(value: Any, fixture: str | None = None, source: str = "") -> None:
        if isinstance(value, dict):
            current = value.get("fixture_id") or value.get("fixture_code") or fixture
            current = str(current) if current is not None else None
            implied = value.get("implied_win_prob")
            if current and isinstance(implied, dict) and safe_float(implied.get("draw"), None) is not None:
                prices.setdefault(current, {"price": float(implied["draw"]), "source": source})
            handles = value.get("execution_handles")
            if current and isinstance(handles, dict) and isinstance(handles.get("draw"), dict):
                price = safe_float(handles["draw"].get("current_mid_yes"), None)
                if price is not None:
                    prices.setdefault(current, {"price": price, "source": source})
            edges = value.get("all_outcome_edges")
            if current and isinstance(edges, list):
                for edge in edges:
                    if isinstance(edge, dict) and str(edge.get("outcome")).lower() == "draw":
                        price = safe_float(edge.get("market_midpoint"), None)
                        if price is not None:
                            prices.setdefault(current, {"price": price, "source": source})
            for child in value.values():
                walk(child, current, source)
        elif isinstance(value, list):
            for child in value:
                walk(child, fixture, source)

    for path in paths:
        try:
            walk(json.loads(path.read_text(encoding="utf-8-sig")), source=path.name)
        except (OSError, ValueError):
            continue
    return prices


def exclusion(reason: str, fixture_id: Any = None, outcome: Any = None, source: str = "candidate") -> dict[str, Any]:
    return {
        "fixture_id": str(fixture_id or ""),
        "outcome": outcome,
        "source": source,
        "reason": reason,
    }


def build_plan(
    args: argparse.Namespace,
    candidates: list[dict[str, Any]],
    matches: dict[str, dict[str, Any]],
    orders: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    resolver: FixtureResolver,
    mappings: dict[str, dict[str, Any]],
    live_markets: dict[str, dict[str, Any]],
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    excluded: list[dict[str, Any]] = []
    plan: list[dict[str, Any]] = []
    filled_fixtures, owned_pairs = duplicate_sets(orders, positions)
    settled_fixtures = settled_fixture_ids(orders)
    recent_closed = recent_closed_rejections(orders)
    normal_target = max(0, args.plan_size - args.draws)
    normal_stake = min(args.normal_stake, 1.0)
    draw_stake = min(args.draw_stake, 1.0)

    valid_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        fixture = str(candidate.get("fixture_id") or "")
        outcome = candidate.get("execution_outcome") or candidate.get("outcome")
        if not fixture or not outcome:
            excluded.append(exclusion("missing fixture_id or outcome", fixture, outcome))
            continue
        timing = matches.get(fixture)
        if timing is None:
            excluded.append(exclusion("fixture absent from live matches response", fixture, outcome))
            continue
        open_now, reason = is_current_prematch(timing)
        if not open_now:
            excluded.append(exclusion(reason, fixture, outcome))
            continue
        pair = (fixture, str(outcome).lower())
        if fixture in settled_fixtures:
            excluded.append(exclusion("fixture already has settled/closed order history", fixture, outcome))
            continue
        if pair in recent_closed:
            excluded.append(exclusion("recent rejection indicates closed trading window", fixture, outcome))
            continue
        if not args.allow_duplicates and fixture in filled_fixtures:
            excluded.append(exclusion("fixture has positive filled/completed/settled history", fixture, outcome))
            continue
        if not args.allow_duplicates and pair in owned_pairs:
            excluded.append(exclusion("same fixture/outcome is already held", fixture, outcome))
            continue
        model_probability = safe_float(candidate.get("model_probability"), None)
        market = safe_float(candidate.get("market_midpoint"), None)
        if model_probability is None or market is None:
            excluded.append(exclusion("model probability or market midpoint is missing", fixture, outcome))
            continue
        worst = safe_float(candidate.get("fok_worst_price") or candidate.get("worst_price"), None)
        if worst is None:
            worst = round(min(0.99, market + 0.015), 4)
            candidate = {**candidate, "price_note": "derived from candidate midpoint + 1.5 pp"}
        if not 0 < worst < 1:
            excluded.append(exclusion("invalid worst price", fixture, outcome))
            continue
        valid_candidates.append({**candidate, "_worst": worst, "_outcome": str(outcome)})

    valid_candidates.sort(
        key=lambda row: (
            safe_float(row.get("confidence_score"), 0.0) or 0.0,
            safe_float(row.get("source_agreement"), 0.0) or 0.0,
            safe_float(
                row.get("winner_edge_pp") if row.get("winner_edge_pp") is not None else row.get("edge_pp"),
                -999.0,
            ),
            safe_float(row.get("model_probability"), 0.0) or 0.0,
            -safe_float(row.get("component_disagreement"), 1.0),
        ),
        reverse=True,
    )
    selected_fixtures: set[str] = set()
    for candidate in valid_candidates[:normal_target]:
        fixture = str(candidate["fixture_id"])
        edge = safe_float(candidate.get("winner_edge_pp") or candidate.get("edge_pp"), None)
        plan.append(
            {
                "type": "CALCULATED",
                "fixture_id": fixture,
                "match": candidate.get("match") or resolved_match(fixture, resolver, mappings),
                "pick": candidate["_outcome"],
                "stake_usdc": normal_stake,
                "worst_price": candidate["_worst"],
                "market_midpoint": candidate.get("market_midpoint"),
                "model_probability": candidate.get("model_probability"),
                "confidence_score": candidate.get("confidence_score"),
                "source_agreement": candidate.get("source_agreement"),
                "edge_pp": edge,
                "reason": candidate.get("reason") or (
                    f"confidence {candidate.get('confidence_score')}, model {candidate.get('model_probability')}, "
                    f"market {candidate.get('market_midpoint')}"
                ),
                "duplicate": fixture in filled_fixtures or (fixture, candidate["_outcome"].lower()) in owned_pairs,
                "window": "PRE_MATCH",
                "price_source": candidate.get("candidate_source") or "qualification_ladder_candidates.json",
            }
        )
        selected_fixtures.add(fixture)

    prices = local_draw_prices(live_markets)
    draw_pool: list[dict[str, Any]] = []
    for fixture, timing in matches.items():
        if fixture in selected_fixtures:
            continue
        open_now, reason = is_current_prematch(timing)
        if not open_now:
            continue
        pair = (fixture, "draw")
        if fixture in settled_fixtures:
            excluded.append(exclusion("draw fixture already settled/closed", fixture, "draw", "draw_pool"))
            continue
        if pair in recent_closed:
            excluded.append(exclusion("recent draw rejection indicates closed trading window", fixture, "draw", "draw_pool"))
            continue
        if not args.allow_duplicates and (fixture in filled_fixtures or pair in owned_pairs):
            excluded.append(exclusion("draw fixture already filled or held", fixture, "draw", "draw_pool"))
            continue
        price_info = prices.get(fixture)
        price = safe_float((price_info or {}).get("price"), None)
        if price is not None and not 0 < price <= 0.35:
            excluded.append(exclusion(f"draw price {price:.3f} is above 0.35 or invalid", fixture, "draw", "draw_pool"))
            continue
        kickoff = kickoff_epoch(timing)
        draw_pool.append(
            {
                "fixture_id": fixture,
                "price": price,
                "source": (price_info or {}).get("source"),
                "kickoff": kickoff if kickoff is not None else float("inf"),
                "timing": timing,
            }
        )
    draw_pool.sort(key=lambda row: (row["price"] is None, row["kickoff"], row["price"] or 1.0))
    for row in draw_pool[: args.draws]:
        fixture = row["fixture_id"]
        price = row["price"]
        unknown = price is None
        worst = 0.25 if unknown else round(min(0.99, price + 0.01), 4)
        plan.append(
            {
                "type": "DRAW",
                "fixture_id": fixture,
                "match": resolved_match(fixture, resolver, mappings),
                "pick": "draw",
                "stake_usdc": draw_stake,
                "worst_price": worst,
                "market_midpoint": price,
                "model_probability": None,
                "confidence_score": None,
                "source_agreement": None,
                "edge_pp": None,
                "reason": (
                    f"draw punt, open PRE_MATCH, cached draw price {price:.3f}"
                    if not unknown
                    else "draw punt, open PRE_MATCH, price unknown; conservative default cap 0.25"
                ),
                "duplicate": fixture in filled_fixtures or (fixture, "draw") in owned_pairs,
                "window": "PRE_MATCH",
                "price_source": row["source"] or "conservative_default",
            }
        )
        selected_fixtures.add(fixture)
    return plan, excluded


def enforce_budget(plan: list[dict[str, Any]], available: float, max_spend: float, warnings: list[str]) -> list[dict[str, Any]]:
    budget = max(0.0, min(available, max_spend))
    kept: list[dict[str, Any]] = []
    spent = 0.0
    for row in plan:
        stake = safe_float(row.get("stake_usdc"), 0.0) or 0.0
        if spent + stake > budget + 1e-9:
            warnings.append(f"Excluded {row['fixture_id']} {row['pick']}: plan would exceed available/max-spend budget")
            continue
        kept.append(row)
        spent += stake
    return kept


def add_safety_warnings(
    args: argparse.Namespace,
    plan: list[dict[str, Any]],
    available: float,
    locked: float,
    candidate_path: Path,
    warnings: list[str],
) -> None:
    spend = sum(safe_float(row.get("stake_usdc"), 0.0) or 0.0 for row in plan)
    if available < spend:
        warnings.append("Wallet available balance is below planned spend")
    if locked > 0:
        warnings.append(f"Locked balance is nonzero: {money(locked)}")
    if len(plan) < args.plan_size:
        warnings.append(f"Plan has {len(plan)} bets; requested {args.plan_size}")
    if candidate_path.exists():
        age_minutes = (time.time() - candidate_path.stat().st_mtime) / 60
        if age_minutes > 30:
            warnings.append(f"Candidate file is stale: {age_minutes:.0f} minutes old")
    for row in plan:
        if row["match"] == "UNKNOWN":
            warnings.append(f"Fixture {row['fixture_id']} has no resolved match name")
        if row["type"] == "DRAW" and row.get("market_midpoint") is None:
            warnings.append(f"Draw price is unknown for fixture {row['fixture_id']}; cap defaults to 0.25")
        if (safe_float(row.get("worst_price"), 0.0) or 0.0) > 0.85:
            warnings.append(f"Worst price exceeds 0.85 for fixture {row['fixture_id']}")
        if row["type"] == "CALCULATED" and safe_float(row.get("edge_pp"), 0.0) < 0:
            warnings.append(f"Calculated bet {row['fixture_id']} has negative edge {row['edge_pp']} pp")
        if row["type"] == "DRAW" and safe_float(row.get("market_midpoint"), 0.0) > 0.35:
            warnings.append(f"Draw punt {row['fixture_id']} is above 0.35")
    fixtures = [row["fixture_id"] for row in plan]
    if len(fixtures) != len(set(fixtures)):
        warnings.append("Plan contains duplicate fixtures")


def reasoning_check() -> dict[str, Any]:
    command = [sys.executable, str(ROOT / "scripts" / "reasoning_score_self_check.py"), "--latest"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    first = output.splitlines()[0].strip() if output else "FAIL"
    return {"status": first if first in {"PASS", "WARN", "FAIL"} else "FAIL", "returncode": result.returncode, "output": output}


def execute_plan(
    plan: list[dict[str, Any]],
    client: ArenaClient,
    arena_key: str,
    base_url: str,
    allow_duplicates: bool,
    dry_run: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for row in plan:
        try:
            current_orders = as_list(client.get(f"{API_PREFIX}/arena/orders"), "orders")
            duplicates = [
                order for order in current_orders
                if str(order.get("fixture_id")) == row["fixture_id"]
                and str(order.get("team_code") or "").lower() == row["pick"].lower()
                and order_is_filled(order)
            ]
            if duplicates and not allow_duplicates:
                results.append({**row, "final_status": "duplicate_abort", "order_id": None, "error": "positive filled duplicate found before POST"})
                continue
            args = SimpleNamespace(
                fixture=row["fixture_id"], team=row["pick"], usd=row["stake_usdc"], worst_price=row["worst_price"]
            )
            payload = build_payload(args)
            if dry_run:
                results.append({**row, "payload": payload, "final_status": "dry_run", "order_id": None})
                continue
            response = post_json(
                f"{base_url}{API_PREFIX}/arena/orders_fok",
                headers={"x-api-key": arena_key},
                payload=payload,
                timeout=20,
            )
            order_id = extract_order_id(response)
            final = poll_order(base_url, arena_key, order_id) if order_id else response
            summary = summarize_order(final, fallback={**response, **payload, "order_id": order_id})
            time.sleep(2)
            verify_rows = as_list(client.get(f"{API_PREFIX}/arena/orders"), "orders")
            verified = next((order for order in verify_rows if str(order.get("order_id") or order.get("id")) == str(order_id)), None)
            results.append(
                {
                    **row,
                    "payload": payload,
                    "order_id": order_id,
                    "final_status": summary.get("status") or "unknown",
                    "usd_size_filled": summary.get("usd_size_filled"),
                    "open_avg_fill_price": summary.get("open_avg_fill_price"),
                    "rejection_reason": summary.get("rejection_reason"),
                    "verified_in_orders": verified is not None,
                    "verified_order": verified,
                }
            )
        except Exception as exc:
            results.append({**row, "final_status": "error", "order_id": None, "error": str(exc)})
    return results


def table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "(none)"
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], min(34, len(cell)))
    def render(row: list[str]) -> str:
        values = [cell if len(cell) <= 34 else cell[:31] + "..." for cell in row]
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(values)).rstrip()
    return "\n".join([render(headers), render(["-" * width for width in widths]), *(render(row) for row in rows)])


def render(report: dict[str, Any], compact: bool = False) -> str:
    summary = report["summary"]
    if compact:
        return (
            f"Cash {money(summary['wallet_available'])} | Open {money(summary['open_exposure_value'])} | "
            f"Plan {summary['plan_count']} bets | Spend {money(summary['planned_spend'])} | "
            f"Calculated {summary['calculated_count']} | Draws {summary['draw_count']} | Execute {str(summary['execute']).lower()}"
        )
    lines = [
        "POLYSTAIR TRADE ASSISTANT",
        f"Generated: {report['generated_at']}",
        f"Mode: {'DRY EXECUTION BUILD' if summary['dry_run'] else 'EXECUTE REQUESTED' if summary['execute'] else 'READ-ONLY PLAN'}",
        "",
        table(
            ["Type", "Fixture", "Match", "Pick", "Model", "Market", "Edge", "Conf", "Src", "Stake", "Worst", "Reason", "Dup?", "Window"],
            [[
                row["type"], row["fixture_id"], row["match"], row["pick"],
                f"{row['model_probability']:.1%}" if row.get("model_probability") is not None else "N/A",
                f"{row['market_midpoint']:.1%}" if row.get("market_midpoint") is not None else "N/A",
                f"{row['edge_pp']:+.2f}pp" if row.get("edge_pp") is not None else "N/A",
                str(row.get("confidence_score") if row.get("confidence_score") is not None else "N/A"),
                str(row.get("source_agreement") if row.get("source_agreement") is not None else "N/A"),
                money(row["stake_usdc"]), f"{row['worst_price']:.3f}", row["reason"],
                "YES" if row["duplicate"] else "no", row["window"],
            ] for row in report["plan"]],
        ),
        "",
        f"Total planned spend: {money(summary['planned_spend'])}",
        f"Wallet available: {money(summary['wallet_available'])}",
        f"Remaining cash after plan: {money(summary['remaining_cash'])}",
        f"Open exposure value: {money(summary['open_exposure_value'])}",
        f"Excluded candidates/items: {summary['excluded_count']}",
    ]
    if report["warnings"]:
        lines.extend(["", "WARNINGS", *(f"- {warning}" for warning in report["warnings"])])
    if report.get("execution"):
        lines.extend(["", "EXECUTION"])
        for row in report["execution"]:
            lines.append(
                f"- {row['fixture_id']} {row['pick']}: {row.get('final_status')} | "
                f"order={row.get('order_id')} | filled={row.get('usd_size_filled')} | avg={row.get('open_avg_fill_price')}"
            )
    return "\n".join(lines)


def save_report(report: dict[str, Any], executed: bool) -> list[Path]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    paths = [
        OUTPUT / "trade_assistant_plan_latest.json",
        OUTPUT / "trade_assistant_plan_latest.md",
        OUTPUT / f"trade_assistant_plan_{stamp}.json",
        OUTPUT / f"trade_assistant_plan_{stamp}.md",
    ]
    if executed:
        paths.extend(
            [
                OUTPUT / "trade_assistant_execution_latest.json",
                OUTPUT / f"trade_assistant_execution_{stamp}.json",
            ]
        )
    payload = json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    for path in paths:
        path.write_text(("```text\n" + render(report) + "\n```\n") if path.suffix == ".md" else payload, encoding="utf-8")
    return paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan and manually confirm a safe five-bet FOK batch")
    parser.add_argument("--plan-size", type=int, default=5)
    parser.add_argument("--draws", type=int, default=2)
    parser.add_argument("--max-spend", type=float, default=5.0)
    parser.add_argument("--stake", type=float, default=1.0, help="Default stake applied unless normal/draw stake is supplied")
    parser.add_argument("--normal-stake", type=float)
    parser.add_argument("--draw-stake", type=float)
    parser.add_argument("--show-rejected", action="store_true")
    parser.add_argument("--allow-duplicates", action="store_true")
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Never POST; with --execute, build FOK payloads after confirmation")
    parser.add_argument("--skip-reasoning-check", action="store_true")
    parser.add_argument("--refresh-scan", action="store_true")
    parser.add_argument("--confirm-text", default=DEFAULT_CONFIRMATION)
    parser.add_argument("--force", action="store_true", help="Allow execution confirmation after a failed reasoning self-check")
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.plan_size < 0 or args.draws < 0 or args.draws > args.plan_size:
        raise ValueError("--plan-size must be nonnegative and --draws must be between 0 and plan-size")
    for name in ("stake", "max_spend"):
        if getattr(args, name) <= 0:
            raise ValueError(f"--{name.replace('_', '-')} must be positive")
    args.normal_stake = args.stake if args.normal_stake is None else args.normal_stake
    args.draw_stake = args.stake if args.draw_stake is None else args.draw_stake
    if args.normal_stake <= 0 or args.draw_stake <= 0:
        raise ValueError("stake values must be positive")
    if args.normal_stake > 1 or args.draw_stake > 1:
        raise ValueError("maximum single bet is 1 USDC")


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not ENV_PATH.exists():
        print(f"ERROR: missing {ENV_PATH}", file=sys.stderr)
        return 2
    load_dotenv(ENV_PATH, override=False)
    arena_key = os.getenv("ARENA_KEY", "").strip()
    base_url = os.getenv("ARENA_BASE_URL", "").strip().rstrip("/")
    if not arena_key or not base_url:
        print("ERROR: ARENA_KEY and ARENA_BASE_URL are required", file=sys.stderr)
        return 2
    warnings: list[str] = []
    endpoint_errors: list[str] = []
    refresh = None
    live_engine: dict[str, Any] = {"ran": False, "candidate_count": 0, "failures": []}
    candidate_path = OUTPUT / "qualification_ladder_candidates.json"
    candidate_data = read_json(candidate_path, warnings) or {}
    candidates = candidate_data.get("selected_batch") if isinstance(candidate_data, dict) else []
    candidates = candidates if isinstance(candidates, list) else []
    client = ArenaClient(base_url, arena_key)

    def fetch(path: str, fallback: Any) -> Any:
        try:
            return client.get(path)
        except RuntimeError as exc:
            endpoint_errors.append(str(exc))
            warnings.append(str(exc))
            return fallback

    wallet_response = fetch(f"{API_PREFIX}/arena/agents/me", {})
    orders_response = fetch(f"{API_PREFIX}/arena/orders", {"orders": []})
    exposure_response = fetch(f"{API_PREFIX}/arena/exposure", {"positions": []})
    matches_response = fetch(f"{API_PREFIX}/arena/matches", {"matches": []})
    wallet = wallet_response.get("wallet") if isinstance(wallet_response, dict) else {}
    wallet = wallet if isinstance(wallet, dict) else {}
    orders = as_list(orders_response, "orders")
    positions = as_list(exposure_response, "positions")
    matches = live_match_index(matches_response)
    if not matches:
        warnings.append("No live match windows available; planning fails closed")
    live_fixture_ids = [fixture_id for fixture_id, row in matches.items() if is_current_prematch(row)[0]]
    mappings: list[dict[str, Any]] = []
    if live_fixture_ids:
        try:
            mappings = fetch_mappings_for_fixtures(live_fixture_ids)
        except Exception as exc:
            warnings.append(f"Live fixture mapping lookup failed: {exc}")
    mappings_by_fixture = mapping_index(mappings)
    normal_target = max(0, args.plan_size - args.draws)
    initial_eligible = count_eligible_calculated(
        candidates, matches, orders, positions, args.allow_duplicates
    )
    if args.refresh_scan or initial_eligible < normal_target:
        refresh = refresh_scan(warnings)
        refreshed_data = read_json(candidate_path, warnings) or {}
        refreshed = refreshed_data.get("selected_batch") if isinstance(refreshed_data, dict) else []
        if isinstance(refreshed, list):
            candidates = refreshed
    local_recovery = local_prediction_candidates(matches, warnings)
    candidates = merge_candidates(candidates, local_recovery)
    refreshed_eligible = count_eligible_calculated(
        candidates, matches, orders, positions, args.allow_duplicates
    )
    if refreshed_eligible < normal_target and mappings:
        direct_candidates, direct_failures = direct_live_candidates(arena_key, mappings, matches, warnings)
        live_engine = {
            "ran": True,
            "candidate_count": len(direct_candidates),
            "failures": direct_failures,
            "mapped_live_fixtures": len(mappings),
        }
        candidates = merge_candidates(candidates, direct_candidates)
    elif refreshed_eligible < normal_target:
        warnings.append("Direct live candidate engine could not run because live fixture mappings are unavailable")
    live_markets = fetch_live_market_data(client, matches, warnings) if matches and args.draws else {}
    resolver = FixtureResolver(OUTPUT, warnings)
    plan, excluded = build_plan(
        args,
        candidates,
        matches,
        orders,
        positions,
        resolver,
        mappings_by_fixture,
        live_markets,
        warnings,
    )
    available = safe_float(wallet.get("available_balance_usdc"), 0.0) or 0.0
    locked = safe_float(wallet.get("locked_balance_usdc"), 0.0) or 0.0
    plan = enforce_budget(plan, available, args.max_spend, warnings)
    add_safety_warnings(args, plan, available, locked, candidate_path, warnings)
    if args.show_rejected:
        rejected = [order for order in orders if str(order.get("status") or "").lower() in {"rejected", "unfilled"}]
    else:
        rejected = []
    spend = sum(safe_float(row.get("stake_usdc"), 0.0) or 0.0 for row in plan)
    report: dict[str, Any] = {
        "generated_at": utc_now(),
        "wallet": wallet,
        "open_positions": positions,
        "plan": plan,
        "excluded": excluded,
        "warnings": list(dict.fromkeys(warnings)),
        "endpoint_errors": endpoint_errors,
        "rejected_orders": rejected,
        "refresh_scan": refresh,
        "live_candidate_engine": live_engine,
        "local_prediction_recovery_count": len(local_recovery),
        "reasoning_check": None,
        "execution": [],
        "summary": {
            "wallet_available": available,
            "wallet_locked": locked,
            "open_exposure_value": sum(safe_float(row.get("value_usdc"), 0.0) or 0.0 for row in positions),
            "plan_count": len(plan),
            "calculated_count": sum(row["type"] == "CALCULATED" for row in plan),
            "draw_count": sum(row["type"] == "DRAW" for row in plan),
            "planned_spend": round(spend, 2),
            "remaining_cash": round(available - spend, 2),
            "excluded_count": len(excluded),
            "execute": args.execute,
            "dry_run": args.dry_run,
        },
    }
    if not args.json:
        print(render(report, args.compact))

    if args.execute:
        if not plan:
            report["warnings"].append("Execution blocked: plan is empty")
            print("EXECUTION BLOCKED: plan is empty", file=sys.stderr)
        elif endpoint_errors:
            report["warnings"].append("Execution blocked: one or more required live endpoints failed")
            print("EXECUTION BLOCKED: live endpoint failure", file=sys.stderr)
        else:
            check = {"status": "SKIPPED", "output": "Skipped by --skip-reasoning-check"}
            if not args.skip_reasoning_check:
                check = reasoning_check()
            report["reasoning_check"] = check
            if check["status"] == "FAIL" and not args.force:
                report["warnings"].append("Execution blocked: reasoning self-check failed; use --force only after review")
                print(check["output"], file=sys.stderr)
                print("EXECUTION BLOCKED: reasoning self-check failed", file=sys.stderr)
            else:
                print("\nFULL FOK PLAN")
                for row in plan:
                    print(json.dumps({
                        "fixture_id": row["fixture_id"], "team_code": row["pick"],
                        "usd_size": f"{row['stake_usdc']:.2f}", "worst_price": row["worst_price"], "reason": row["reason"],
                    }, sort_keys=True))
                print(f"Type exactly: {args.confirm_text}")
                typed = input("> ")
                if typed != args.confirm_text:
                    report["warnings"].append("Execution aborted: confirmation text did not match")
                    print("CONFIRMATION_FAILED_ABORT")
                else:
                    report["execution"] = execute_plan(
                        plan, client, arena_key, base_url, args.allow_duplicates, args.dry_run
                    )
    if args.save or args.execute:
        saved = save_report(report, bool(report["execution"]))
        if not args.json:
            print("\nSaved:")
            for path in saved:
                print(f"- {path.relative_to(ROOT)}")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("ABORTED")
        raise SystemExit(130)
    except requests.RequestException as exc:
        print(f"Network/API error: {exc}", file=sys.stderr)
        raise SystemExit(1)
