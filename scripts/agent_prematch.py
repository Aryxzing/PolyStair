import json
import os
import re
import sys
import uuid
import argparse
import time
import hashlib
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from reasoning_trace_builder import build_reasoning_trace

load_dotenv()

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


ARENA = os.getenv("ARENA_BASE_URL", "https://stair-ai.com").rstrip("/")
ARENA_V1 = os.getenv("ARENA_API_PREFIX", "/api/v1").rstrip("/")
SPORTMONKS_PROXY = f"{ARENA}/api/v1/data/proxy/sportmonks/v3/football"
POLYMARKET_CLOB = f"{ARENA}/api/v1/data/proxy/polymarket-clob"
POLYMARKET_GAMMA = f"{ARENA}/api/v1/data/proxy/polymarket-gamma"

SUPABASE = os.getenv("SUPABASE_URL", "https://ezvbmtvrvzageqixvdak.supabase.co").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

SPORTMONKS_SEASON_ID = 26618
DEFAULT_FIXTURE_ID = 19609127
LEDGER_SCHEMA_VERSION = "0.3"
LLM_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
TICKER_RE = re.compile(r"^fifwc-([a-z]{2,4})-([a-z]{2,4})-\d{4}-\d{2}-\d{2}$")


PREDICT_SYS = """Return compact JSON only.
You are a football prediction agent. Use the provided independent data summaries,
not market prices, to estimate the 3-way match outcome.
Schema:
{"outcome":"<home_code|draw|away_code>","probability":0.0,"confidence_level":"low|medium|high","rationale":"brief"}
"""

STRATEGY_SYS = """Return compact JSON only.
You are a conservative trading agent. Compare the agent prediction with the
market mids and trade only when there is a meaningful edge.
Schema:
{"should_trade":false,"outcome":"<code|draw>","direction":"long|short","size_usdc":0.0,"limit_price":0.0,"edge_pp":0.0,"rationale":"brief"}
"""


def get_json(url, *, headers, params=None, timeout=20):
    response = requests.get(url, headers=headers, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def post_json(url, *, headers, payload, timeout=20):
    response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


def post_json_response(url, *, headers, payload, timeout=20):
    return requests.post(url, headers=headers, json=payload, timeout=timeout)


def unwrap_proxy(response_json):
    return response_json.get("body", response_json)


def parse_json_object(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in model response: {text[:200]}")
    return json.loads(match.group(0))


def public_headers():
    return {"apikey": SUPABASE_KEY}


def arena_headers(arena_key):
    return {"x-api-key": arena_key}


def arena_supabase_headers():
    return {"apikey": SUPABASE_KEY, "Accept-Profile": "world_cup_arena"}


def get_agent_me(arena_key):
    return get_json(f"{ARENA}{ARENA_V1}/arena/agents/me", headers=arena_headers(arena_key), timeout=20)


def get_exposure(arena_key, fixture_id=None, include_closing=True):
    params = {"include_closing": str(include_closing).lower()}
    if fixture_id:
        params["fixture_id"] = str(fixture_id)
    return get_json(f"{ARENA}{ARENA_V1}/arena/exposure", headers=arena_headers(arena_key), params=params, timeout=20)


def get_order_history(arena_key, fixture_id=None):
    params = {}
    if fixture_id:
        params["fixture_id"] = str(fixture_id)
    return get_json(f"{ARENA}{ARENA_V1}/arena/orders", headers=arena_headers(arena_key), params=params or None, timeout=20)


def get_match_timing(arena_key, fixture_id):
    return get_json(f"{ARENA}{ARENA_V1}/arena/matches/{fixture_id}", headers=arena_headers(arena_key), timeout=20)


def discover_fixture(arena_key, fixture_id):
    body = unwrap_proxy(
        get_json(
            f"{SPORTMONKS_PROXY}/schedules/seasons/{SPORTMONKS_SEASON_ID}",
            headers=arena_headers(arena_key),
            timeout=30,
        )
    )
    schedule = body.get("data", [])
    return {
        "fixture_id": fixture_id,
        "season_id": SPORTMONKS_SEASON_ID,
        "schedule_items_seen": len(schedule),
    }


def fetch_mapping(fixture_id):
    rows = get_json(
        f"{SUPABASE}/rest/v1/data_sportmonks_polymarket_mapping",
        headers=arena_supabase_headers(),
        params={
            "sportmonks_fixture_id": f"eq.{fixture_id}",
            "select": "polymarket_event_slug,home_country,home_short_code,away_country,away_short_code",
        },
        timeout=15,
    )
    if not rows:
        raise RuntimeError(f"No Polymarket mapping found for fixture {fixture_id}")
    return rows[0]


def fetch_fixture_detail(arena_key, fixture_id):
    body = unwrap_proxy(
        get_json(
            f"{SPORTMONKS_PROXY}/fixtures/{fixture_id}",
            headers=arena_headers(arena_key),
            params={"include": "participants;predictions;odds;xGFixture"},
            timeout=60,
        )
    )
    fixture = body["data"]
    participants = fixture.get("participants") or []
    home = next((p for p in participants if p.get("meta", {}).get("location") == "home"), None)
    away = next((p for p in participants if p.get("meta", {}).get("location") == "away"), None)
    return fixture, home, away


def fetch_polymarket(arena_key, event_slug):
    event_body = unwrap_proxy(
        get_json(
            f"{POLYMARKET_GAMMA}/events",
            headers=arena_headers(arena_key),
            params={"slug": event_slug},
            timeout=20,
        )
    )
    event = (event_body or [None])[0]
    if not event:
        raise RuntimeError(f"No Polymarket event found for slug {event_slug}")

    ticker = (event.get("ticker") or event_slug).lower()
    match = TICKER_RE.match(ticker)
    if not match:
        raise RuntimeError(f"Unexpected Polymarket ticker shape: {ticker}")
    home_code, away_code = match.groups()

    outcomes = {}
    for market in event.get("markets") or []:
        slug = (market.get("slug") or "").lower()
        suffix = slug[len(ticker) + 1 :] if slug.startswith(f"{ticker}-") else ""
        key = (
            "home"
            if suffix == home_code
            else "draw"
            if suffix == "draw"
            else "away"
            if suffix == away_code
            else None
        )
        if not key:
            continue

        token_yes = json.loads(market["clobTokenIds"])[0]
        midpoint = unwrap_proxy(
            get_json(
                f"{POLYMARKET_CLOB}/midpoint",
                headers=arena_headers(arena_key),
                params={"token_id": token_yes},
                timeout=15,
            )
        )
        outcomes[key] = {
            "team_code": "draw" if key == "draw" else (home_code if key == "home" else away_code).upper(),
            "condition_id": market.get("conditionId"),
            "token_yes": token_yes,
            "current_mid_yes": float(midpoint["mid"]) if midpoint and midpoint.get("mid") is not None else None,
        }

    return {
        "fixture": event.get("title"),
        "polymarket_event_slug": event_slug,
        "ticker": ticker,
        "outcomes": outcomes,
    }


def fetch_country_ids(home_country, away_country):
    rows = get_json(
        f"{SUPABASE}/rest/v1/dim_country",
        headers=arena_supabase_headers(),
        params={"country_name": f"in.(\"{home_country}\",\"{away_country}\")", "select": "country_id,country_name,continent"},
        timeout=15,
    )
    by_name = {row["country_name"]: row for row in rows}
    return by_name


def fetch_supabase_priors(country_ids):
    ids = ",".join(str(country_id) for country_id in country_ids)
    result = {}
    for table in ("ads_a_country_style", "ads_a_country_struct", "ads_a_ko_pattern", "ads_a_special_match"):
        result[table] = get_json(
            f"{SUPABASE}/rest/v1/{table}",
            headers=arena_supabase_headers(),
            params={"country_id": f"in.({ids})", "select": "*"},
            timeout=15,
        )
    return result


def sportmonks_digest(fixture, home, away):
    predictions = fixture.get("predictions") or []
    odds = fixture.get("odds") or []
    xg = fixture.get("xGFixture")
    return {
        "fixture": fixture.get("name"),
        "starting_at": fixture.get("starting_at"),
        "home": {"name": home.get("name") if home else None, "code": home.get("short_code") if home else None},
        "away": {"name": away.get("name") if away else None, "code": away.get("short_code") if away else None},
        "prediction_rows": len(predictions),
        "odds_rows": len(odds),
        "xg_available": bool(xg),
        "sample_prediction": predictions[0] if predictions else None,
        "summary": "Sportmonks fixture detail fetched with participants, predictions, odds, and xGFixture includes.",
    }


def polymarket_digest(moneyline, mapping):
    code_by_key = {
        "home": mapping["home_short_code"],
        "draw": "draw",
        "away": mapping["away_short_code"],
    }
    normalized_outcomes = {}
    for key, outcome in moneyline["outcomes"].items():
        normalized_outcomes[key] = {**outcome, "team_code": code_by_key[key]}
    mids = {
        outcome["team_code"]: outcome["current_mid_yes"]
        for outcome in normalized_outcomes.values()
        if outcome.get("current_mid_yes") is not None
    }
    return {
        "fixture": moneyline["fixture"],
        "polymarket_event_slug": moneyline["polymarket_event_slug"],
        "implied_win_prob": mids,
        "sum_implied_prob": round(sum(mids.values()), 4),
        "execution_handles": normalized_outcomes,
        "summary": "Polymarket moneyline event and YES-token midpoints fetched through arena proxies.",
    }


def supabase_digest(priors, country_rows):
    return {
        "countries": country_rows,
        "tables": {table: {"rows": len(rows), "sample": rows[:1]} for table, rows in priors.items()},
        "summary": "StatsBomb-derived country priors fetched from the world_cup_arena Supabase schema.",
    }


def normalize_probs(probs):
    clipped = {key: max(0.001, float(value)) for key, value in probs.items() if value is not None}
    total = sum(clipped.values())
    if total <= 0:
        return probs
    return {key: value / total for key, value in clipped.items()}


def extract_sportmonks_1x2(fixture, mapping):
    for row in fixture.get("predictions") or []:
        preds = row.get("predictions") or {}
        if {"home", "draw", "away"}.issubset(preds):
            return normalize_probs(
                {
                    mapping["home_short_code"]: float(preds["home"]) / 100,
                    "draw": float(preds["draw"]) / 100,
                    mapping["away_short_code"]: float(preds["away"]) / 100,
                }
            )
    return None


def extract_bookmaker_1x2(fixture, mapping):
    grouped = {}
    for odd in fixture.get("odds") or []:
        if odd.get("market_id") != 1:
            continue
        label = (odd.get("label") or "").lower()
        key = (
            mapping["home_short_code"]
            if label == "home"
            else "draw"
            if label == "draw"
            else mapping["away_short_code"]
            if label == "away"
            else None
        )
        if not key:
            continue
        try:
            decimal = float(odd.get("value"))
        except (TypeError, ValueError):
            continue
        grouped.setdefault(odd.get("bookmaker_id"), {})[key] = 1 / decimal

    normalized_books = [
        normalize_probs(book)
        for book in grouped.values()
        if {mapping["home_short_code"], "draw", mapping["away_short_code"]}.issubset(book)
    ]
    if not normalized_books:
        return None

    keys = [mapping["home_short_code"], "draw", mapping["away_short_code"]]
    return {
        key: sum(book[key] for book in normalized_books) / len(normalized_books)
        for key in keys
    }


def top_pick(probs):
    if not probs:
        return None
    return max(probs.items(), key=lambda item: item[1])[0]


def classify_margin(probs):
    ordered = sorted(probs.values(), reverse=True)
    if len(ordered) < 2:
        return 1.0
    return ordered[0] - ordered[1]


def confidence_from_metrics(edge_pp, source_agreement, data_quality, probability, concentration, disagreement=0.0):
    score = 0
    score += min(30, abs(edge_pp) * 2.0)
    score += min(25, source_agreement * 8.5)
    score += min(25, data_quality * 25)
    score += min(20, max(0.0, concentration) * 100)
    if probability > 0.55:
        score += 5
    if concentration < 0.05:
        score -= 15
    if disagreement > 0.25:
        score -= 15
    elif disagreement > 0.15:
        score -= 8
    score = max(0, min(100, round(score)))
    label = "high" if score >= 80 else "medium" if score >= 60 else "low"
    return score, label


def risk_factors_for_prediction(sportmonks_probs, bookmaker_probs, prior_probs, source_tops, outcome, concentration):
    risks = []
    if not sportmonks_probs:
        risks.append("Sportmonks 1X2 prediction unavailable")
    if not bookmaker_probs:
        risks.append("Bookmaker Fulltime Result consensus unavailable")
    if not prior_probs:
        risks.append("Supabase priors unavailable")
    if len(set(source_tops.values())) > 1:
        risks.append("Conflicting top picks across sources")
    if concentration < 0.05:
        risks.append("Top outcome is very close to the next-best alternative")
    elif concentration < 0.1:
        risks.append("Prediction is only narrowly separated from alternatives")
    if outcome == "draw":
        risks.append("Draw outcomes carry high variance")
    if prior_probs and max(prior_probs.values()) < 0.4:
        risks.append("Priors do not strongly favor any outcome")
    return risks or ["No major data-quality risk detected"]


def rejected_alternatives(probs, outcome):
    return [
        {"outcome": key, "probability": round(value, 4), "reason": "lower blended probability than selected outcome"}
        for key, value in sorted(probs.items(), key=lambda item: item[1], reverse=True)
        if key != outcome
    ]


def call_anthropic(anthropic_key, system, payload):
    if not anthropic:
        raise RuntimeError("anthropic package is not installed")
    client = anthropic.Anthropic(api_key=anthropic_key)
    response = client.messages.create(
        model=LLM_MODEL,
        max_tokens=800,
        system=system,
        messages=[{"role": "user", "content": json.dumps(payload)}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return parse_json_object(text), {
        "provider": "anthropic",
        "model_name": LLM_MODEL,
        "tokens_in": response.usage.input_tokens,
        "tokens_out": response.usage.output_tokens,
    }


def call_openai(openai_key, system, payload):
    if not OpenAI:
        raise RuntimeError("openai package is not installed")
    client = OpenAI(api_key=openai_key)
    models = [
        model.strip()
        for model in os.getenv("OPENAI_FALLBACK_MODELS", OPENAI_MODEL).split(",")
        if model.strip()
    ]
    last_error = None
    for model in models:
        try:
            response = client.responses.create(
                model=model,
                instructions=system,
                input=json.dumps(payload),
                max_output_tokens=800,
            )
            break
        except Exception as exc:
            last_error = exc
    else:
        raise last_error

    text = response.output_text
    usage = getattr(response, "usage", None)
    return parse_json_object(text), {
        "provider": "openai",
        "model_name": response.model,
        "tokens_in": getattr(usage, "input_tokens", None),
        "tokens_out": getattr(usage, "output_tokens", None),
    }


def fallback_prediction(mapping, poly_digest):
    mids = poly_digest["implied_win_prob"]
    if not mids:
        outcome = mapping["home_short_code"]
        probability = 0.34
    else:
        outcome, probability = max(mids.items(), key=lambda item: item[1])
    return {
        "outcome": outcome,
        "probability": probability,
        "confidence_level": "low",
        "rationale": "Fallback prediction because no model provider key is configured.",
    }


def _rows_by_country(rows):
    return {row.get("country_id"): row for row in rows or []}


def rules_prediction(mapping, priors, country_rows):
    by_country_name = country_rows
    home_country = by_country_name.get(mapping["home_country"], {})
    away_country = by_country_name.get(mapping["away_country"], {})
    home_id = home_country.get("country_id")
    away_id = away_country.get("country_id")

    styles = _rows_by_country(priors.get("ads_a_country_style"))
    structures = _rows_by_country(priors.get("ads_a_country_struct"))
    ko = _rows_by_country(priors.get("ads_a_ko_pattern"))
    special = _rows_by_country(priors.get("ads_a_special_match"))

    def team_score(country_id, is_home):
        style = styles.get(country_id, {})
        struct = structures.get(country_id, {})
        ko_row = ko.get(country_id, {})
        special_row = special.get(country_id, {})

        score = 0.15 if is_home else 0.0
        score += float(style.get("conversion_rate") or 0) * 3.0
        score -= float(style.get("group_gpg") or 1.5) * 0.18
        score -= float(style.get("ko_gpg") or 1.5) * 0.08
        score += float(struct.get("recent_form_score_last_4_years") or 0) * 0.02
        score += float(ko_row.get("ceiling_consistency_rate") or 0) * 0.15
        score -= float(ko_row.get("first_ko_loss_rate") or 0) * 0.05
        score += float(special_row.get("et_win_rate") or 0) * 0.04
        score += float(special_row.get("pen_win_rate") or 0) * 0.03
        return score

    home_score = team_score(home_id, True)
    away_score = team_score(away_id, False)
    diff = max(-1.5, min(1.5, home_score - away_score))

    draw = 0.27 + max(0.0, 0.08 - abs(diff) * 0.04)
    home = 0.365 + diff * 0.14
    away = 1.0 - home - draw

    probs = {
        mapping["home_short_code"]: max(0.08, home),
        "draw": max(0.12, draw),
        mapping["away_short_code"]: max(0.08, away),
    }
    total = sum(probs.values())
    probs = {key: value / total for key, value in probs.items()}
    outcome, probability = max(probs.items(), key=lambda item: item[1])
    confidence = "medium" if probability >= 0.42 else "low"

    return {
        "outcome": outcome,
        "probability": round(probability, 4),
        "confidence_level": confidence,
        "rationale": (
            "Rules model using Supabase priors only: set-piece conversion, "
            "goals-against rates, knockout/special-match indicators, and a small home prior."
        ),
        "probabilities": {key: round(value, 4) for key, value in probs.items()},
        "model_scores": {
            mapping["home_short_code"]: round(home_score, 4),
            mapping["away_short_code"]: round(away_score, 4),
        },
    }


MODEL_VARIANTS = {
    "baseline": {
        "sportmonks": 0.45,
        "bookmaker": 0.35,
        "supabase": 0.20,
    },
    "research_602020": {
        "sportmonks": 0.60,
        "bookmaker": 0.20,
        "supabase": 0.20,
    },
}
STRATEGY_MODES = {"winner_only", "three_outcome_value", "qualification_hybrid_9010", "qualification_probability_ladder"}


def model_variant_weights(model_variant):
    if model_variant not in MODEL_VARIANTS:
        raise ValueError(f"Unknown model_variant {model_variant!r}; choose one of {', '.join(MODEL_VARIANTS)}")
    return MODEL_VARIANTS[model_variant]


def blended_prediction(fixture, mapping, priors, country_rows, model_variant="baseline"):
    weights = model_variant_weights(model_variant)
    prior = rules_prediction(mapping, priors, country_rows)
    keys = [mapping["home_short_code"], "draw", mapping["away_short_code"]]
    prior_probs = prior["probabilities"]
    sportmonks_probs = extract_sportmonks_1x2(fixture, mapping)
    bookmaker_probs = extract_bookmaker_1x2(fixture, mapping)

    weighted = {key: 0.0 for key in keys}
    total_weight = 0.0
    components = {}
    source_tops = {}

    if sportmonks_probs:
        for key in keys:
            weighted[key] += sportmonks_probs[key] * weights["sportmonks"]
        total_weight += weights["sportmonks"]
        components["sportmonks_1x2"] = {key: round(sportmonks_probs[key], 4) for key in keys}
        source_tops["sportmonks_1x2"] = top_pick(sportmonks_probs)

    if bookmaker_probs:
        for key in keys:
            weighted[key] += bookmaker_probs[key] * weights["bookmaker"]
        total_weight += weights["bookmaker"]
        components["bookmaker_consensus"] = {key: round(bookmaker_probs[key], 4) for key in keys}
        source_tops["bookmaker_consensus"] = top_pick(bookmaker_probs)

    for key in keys:
        weighted[key] += prior_probs[key] * weights["supabase"]
    total_weight += weights["supabase"]
    components["supabase_priors"] = {key: round(prior_probs[key], 4) for key in keys}
    source_tops["supabase_priors"] = top_pick(prior_probs)

    blended = normalize_probs({key: weighted[key] / total_weight for key in keys})
    outcome, probability = max(blended.items(), key=lambda item: item[1])
    concentration = classify_margin(blended)

    component_values = []
    for probs in (sportmonks_probs, bookmaker_probs, prior_probs):
        if probs and outcome in probs:
            component_values.append(probs[outcome])
    disagreement = max(component_values) - min(component_values) if len(component_values) >= 2 else 0.25
    data_sources = int(bool(sportmonks_probs)) + int(bool(bookmaker_probs)) + 1
    source_agreement = sum(1 for pick in source_tops.values() if pick == outcome)
    data_quality = data_sources / 3.0
    confidence_score, confidence = confidence_from_metrics(0, source_agreement, data_quality, probability, concentration, disagreement)
    risk_factors = risk_factors_for_prediction(
        sportmonks_probs,
        bookmaker_probs,
        prior_probs,
        source_tops,
        outcome,
        concentration,
    )

    return {
        "outcome": outcome,
        "probability": round(probability, 4),
        "confidence_level": confidence,
        "rationale": (
            "Blended model using Sportmonks 1X2 predictions, bookmaker Fulltime Result consensus, "
            "and Supabase priors. Market prices are excluded from prediction and used only for edge."
        ),
        "probabilities": {key: round(value, 4) for key, value in blended.items()},
        "components": components,
        "data_sources": data_sources,
        "data_quality": round(data_quality, 4),
        "source_tops": source_tops,
        "source_agreement": source_agreement,
        "prediction_concentration": round(concentration, 4),
        "confidence_score": confidence_score,
        "risk_factors": risk_factors,
        "rejected_alternatives": rejected_alternatives(blended, outcome),
        "component_disagreement": round(disagreement, 4),
        "model_variant": model_variant,
        "model_weights": weights,
    }


def fallback_strategy(prediction, poly_digest):
    market_prob = poly_digest["implied_win_prob"].get(prediction["outcome"])
    edge_pp = None if market_prob is None else round((prediction["probability"] - market_prob) * 100, 2)
    return {
        "should_trade": False,
        "outcome": prediction["outcome"],
        "direction": "long",
        "orders": [],
        "size_usdc": 0.0,
        "limit_price": market_prob or 0.0,
        "edge_pp": edge_pp or 0.0,
        "rationale": "Dry-run fallback strategy; configure a model key before trading.",
    }


def outcome_type_for_code(outcome, mapping):
    if outcome == "draw":
        return "draw"
    if outcome == mapping.get("home_short_code"):
        return "home"
    if outcome == mapping.get("away_short_code"):
        return "away"
    return "unknown"


def all_outcome_edges(prediction, poly_digest, mapping):
    rows = []
    market = poly_digest.get("implied_win_prob") or {}
    for outcome in (mapping.get("home_short_code"), "draw", mapping.get("away_short_code")):
        model_probability = (prediction.get("probabilities") or {}).get(outcome)
        market_midpoint = market.get(outcome)
        if outcome is None or model_probability is None or market_midpoint is None:
            continue
        rows.append(
            {
                "outcome": outcome,
                "outcome_type": outcome_type_for_code(outcome, mapping),
                "model_probability": round(float(model_probability), 4),
                "market_midpoint": round(float(market_midpoint), 4),
                "edge_pp": round((float(model_probability) - float(market_midpoint)) * 100, 2),
                "is_model_winner": outcome == prediction.get("outcome"),
            }
        )
    return sorted(rows, key=lambda row: row["edge_pp"], reverse=True)


def three_outcome_value_strategy(prediction, poly_digest, mapping):
    winner_only_reference = normalize_strategy(fallback_strategy(prediction, poly_digest), prediction, poly_digest)
    threshold_pp = float(os.getenv("TRADE_EDGE_THRESHOLD_PP", "6"))
    max_trade_usdc = float(os.getenv("MAX_TRADE_USDC", "2"))
    rows = all_outcome_edges(prediction, poly_digest, mapping)
    positive_rows = [row for row in rows if row["edge_pp"] > 0]
    selected = positive_rows[0] if positive_rows else rows[0] if rows else None
    market = poly_digest.get("implied_win_prob") or {}
    market_favorite = max(market.items(), key=lambda item: item[1])[0] if market else None
    data_quality = float(prediction.get("data_quality") or 0.0)
    source_agreement = int(prediction.get("source_agreement") or 0)
    confidence_level = prediction.get("confidence_level")
    confidence_score = int(prediction.get("confidence_score") or 0)

    strategy = {
        "strategy_mode": "three_outcome_value",
        "should_trade": False,
        "outcome": selected["outcome"] if selected else prediction.get("outcome"),
        "direction": "long",
        "orders": [],
        "size_usdc": 0.0,
        "limit_price": selected["market_midpoint"] if selected else 0.0,
        "edge_pp": selected["edge_pp"] if selected else 0.0,
        "market_midpoint": selected["market_midpoint"] if selected else None,
        "data_quality": round(data_quality, 4),
        "source_agreement": source_agreement,
        "confidence_level": confidence_level,
        "confidence_score": confidence_score,
        "all_outcome_edges": rows,
        "model_winner": prediction.get("outcome"),
        "selected_value_outcome": selected["outcome"] if selected else None,
        "market_favorite": market_favorite,
        "winner_only_reference": {
            "outcome": winner_only_reference.get("outcome"),
            "edge_pp": winner_only_reference.get("edge_pp"),
            "confidence_score": winner_only_reference.get("confidence_score"),
            "confidence_level": winner_only_reference.get("confidence_level"),
        },
        "rationale": (
            "Three-outcome value mode scans home/draw/away YES prices after prediction. "
            "The selected value outcome can differ from the model winner when the market favorite is overpriced "
            "and another outcome has the highest positive model-minus-market edge."
        ),
    }
    edge_pp = float(strategy["edge_pp"] or 0)
    if (
        selected
        and edge_pp >= threshold_pp
        and confidence_level in ("medium", "high")
        and data_quality >= 0.7
        and source_agreement >= 2
    ):
        size = min(max_trade_usdc, max(1.0, edge_pp / 5.0))
        strategy["should_trade"] = True
        strategy["size_usdc"] = round(size, 2)
        strategy["orders"] = [
            {
                "outcome": selected["outcome"],
                "side": "buy_yes",
                "size_usdc": round(size, 2),
                "limit_price": selected["market_midpoint"],
            }
        ]
        strategy["no_trade_reason"] = None
    else:
        reasons = []
        if not selected:
            reasons.append("no complete outcome market data")
        if edge_pp < threshold_pp:
            reasons.append(f"edge {edge_pp} pp below threshold {threshold_pp} pp")
        if confidence_level not in ("medium", "high"):
            reasons.append("confidence is low")
        if data_quality < 0.7:
            reasons.append(f"data quality {data_quality:.2f} below 0.70")
        if source_agreement < 2:
            reasons.append(f"source agreement {source_agreement}/3 below 2")
        strategy["no_trade_reason"] = "; ".join(reasons) or "trade gate blocked"
    strategy["opportunity_class"] = classify_opportunity(edge_pp, confidence_score, data_quality, strategy["should_trade"])
    strategy["recommended_action"] = recommended_action(strategy)
    return strategy


def deterministic_bucket(*parts):
    raw = ":".join(str(part) for part in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def hybrid_fail_reasons(winner_gate, value_gate):
    reasons = []
    if not winner_gate["passed"]:
        reasons.append("winner gate failed: " + "; ".join(winner_gate["reasons"]))
    if not value_gate["passed"]:
        reasons.append("value override gate failed: " + "; ".join(value_gate["reasons"]))
    return " | ".join(reasons)


def gate_result(passed, reasons, **extra):
    result = {
        "passed": bool(passed),
        "reason": "passed" if passed else "; ".join(reasons),
    }
    result.update(extra)
    return result


def qualification_hybrid_9010_strategy(prediction, poly_digest, mapping):
    base = three_outcome_value_strategy(prediction, poly_digest, mapping)
    threshold_pp = float(os.getenv("TRADE_EDGE_THRESHOLD_PP", "6"))
    max_trade_usdc = float(os.getenv("MAX_TRADE_USDC", "2"))
    winner_weight = float(os.getenv("HYBRID_WINNER_WEIGHT", "0.90"))
    value_weight = float(os.getenv("HYBRID_VALUE_WEIGHT", "0.10"))
    override_rate = float(os.getenv("HYBRID_VALUE_OVERRIDE_RATE", str(value_weight)))
    min_confidence = int(os.getenv("HYBRID_MIN_CONFIDENCE", "70"))
    min_source_agreement = int(os.getenv("HYBRID_MIN_SOURCE_AGREEMENT", "2"))
    min_winner_prob = float(os.getenv("HYBRID_MIN_WINNER_PROB", "0.44"))
    max_winner_price = float(os.getenv("HYBRID_MAX_WINNER_PRICE", "0.90"))
    min_winner_edge = float(os.getenv("HYBRID_MIN_WINNER_EDGE_PP", "-5.0"))
    value_min_edge = float(os.getenv("HYBRID_VALUE_MIN_EDGE_PP", "15.0"))
    value_min_confidence = int(os.getenv("HYBRID_VALUE_MIN_CONFIDENCE", "85"))
    value_min_source_agreement = int(os.getenv("HYBRID_VALUE_MIN_SOURCE_AGREEMENT", "3"))
    value_max_price = float(os.getenv("HYBRID_VALUE_MAX_PRICE", "0.20"))

    fixture_id = str(mapping.get("sportmonks_fixture_id") or "")
    model_winner = prediction.get("outcome")
    selected_value = base.get("selected_value_outcome") or base.get("outcome")
    all_edges = base.get("all_outcome_edges") or []
    winner_row = next((row for row in all_edges if row.get("outcome") == model_winner), None)
    value_row = next((row for row in all_edges if row.get("outcome") == selected_value), None)
    confidence_score = int(prediction.get("confidence_score") or base.get("confidence_score") or 0)
    source_agreement = int(prediction.get("source_agreement") or base.get("source_agreement") or 0)
    winner_probability = float(prediction.get("probability") or 0)
    winner_market_midpoint = winner_row.get("market_midpoint") if winner_row else None
    winner_edge_pp = float(winner_row.get("edge_pp") or 0) if winner_row else 0.0
    value_edge_pp = float(value_row.get("edge_pp") or 0) if value_row else 0.0
    value_market_midpoint = value_row.get("market_midpoint") if value_row else None
    bucket = deterministic_bucket(fixture_id, model_winner, selected_value)

    winner_reasons = []
    if confidence_score < min_confidence:
        winner_reasons.append(f"confidence {confidence_score} below {min_confidence}")
    if source_agreement < min_source_agreement:
        winner_reasons.append(f"source agreement {source_agreement} below {min_source_agreement}")
    if winner_probability < min_winner_prob:
        winner_reasons.append(f"winner probability {winner_probability:.4f} below {min_winner_prob:.4f}")
    if winner_market_midpoint is None:
        winner_reasons.append("winner market midpoint missing")
    elif float(winner_market_midpoint) > max_winner_price:
        winner_reasons.append(f"winner market midpoint {float(winner_market_midpoint):.4f} above {max_winner_price:.4f}")
    if winner_edge_pp < min_winner_edge:
        winner_reasons.append(f"winner edge {winner_edge_pp:.2f} below {min_winner_edge:.2f}")
    winner_gate = {"passed": not winner_reasons, "reasons": winner_reasons}

    value_reasons = []
    if not value_row:
        value_reasons.append("selected value row missing")
    if value_edge_pp < value_min_edge:
        value_reasons.append(f"value edge {value_edge_pp:.2f} below {value_min_edge:.2f}")
    if confidence_score < value_min_confidence:
        value_reasons.append(f"confidence {confidence_score} below {value_min_confidence}")
    if source_agreement < value_min_source_agreement:
        value_reasons.append(f"source agreement {source_agreement} below {value_min_source_agreement}")
    if value_market_midpoint is None:
        value_reasons.append("value market midpoint missing")
    elif float(value_market_midpoint) > value_max_price:
        value_reasons.append(f"value market midpoint {float(value_market_midpoint):.4f} above {value_max_price:.4f}")
    if bucket >= override_rate:
        value_reasons.append(f"deterministic bucket {bucket:.4f} outside override rate {override_rate:.4f}")
    value_gate = {"passed": not value_reasons, "reasons": value_reasons, "bucket": round(bucket, 6)}

    execution_row = None
    execution_selector = None
    execution_reason = None
    if value_gate["passed"]:
        execution_row = value_row
        execution_selector = "value_override"
        execution_reason = (
            f"Value override selected because edge {value_edge_pp:.2f} pp, confidence {confidence_score}, "
            f"source agreement {source_agreement}, price {float(value_market_midpoint):.4f}, and bucket {bucket:.4f} "
            f"passed the {override_rate:.2%} override allocation."
        )
    elif winner_gate["passed"]:
        execution_row = winner_row
        execution_selector = "winner_default"
        execution_reason = (
            f"Hybrid defaulted to model winner {model_winner} to reduce qualification variance; "
            f"winner gate passed with confidence {confidence_score}, source agreement {source_agreement}, "
            f"probability {winner_probability:.4f}, market price {float(winner_market_midpoint):.4f}, "
            f"and winner edge {winner_edge_pp:.2f} pp."
        )

    strategy = dict(base)
    strategy["strategy_mode"] = "qualification_hybrid_9010"
    strategy["execution_selector"] = execution_selector or "no_trade"
    strategy["execution_reason"] = execution_reason or hybrid_fail_reasons(winner_gate, value_gate)
    strategy["execution_outcome"] = execution_row.get("outcome") if execution_row else None
    strategy["value_candidate_preserved"] = selected_value
    strategy["hybrid_weights"] = {
        "winner": round(winner_weight, 4),
        "value": round(value_weight, 4),
        "value_override_rate": round(override_rate, 4),
        "bucket": round(bucket, 6),
    }
    strategy["hybrid_gates"] = {
        "winner_gate": winner_gate,
        "value_override_gate": value_gate,
    }
    strategy["winner_edge_pp"] = round(winner_edge_pp, 2)
    strategy["selected_value_edge_pp"] = round(value_edge_pp, 2)
    strategy["winner_gate_result"] = gate_result(
        winner_gate["passed"],
        winner_gate["reasons"],
        min_winner_edge_pp=min_winner_edge,
        winner_edge_pp=round(winner_edge_pp, 2),
    )
    strategy["value_gate_result"] = gate_result(
        value_gate["passed"],
        value_gate["reasons"],
        min_value_edge_pp=value_min_edge,
        value_edge_pp=round(value_edge_pp, 2),
        bucket=round(bucket, 6),
    )
    strategy["execution_gate_result"] = gate_result(
        execution_row is not None,
        [] if execution_row else [strategy["execution_reason"]],
        selected_case=execution_selector or "none",
    )

    if execution_row:
        edge_pp = float(execution_row.get("edge_pp") or 0.0)
        market_midpoint = float(execution_row.get("market_midpoint"))
        size = min(max_trade_usdc, max(1.0, abs(edge_pp) / 5.0))
        strategy["outcome"] = execution_row["outcome"]
        strategy["edge_pp"] = round(edge_pp, 2)
        strategy["market_midpoint"] = round(market_midpoint, 4)
        strategy["limit_price"] = round(market_midpoint, 4)
        strategy["size_usdc"] = round(size, 2)
        strategy["should_trade"] = True
        strategy["orders"] = [
            {
                "outcome": execution_row["outcome"],
                "side": "buy_yes",
                "size_usdc": round(size, 2),
                "limit_price": round(market_midpoint, 4),
            }
        ]
        strategy["no_trade_reason"] = None
    else:
        strategy["outcome"] = model_winner
        strategy["execution_outcome"] = model_winner
        strategy["edge_pp"] = winner_row.get("edge_pp", 0.0) if winner_row else 0.0
        strategy["market_midpoint"] = winner_market_midpoint
        strategy["limit_price"] = winner_market_midpoint or 0.0
        strategy["size_usdc"] = 0.0
        strategy["should_trade"] = False
        strategy["orders"] = []
        strategy["no_trade_reason"] = strategy["execution_reason"]

    strategy["opportunity_class"] = classify_opportunity(
        float(strategy.get("edge_pp") or 0.0),
        confidence_score,
        float(strategy.get("data_quality") or 0.0),
        bool(strategy.get("should_trade")),
    )
    if strategy.get("should_trade") and strategy.get("opportunity_class") == "Avoid":
        if strategy.get("execution_selector") == "winner_default":
            strategy["opportunity_class"] = "Acceptable Favorite"
        elif strategy.get("execution_selector") == "value_override":
            strategy["opportunity_class"] = "True Value Bet"
    strategy["recommended_action"] = recommended_action(strategy)
    strategy["rationale"] = (
        "Qualification hybrid 90/10 preserves three-outcome value calculations but executes the model winner by default "
        "to reduce short-competition variance. A value override is allowed only when strict value gates pass and the "
        "deterministic allocation bucket falls inside the configured override rate."
    )
    return strategy


def normalize_strategy(strategy, prediction, poly_digest):
    threshold_pp = float(os.getenv("TRADE_EDGE_THRESHOLD_PP", "6"))
    max_trade_usdc = float(os.getenv("MAX_TRADE_USDC", "2"))
    outcome = strategy.get("outcome") or prediction.get("outcome")
    market_prob = poly_digest["implied_win_prob"].get(outcome)
    if market_prob is None:
        strategy["should_trade"] = False
        strategy["edge_pp"] = 0.0
        strategy["rationale"] = f"{strategy.get('rationale', '')} Market price missing for {outcome}.".strip()
        return strategy

    edge_pp = round((float(prediction["probability"]) - float(market_prob)) * 100, 2)
    data_quality = float(prediction.get("data_quality") or 0.0)
    source_agreement = int(prediction.get("source_agreement") or 0)
    confidence_score, confidence_label = confidence_from_metrics(
        edge_pp,
        source_agreement,
        data_quality,
        float(prediction["probability"]),
        float(prediction.get("prediction_concentration") or 0),
        float(prediction.get("component_disagreement") or 0),
    )
    prediction["confidence_score"] = confidence_score
    prediction["confidence_level"] = confidence_label
    strategy["outcome"] = outcome
    strategy["edge_pp"] = edge_pp
    strategy["market_midpoint"] = round(float(market_prob), 4)
    strategy["data_quality"] = round(data_quality, 4)
    strategy["source_agreement"] = source_agreement
    strategy["confidence_level"] = prediction.get("confidence_level")
    strategy["confidence_score"] = confidence_score

    if (
        edge_pp >= threshold_pp
        and prediction.get("confidence_level") in ("medium", "high")
        and data_quality >= 0.7
        and source_agreement >= 2
    ):
        strategy["direction"] = "long"
        strategy["should_trade"] = True
        strategy["limit_price"] = round(float(market_prob), 4)
        size = min(max_trade_usdc, max(1.0, edge_pp / 5.0))
        strategy["size_usdc"] = round(size, 2)
        strategy["orders"] = [
            {
                "outcome": outcome,
                "side": "buy_yes",
                "size_usdc": round(size, 2),
                "limit_price": round(float(market_prob), 4),
            }
        ]
        strategy["no_trade_reason"] = None
    else:
        strategy["direction"] = "long"
        strategy["should_trade"] = False
        strategy["size_usdc"] = 0.0
        strategy["orders"] = []
        strategy["limit_price"] = round(float(market_prob), 4)
        reasons = []
        if edge_pp < threshold_pp:
            reasons.append(f"edge {edge_pp} pp below threshold {threshold_pp} pp")
        if prediction.get("confidence_level") == "low":
            reasons.append("confidence is low")
        if data_quality < 0.7:
            reasons.append(f"data quality {data_quality:.2f} below 0.70")
        if source_agreement < 2:
            reasons.append(f"source agreement {source_agreement}/3 below 2")
        strategy["no_trade_reason"] = "; ".join(reasons) or "trade gate blocked"
    strategy["opportunity_class"] = classify_opportunity(edge_pp, confidence_score, data_quality, strategy["should_trade"])
    strategy["recommended_action"] = recommended_action(strategy)

    return strategy


def probability_ladder_bucket(probability, market_midpoint):
    if probability >= 0.70 and market_midpoint <= 0.85:
        return {"bucket": "70_plus", "suggested_size": 5.0, "max_market_price": 0.85}
    if 0.60 <= probability < 0.70 and market_midpoint <= 0.75:
        return {"bucket": "60_70", "suggested_size": 2.0, "max_market_price": 0.75}
    if 0.50 <= probability < 0.60 and market_midpoint <= 0.65:
        return {"bucket": "50_60", "suggested_size": 1.0, "max_market_price": 0.65}
    return None


def qualification_probability_ladder_strategy(prediction, poly_digest, mapping):
    base = three_outcome_value_strategy(prediction, poly_digest, mapping)
    market = poly_digest.get("implied_win_prob") or {}
    model_winner = prediction.get("outcome")
    confidence_score = int(prediction.get("confidence_score") or 0)
    source_agreement = int(prediction.get("source_agreement") or 0)
    probability = float(prediction.get("probability") or 0.0)
    winner_market = market.get(model_winner)
    winner_market_float = float(winner_market) if winner_market is not None else None
    all_edges = base.get("all_outcome_edges") or []
    value_rows = [
        row for row in all_edges
        if row.get("outcome") != model_winner and row.get("market_midpoint") is not None
    ]
    value_rows.sort(
        key=lambda row: (
            float(row.get("edge_pp") or 0),
            1 if row.get("outcome") != "draw" else 0,
            -float(row.get("market_midpoint") or 1),
        ),
        reverse=True,
    )
    value_row = value_rows[0] if value_rows else None
    value_edge = float(value_row.get("edge_pp") or 0.0) if value_row else 0.0
    value_market = value_row.get("market_midpoint") if value_row else None

    strategy = dict(base)
    strategy["strategy_mode"] = "qualification_probability_ladder"
    strategy["model_winner"] = model_winner
    strategy["execution_selector"] = "no_trade"
    strategy["execution_outcome"] = model_winner
    strategy["candidate_type"] = "none"
    strategy["ladder_bucket"] = None
    strategy["suggested_size"] = 0.0
    strategy["selected_value_outcome"] = value_row.get("outcome") if value_row else None
    strategy["selected_value_edge_pp"] = round(value_edge, 2)
    strategy["selected_value_market_midpoint"] = round(float(value_market), 4) if value_market is not None else None

    reasons = []
    if model_winner == "draw":
        reasons.append("draw is not allowed in winner ladder")
    if confidence_score < 65:
        reasons.append(f"confidence {confidence_score} below 65")
    if source_agreement < 2:
        reasons.append(f"source agreement {source_agreement} below 2")
    if probability < 0.40:
        reasons.append(f"model probability {probability:.4f} below 0.4000")
    if winner_market_float is None:
        reasons.append("winner market midpoint missing")
    elif winner_market_float <= 0 or winner_market_float >= 1:
        reasons.append(f"winner market midpoint {winner_market_float:.4f} outside (0,1)")

    bucket = probability_ladder_bucket(probability, winner_market_float) if winner_market_float is not None else None
    if winner_market_float is not None and not bucket:
        reasons.append("winner probability/price did not fit ladder bucket")

    if not reasons and bucket:
        edge_pp = round((probability - winner_market_float) * 100, 2)
        strategy.update(
            {
                "outcome": model_winner,
                "execution_outcome": model_winner,
                "execution_selector": "winner_ladder",
                "candidate_type": "winner_ladder",
                "ladder_bucket": bucket["bucket"],
                "suggested_size": bucket["suggested_size"],
                "size_usdc": bucket["suggested_size"],
                "limit_price": round(winner_market_float, 4),
                "market_midpoint": round(winner_market_float, 4),
                "edge_pp": edge_pp,
                "should_trade": True,
                "orders": [
                    {
                        "outcome": model_winner,
                        "side": "buy_yes",
                        "size_usdc": bucket["suggested_size"],
                        "limit_price": round(winner_market_float, 4),
                    }
                ],
                "no_trade_reason": None,
                "execution_reason": (
                    f"Winner ladder bucket {bucket['bucket']} selected because model winner {model_winner} "
                    f"is {probability:.2%}, market is {winner_market_float:.2%}, confidence {confidence_score}, "
                    f"and source agreement {source_agreement}."
                ),
            }
        )
    else:
        value_reasons = []
        if not value_row:
            value_reasons.append("no non-winner value candidate")
        if value_edge < 15.0:
            value_reasons.append(f"value edge {value_edge:.2f} below 15.00")
        if confidence_score < 85:
            value_reasons.append(f"confidence {confidence_score} below 85")
        if source_agreement < 3:
            value_reasons.append(f"source agreement {source_agreement} below 3")
        if value_market is None:
            value_reasons.append("value market midpoint missing")
        elif float(value_market) > 0.20:
            value_reasons.append(f"value market midpoint {float(value_market):.4f} above 0.2000")

        if not value_reasons and value_row:
            strategy.update(
                {
                    "outcome": value_row["outcome"],
                    "execution_outcome": value_row["outcome"],
                    "execution_selector": "value_slot",
                    "candidate_type": "value_slot",
                    "ladder_bucket": "value_slot",
                    "suggested_size": 1.0,
                    "size_usdc": 1.0,
                    "limit_price": round(float(value_market), 4),
                    "market_midpoint": round(float(value_market), 4),
                    "edge_pp": round(value_edge, 2),
                    "should_trade": True,
                    "orders": [
                        {
                            "outcome": value_row["outcome"],
                            "side": "buy_yes",
                            "size_usdc": 1.0,
                            "limit_price": round(float(value_market), 4),
                        }
                    ],
                    "no_trade_reason": None,
                    "execution_reason": (
                        f"Strict value slot selected {value_row['outcome']} with edge {value_edge:.2f} pp, "
                        f"market {float(value_market):.2%}, confidence {confidence_score}, and 3-source agreement."
                    ),
                }
            )
        else:
            strategy["should_trade"] = False
            strategy["orders"] = []
            strategy["size_usdc"] = 0.0
            strategy["limit_price"] = winner_market_float or 0.0
            strategy["market_midpoint"] = winner_market_float
            strategy["edge_pp"] = round((probability - winner_market_float) * 100, 2) if winner_market_float is not None else 0.0
            strategy["no_trade_reason"] = "winner ladder failed: " + "; ".join(reasons) + " | value slot failed: " + "; ".join(value_reasons)
            strategy["execution_reason"] = strategy["no_trade_reason"]

    strategy["opportunity_class"] = "Qualification Ladder" if strategy.get("should_trade") else "Avoid"
    strategy["recommended_action"] = recommended_action(strategy)
    return strategy


def select_strategy(prediction, poly_digest, mapping, strategy_mode):
    if strategy_mode not in STRATEGY_MODES:
        raise ValueError(f"Unknown STRATEGY_MODE {strategy_mode!r}; choose one of {', '.join(sorted(STRATEGY_MODES))}")
    if strategy_mode == "qualification_probability_ladder":
        return qualification_probability_ladder_strategy(prediction, poly_digest, mapping)
    if strategy_mode == "qualification_hybrid_9010":
        return qualification_hybrid_9010_strategy(prediction, poly_digest, mapping)
    if strategy_mode == "three_outcome_value":
        return three_outcome_value_strategy(prediction, poly_digest, mapping)
    strategy = fallback_strategy(prediction, poly_digest)
    strategy = normalize_strategy(strategy, prediction, poly_digest)
    strategy["strategy_mode"] = "winner_only"
    return strategy


def classify_opportunity(edge_pp, confidence_score, data_quality, should_trade):
    if should_trade and edge_pp >= 12 and confidence_score >= 80 and data_quality >= 0.85:
        return "A+ Opportunity"
    if should_trade and edge_pp >= 8 and confidence_score >= 70:
        return "A Opportunity"
    if edge_pp >= 6 and confidence_score >= 60 and data_quality >= 0.7:
        return "B Opportunity"
    if edge_pp > 0:
        return "Watchlist"
    return "Avoid"


def recommended_action(strategy):
    if strategy.get("should_trade"):
        if strategy.get("strategy_mode") == "qualification_probability_ladder":
            return (
                f"Probability ladder {strategy.get('candidate_type')} buy-YES {strategy['outcome']} "
                f"for ${float(strategy['size_usdc']):.2f} at latest midpoint {strategy['limit_price']}."
            )
        if strategy.get("strategy_mode") == "qualification_hybrid_9010":
            return (
                f"Hybrid {strategy.get('execution_selector')} buy-YES {strategy['outcome']} "
                f"at limit {strategy['limit_price']} for ${strategy['size_usdc']:.2f}. "
                f"Model winner={strategy.get('model_winner')}; value candidate={strategy.get('selected_value_outcome')}."
            )
        return f"Consider tiny production buy-YES {strategy['outcome']} at limit {strategy['limit_price']} for ${strategy['size_usdc']:.2f}."
    if strategy.get("edge_pp", 0) > 0:
        return f"Watch only: {strategy.get('no_trade_reason', 'trade gate blocked')}."
    return "Avoid trade: market price is not favorable versus model probability."


def structured_reasoning(data, prediction, strategy):
    mapping = data.get("mapping") or {}
    match = data.get("sportmonks_digest", {}).get("fixture") or data.get("polymarket_digest", {}).get("fixture")
    market = data.get("polymarket_digest", {}).get("implied_win_prob", {})
    outcome = prediction["outcome"]
    strategy_mode = strategy.get("strategy_mode", "winner_only")
    selected_value = strategy.get("selected_value_outcome") or strategy.get("outcome")
    all_edges = strategy.get("all_outcome_edges") or []
    market_favorite = strategy.get("market_favorite")
    if strategy_mode == "qualification_probability_ladder":
        trade_decision = (
            f"Model winner is {strategy.get('model_winner') or outcome} at {prediction['probability']:.2%}. "
            f"Execution outcome is {strategy.get('execution_outcome') or strategy.get('outcome')} as "
            f"{strategy.get('candidate_type')} in ladder bucket {strategy.get('ladder_bucket')}. "
            f"Market price is {strategy.get('market_midpoint')}; suggested size is ${float(strategy.get('suggested_size') or strategy.get('size_usdc') or 0):.2f}. "
            f"{strategy.get('execution_reason') or strategy.get('no_trade_reason')} "
            f"Decision: {'trade' if strategy.get('should_trade') else 'no trade'}."
        )
    elif strategy_mode == "qualification_hybrid_9010":
        trade_decision = (
            f"Model winner is {strategy.get('model_winner') or outcome} at {prediction['probability']:.2%}. "
            f"Three-outcome value candidate is {selected_value}. "
            f"Hybrid executed outcome is {strategy.get('execution_outcome') or strategy.get('outcome')} via "
            f"{strategy.get('execution_selector')}. {strategy.get('execution_reason')} "
            "This reduces qualification variance by defaulting to the model winner and allowing only a small, "
            "strictly gated deterministic allocation to value overrides. "
            f"Decision: {'trade' if strategy.get('should_trade') else 'no trade'}."
        )
    elif strategy_mode == "three_outcome_value":
        trade_decision = (
            f"Model's most likely winner is {outcome} at {prediction['probability']:.2%}. "
            f"Three-outcome value mode selected {selected_value} because it has the highest positive edge "
            f"({strategy.get('edge_pp')} pp) versus market midpoint {strategy.get('market_midpoint')}. "
            f"These can differ when the market favorite ({market_favorite}) is priced above the model's fair probability, "
            "leaving buy-YES value on draw or underdog outcomes. "
            f"Decision: {'trade' if strategy.get('should_trade') else 'no trade'}."
        )
    else:
        trade_decision = (
            f"Market midpoint for {outcome} is {market.get(outcome)}; model edge is {strategy['edge_pp']} pp. "
            f"Decision: {'trade' if strategy.get('should_trade') else 'no trade'}."
        )
    return {
        "match_summary": {
            "match": match,
            "home": mapping.get("home_short_code"),
            "away": mapping.get("away_short_code"),
        },
        "evidence_summary": {
            "sportmonks_top": prediction.get("source_tops", {}).get("sportmonks_1x2"),
            "bookmaker_top": prediction.get("source_tops", {}).get("bookmaker_consensus"),
            "priors_top": prediction.get("source_tops", {}).get("supabase_priors"),
            "source_agreement": prediction.get("source_agreement"),
            "data_quality": prediction.get("data_quality"),
        },
        "prediction_choice": (
            f"Selected {outcome} at {prediction['probability']:.2%} from blended independent sources. "
            f"Top-vs-runner-up separation is {prediction.get('prediction_concentration', 0):.2%}."
        ),
        "alternative_rejections": prediction.get("rejected_alternatives", []),
        "trade_decision": trade_decision,
        "value_selection": {
            "strategy_mode": strategy_mode,
            "model_winner": outcome,
            "selected_value_outcome": selected_value,
            "execution_outcome": strategy.get("execution_outcome") or strategy.get("outcome"),
            "execution_selector": strategy.get("execution_selector"),
            "execution_reason": strategy.get("execution_reason"),
            "candidate_type": strategy.get("candidate_type"),
            "ladder_bucket": strategy.get("ladder_bucket"),
            "suggested_size": strategy.get("suggested_size"),
            "value_candidate_preserved": strategy.get("value_candidate_preserved"),
            "hybrid_weights": strategy.get("hybrid_weights"),
            "market_favorite": market_favorite,
            "outcome_edges": all_edges,
            "why_different": (
                "The most likely outcome maximizes model probability, while the value outcome maximizes "
                "model probability minus market midpoint. A less likely outcome can be a better buy-YES value "
                "when the market overprices the favorite."
            ),
        },
        "risk_factors": prediction.get("risk_factors", []),
        "confidence_justification": (
            f"Confidence score {prediction.get('confidence_score')}/100 with "
            f"{prediction.get('source_agreement')}/3 source agreement and data quality {prediction.get('data_quality')}."
        ),
        "recommended_action": strategy.get("recommended_action"),
    }


def new_record(session_id, behavior, **fields):
    record = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "session_id": session_id,
        "record_id": str(uuid.uuid4()),
        "behavior": behavior,
        "client_ts_utc": int(datetime.now(timezone.utc).timestamp() * 1000),
    }
    record.update({key: value for key, value in fields.items() if value is not None})
    return record


def compact_json(payload):
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def build_records(session_id, data, prediction, strategy, model_meta):
    reasoning = structured_reasoning(data, prediction, strategy)
    prediction_trace = build_reasoning_trace(data, prediction, strategy, phase="prediction")
    trade_trace = build_reasoning_trace(data, prediction, strategy, phase="trade_and_edge")
    fixture_id = str((data.get("discovery") or {}).get("fixture_id") or (session_id.split(":")[1] if ":" in session_id else ""))
    ledger_submit_attempted = os.getenv("SUBMIT_LEDGER", "0") == "1"
    rec_observe = new_record(
        session_id,
        "Observing",
        trigger_source="agent_prematch",
        trigger_type="cron_trigger",
        trigger_description=f"Prematch agent run for fixture {fixture_id}",
        trigger_payload_summary=compact_json({"fixture_id": fixture_id, "mode": "dry_run" if os.getenv("DRY_RUN", "1") != "0" else "live"}),
    )
    rec_sportmonks = new_record(
        session_id,
        "ToolCalling",
        upstream_record_id=[rec_observe["record_id"]],
        tool_meta={"source": "stair_arena_proxy", "system": "sportmonks", "endpoint": "/api/v1/data/proxy/sportmonks/v3/football/fixtures/{fixture_id}"},
        description="Fetch Sportmonks fixture detail and pre-match signals.",
        input_payload=compact_json({"fixture_id": fixture_id, "include": "participants;predictions;odds;xGFixture"}),
        output_payload=compact_json(data["sportmonks_digest"]),
        success=bool(data.get("sportmonks_digest")),
    )
    rec_supabase = new_record(
        session_id,
        "ToolCalling",
        upstream_record_id=[rec_observe["record_id"]],
        tool_meta={"source": "supabase", "system": "world_cup_arena", "endpoint": "/rest/v1/ads_*"},
        description="Fetch Supabase country priors for both teams.",
        input_payload=compact_json({"fixture_id": fixture_id, "tables": list((data.get("supabase_digest") or {}).get("tables", {}).keys())}),
        output_payload=compact_json(data["supabase_digest"]),
        success=bool(data.get("supabase_digest")),
    )
    rec_polymarket = new_record(
        session_id,
        "ToolCalling",
        upstream_record_id=[rec_observe["record_id"]],
        tool_meta={"source": "stair_arena_proxy", "system": "polymarket", "endpoint": "/api/v1/data/proxy/polymarket-gamma/events + /api/v1/data/proxy/polymarket-clob/midpoint"},
        description="Fetch Polymarket moneyline markets and YES-token midpoints.",
        input_payload=compact_json({"fixture_id": fixture_id, "event_slug": (data.get("mapping") or {}).get("polymarket_event_slug")}),
        output_payload=compact_json(data["polymarket_digest"]),
        success=bool(data.get("polymarket_digest")),
    )
    rec_predict = new_record(
        session_id,
        "Thinking",
        upstream_record_id=[rec_sportmonks["record_id"], rec_supabase["record_id"]],
        model_invocation=model_meta,
        prompt="Predict the 3-way match outcome using Sportmonks, bookmaker, and Supabase priors only; exclude Polymarket prices.",
        inputs=[
            {"input_record_id": rec_sportmonks["record_id"], "input_payload": compact_json(data["sportmonks_digest"])},
            {"input_record_id": rec_supabase["record_id"], "input_payload": compact_json(data["supabase_digest"])},
        ],
        output_payload=compact_json({
            "prediction": prediction,
            "reasoning_trace": prediction_trace,
            "reasoning": {
                "match_summary": reasoning["match_summary"],
                "evidence_summary": reasoning["evidence_summary"],
                "prediction_choice": reasoning["prediction_choice"],
                "alternative_rejections": reasoning["alternative_rejections"],
                "risk_factors": reasoning["risk_factors"],
                "confidence_justification": reasoning["confidence_justification"],
            },
        }),
    )
    rec_strategy = new_record(
        session_id,
        "Thinking",
        upstream_record_id=[rec_predict["record_id"], rec_polymarket["record_id"]],
        prompt="Compare the prediction against Polymarket prices after prediction is complete and decide whether the trade gate allows an order.",
        inputs=[
            {"input_record_id": rec_predict["record_id"], "input_payload": compact_json(prediction)},
            {"input_record_id": rec_polymarket["record_id"], "input_payload": compact_json(data["polymarket_digest"])},
        ],
        output_payload=compact_json({
            "strategy": strategy,
            "reasoning_trace": trade_trace,
            "reasoning": {
                "trade_decision": reasoning["trade_decision"],
                "value_selection": reasoning["value_selection"],
                "risk_factors": reasoning["risk_factors"],
                "recommended_action": reasoning["recommended_action"],
            },
        }),
    )
    records = [rec_observe, rec_sportmonks, rec_supabase, rec_polymarket, rec_predict, rec_strategy]
    records.append(
        new_record(
            session_id,
            "Acting",
            upstream_record_id=[rec_predict["record_id"]],
            action_type="prediction",
            target_system="arena",
            action_summary=f"Submit prediction for fixture {fixture_id}",
            parameters={
                "fixture_id": fixture_id,
                "outcome": prediction["outcome"],
                "probability": max(0.001, min(0.999, float(prediction["probability"]))),
            },
            dry_run=not ledger_submit_attempted,
            execution_status="confirmed" if ledger_submit_attempted else "simulated",
        )
    )
    if strategy.get("should_trade"):
        records.append(
            new_record(
                session_id,
                "Acting",
                upstream_record_id=[rec_strategy["record_id"]],
                action_type="open_order_intent",
                target_system="arena",
                action_summary=f"Intent to open {strategy['direction']} ${strategy['size_usdc']:.2f} on {strategy['outcome']}",
                parameters=strategy,
                dry_run=os.getenv("DRY_RUN", "1") != "0",
                execution_status="pending",
            )
        )
    return records


TERMINAL_ORDER_STATUSES = {"filled", "rejected", "settled", "refunded", "closed"}


def order_execution_mode():
    mode = os.getenv("ORDER_EXECUTION_MODE", "limit").strip().lower()
    if mode not in {"limit", "fok"}:
        raise ValueError("ORDER_EXECUTION_MODE must be 'limit' or 'fok'")
    return mode


def build_limit_order_payload(fixture_id, strategy):
    order = (strategy.get("orders") or [{}])[0]
    return {
        "fixture_id": str(fixture_id),
        "team_code": str(strategy["outcome"]),
        "side": order.get("side", "buy_yes"),
        "usd_size": f"{float(strategy['size_usdc']):.2f}",
        "limit_price": strategy["limit_price"],
        "time_in_force_seconds": int(os.getenv("ORDER_TIME_IN_FORCE_SECONDS", "30")),
        "idempotency_key": str(uuid.uuid4()),
    }


def build_fok_order_payload(fixture_id, strategy):
    limit_price = float(strategy["limit_price"])
    buffer_pp = float(os.getenv("FOK_PRICE_BUFFER_PP", "1.0"))
    worst_price = min(0.99, limit_price + (buffer_pp / 100.0))
    return {
        "fixture_id": str(fixture_id),
        "team_code": str(strategy["outcome"]),
        "usd_size": f"{float(strategy['size_usdc']):.2f}",
        "worst_price": round(worst_price, 4),
        "idempotency_key": str(uuid.uuid4()),
    }


def build_order_payload(fixture_id, strategy, execution_mode=None):
    mode = execution_mode or order_execution_mode()
    if mode == "fok":
        return build_fok_order_payload(fixture_id, strategy)
    return build_limit_order_payload(fixture_id, strategy)


def get_order_detail(arena_key, order_id):
    return get_json(f"{ARENA}{ARENA_V1}/arena/orders/{order_id}", headers=arena_headers(arena_key), timeout=20)


def extract_order_id(order_response):
    if not isinstance(order_response, dict):
        return None
    for key in ("order_id", "id"):
        if order_response.get(key):
            return order_response[key]
    for key in ("order", "data"):
        value = order_response.get(key)
        if isinstance(value, dict):
            found = extract_order_id(value)
            if found:
                return found
    return None


def summarize_order_status(order_payload):
    if not isinstance(order_payload, dict):
        return {}
    return {
        "order_id": order_payload.get("order_id") or order_payload.get("id"),
        "status": order_payload.get("status"),
        "rejection_reason": order_payload.get("rejection_reason") or order_payload.get("failure_reason") or order_payload.get("error"),
        "usd_size_filled": order_payload.get("usd_size_filled"),
        "open_avg_fill_price": order_payload.get("open_avg_fill_price"),
        "open_fills": order_payload.get("open_fills"),
    }


def poll_order_until_terminal(arena_key, order_id):
    timeout_seconds = float(os.getenv("ORDER_POLL_SECONDS", "30"))
    interval_seconds = float(os.getenv("ORDER_POLL_INTERVAL_SECONDS", "2"))
    deadline = time.monotonic() + timeout_seconds
    attempts = []
    final_detail = None
    while True:
        detail = get_order_detail(arena_key, order_id)
        final_detail = detail
        summary = summarize_order_status(detail)
        attempts.append(summary)
        status = str(summary.get("status") or "").lower()
        if status in TERMINAL_ORDER_STATUSES:
            return {"timed_out": False, "attempts": attempts, "final": detail}
        if time.monotonic() >= deadline:
            return {"timed_out": True, "attempts": attempts, "final": final_detail}
        time.sleep(interval_seconds)


def order_endpoint_for_mode(mode):
    if mode == "fok":
        return f"{ARENA}{ARENA_V1}/arena/orders_fok"
    return f"{ARENA}{ARENA_V1}/arena/orders"


def maybe_submit_order(arena_key, fixture_id, strategy, dry_run, agent_me=None, match_timing=None, no_submit=False):
    if dry_run or not strategy["should_trade"]:
        if strategy.get("should_trade"):
            mode = order_execution_mode()
            payload = build_order_payload(fixture_id, strategy, mode)
            if no_submit:
                print("NO-SUBMIT ORDER PAYLOAD")
                print(f"execution_mode={mode}")
                print(f"endpoint={order_endpoint_for_mode(mode)}")
                print(f"order_payload={json.dumps(payload, sort_keys=True)}")
            return {"submitted": False, "dry_run": True, "execution_mode": mode, "payload": payload}
        return None
    current_window = (match_timing or {}).get("current_window")
    if current_window and current_window not in {"PRE_MATCH", "IN_PLAY"}:
        return {"submitted": False, "blocked": f"match window is {current_window}", "match_timing": match_timing}
    max_trade_usdc = float(os.getenv("MAX_TRADE_USDC", "2"))
    if max_trade_usdc > 1:
        return {"submitted": False, "blocked": "MAX_TRADE_USDC must be <= 1 for production safety"}
    if strategy.get("confidence_level") == "low":
        return {"submitted": False, "blocked": "confidence is low"}
    if strategy.get("strategy_mode") != "qualification_hybrid_9010" and float(strategy.get("edge_pp") or 0) < float(os.getenv("TRADE_EDGE_THRESHOLD_PP", "6")):
        return {"submitted": False, "blocked": "edge below threshold"}

    wallet = (agent_me or get_agent_me(arena_key)).get("wallet") or {}
    available = wallet.get("available_balance_usdc")
    if available is None:
        return {"submitted": False, "blocked": "available balance missing"}
    if float(strategy["size_usdc"]) > float(available):
        return {"submitted": False, "blocked": "size exceeds available balance", "available_balance_usdc": available}

    mode = order_execution_mode()
    payload = build_order_payload(fixture_id, strategy, mode)
    print("NO-SUBMIT ORDER PAYLOAD" if no_submit else "LIVE ORDER WARNING")
    print(f"execution_mode={mode}")
    print(f"fixture_id={fixture_id}")
    print(f"team_code={payload['team_code']}")
    print(f"direction={strategy['direction']}")
    print(f"usd_size={payload['usd_size']}")
    print(f"limit_price={strategy['limit_price']}")
    if mode == "fok":
        print(f"worst_price={payload['worst_price']}")
        print(f"fok_price_buffer_pp={os.getenv('FOK_PRICE_BUFFER_PP', '1.0')}")
    print(f"edge_pp={strategy['edge_pp']}")
    print(f"confidence={strategy.get('confidence_level')}")
    print(f"order_payload={json.dumps(payload, sort_keys=True)}")
    if no_submit:
        return {"submitted": False, "no_submit": True, "execution_mode": mode, "payload": payload}
    if os.getenv("REQUIRE_LIVE_CONFIRM", "1") == "1":
        confirm = input("Type CONFIRM LIVE ORDER to submit: ")
        if confirm != "CONFIRM LIVE ORDER":
            return {"submitted": False, "blocked": "live order confirmation not provided", "execution_mode": mode, "payload": payload}
    response = post_json(order_endpoint_for_mode(mode), headers=arena_headers(arena_key), payload=payload, timeout=20)
    order_id = extract_order_id(response)
    initial_summary = summarize_order_status(response)
    print(f"submitted order_id={order_id or 'unknown'}")
    print(f"initial status={initial_summary.get('status') or 'unknown'}")
    poll = None
    final_summary = initial_summary
    if mode == "fok" and order_id:
        poll = poll_order_until_terminal(arena_key, order_id)
        final_summary = summarize_order_status(poll.get("final") or {})
        print(f"final status={final_summary.get('status') or 'unknown'}")
        print(f"rejection_reason={final_summary.get('rejection_reason')}")
        print(f"usd_size_filled={final_summary.get('usd_size_filled')}")
        print(f"open_avg_fill_price={final_summary.get('open_avg_fill_price')}")
        print(f"open_fills={json.dumps(final_summary.get('open_fills'), sort_keys=True)}")
    return {
        "submitted": True,
        "execution_mode": mode,
        "endpoint": order_endpoint_for_mode(mode),
        "payload": payload,
        "order_id": order_id,
        "initial_status": initial_summary,
        "final_status": final_summary,
        "poll": poll,
        "response": response,
    }


def write_json_output(prefix, payload):
    os.makedirs("output", exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join("output", f"{prefix}_{stamp}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    return out_path


def maybe_validate_ledger(arena_key, payload):
    if os.getenv("VALIDATE_LEDGER", "0") != "1":
        return {"enabled": False}
    endpoint = os.getenv("LEDGER_VALIDATE_ENDPOINT", f"{ARENA_V1}/arena/ledger/records/validate").strip()
    url = endpoint if endpoint.startswith("http") else f"{ARENA}{endpoint}"
    try:
        response = post_json_response(url=url, headers=arena_headers(arena_key), payload=payload, timeout=20)
        body = response.text
        print(f"Ledger validate POST HTTP {response.status_code}")
        if body:
            print(f"Ledger validate response body: {body[:2000]}")
        response.raise_for_status()
        try:
            parsed = response.json()
        except ValueError:
            parsed = body
        return {"enabled": True, "ok": True, "url": url, "http_status": response.status_code, "response": parsed}
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        body = getattr(getattr(exc, "response", None), "text", None)
        if status is None and "response" in locals():
            status = response.status_code
            body = response.text
        print(f"Ledger validate POST failed: HTTP {status if status is not None else 'unknown'}")
        if body:
            print(f"Ledger validate failure body: {body[:2000]}")
        path = write_json_output("ledger_validate_failed", {"payload": payload, "url": url, "error": str(exc), "http_status": status, "response_body": body})
        return {
            "enabled": True,
            "ok": False,
            "url": url,
            "error": str(exc),
            "http_status": status,
            "response_body": body,
            "failed_payload_path": path,
        }


def find_ledger_errors(payload):
    found = []

    def walk(value, path=""):
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else key
                if key == "errors" and child:
                    found.append({"path": child_path, "value": child})
                elif key in {"status", "result"} and isinstance(child, str) and "rejected" in child.lower():
                    found.append({"path": child_path, "value": child})
                elif key in {"ledger_rejected", "rejected"} and child:
                    found.append({"path": child_path, "value": child})
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(payload)
    return found


def maybe_submit_ledger(arena_key, records):
    fixture_id = "unknown"
    if records and records[0].get("session_id"):
        parts = records[0]["session_id"].split(":")
        if len(parts) > 1:
            fixture_id = parts[1]
    endpoint = os.getenv("LEDGER_BATCH_ENDPOINT", f"{ARENA_V1}/arena/ledger/records/batch").strip()
    url = endpoint if endpoint.startswith("http") else f"{ARENA}{endpoint}"
    payload = {"records": records, "fixture_id": str(fixture_id)}
    validation = maybe_validate_ledger(arena_key, payload)
    ledger_timeout = int(os.getenv("LEDGER_TIMEOUT_SECONDS", "60"))

    if os.getenv("SUBMIT_LEDGER", "0") != "1":
        path = write_json_output(f"ledger_trace_{fixture_id}", payload)
        return {
            "submitted": False,
            "reason": "SUBMIT_LEDGER is not 1",
            "url": url,
            "records_count": len(records),
            "local_path": path,
            "validation": validation,
        }
    if not records:
        return {"submitted": False, "reason": "no records", "url": url, "records_count": 0}
    if len(records) > 50:
        path = write_json_output(
            f"ledger_failed_{fixture_id}",
            {"records": records, "fixture_id": str(fixture_id), "error": "ledger batch exceeds 50 records"},
        )
        return {
            "submitted": False,
            "mode": "batch",
            "url": url,
            "records_count": len(records),
            "error": "ledger batch exceeds 50 records",
            "failed_payload_path": path,
        }

    try:
        response = post_json_response(
            url=url,
            headers=arena_headers(arena_key),
            payload=payload,
            timeout=ledger_timeout,
        )
        body = response.text
        print(f"Ledger batch POST HTTP {response.status_code}")
        if body:
            print(f"Ledger response body: {body[:2000]}")
        response.raise_for_status()
        try:
            parsed = response.json()
        except ValueError:
            parsed = body
        ledger_errors = find_ledger_errors(parsed)
        if ledger_errors:
            path = write_json_output(
                f"ledger_failed_{fixture_id}",
                {
                    "endpoint": endpoint,
                    "url": url,
                    "payload": payload,
                    "fixture_id": str(fixture_id),
                    "http_status": response.status_code,
                    "response": parsed,
                    "ledger_errors": ledger_errors,
                },
            )
            return {
                "submitted": False,
                "mode": "batch",
                "url": url,
                "records_count": len(records),
                "http_status": response.status_code,
                "response": parsed,
                "ledger_errors": ledger_errors,
                "failed_payload_path": path,
                "validation": validation,
            }
        return {
            "submitted": True,
            "mode": "batch",
            "url": url,
            "records_count": len(records),
            "http_status": response.status_code,
            "response": parsed,
            "validation": validation,
        }
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        body = getattr(getattr(exc, "response", None), "text", None)
        if status is None and "response" in locals():
            status = response.status_code
            body = response.text
        print(f"Ledger batch POST failed: HTTP {status if status is not None else 'unknown'}")
        if body:
            print(f"Ledger failure body: {body[:2000]}")
        path = write_json_output(
            f"ledger_failed_{fixture_id}",
            {"endpoint": endpoint, "url": url, "records": records, "fixture_id": str(fixture_id), "error": str(exc), "http_status": status, "response_body": body},
        )
        return {
            "submitted": False,
            "mode": "batch",
            "url": url,
            "records_count": len(records),
            "error": str(exc),
            "http_status": status,
            "response_body": body,
            "failed_payload_path": path,
            "validation": validation,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=int, default=None)
    parser.add_argument("--model-variant", choices=sorted(MODEL_VARIANTS), default=os.getenv("MODEL_VARIANT", "baseline"))
    parser.add_argument("--strategy-mode", choices=sorted(STRATEGY_MODES), default=os.getenv("STRATEGY_MODE", "winner_only"))
    parser.add_argument("--no-submit", action="store_true", help="Build and print order payloads without POSTing orders or ledger records.")
    args = parser.parse_args()
    load_dotenv()
    arena_key = os.getenv("ARENA_KEY")
    anthropic_key = os.getenv("ANTHROPIC_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    fixture_id = args.fixture or int(os.getenv("FIXTURE_ID", str(DEFAULT_FIXTURE_ID)))
    dry_run = os.getenv("DRY_RUN", "1") != "0"

    if not arena_key:
        print("ARENA_KEY is missing. Add it to .env first.", file=sys.stderr)
        return 2

    print(f"Running prematch agent for fixture {fixture_id} (dry_run={dry_run}, strategy_mode={args.strategy_mode})")
    match_timing = None
    try:
        timing = get_match_timing(arena_key, fixture_id)
        match_timing = {
            "current_window": timing.get("current_window"),
            "server_ts_utc": timing.get("server_ts_utc"),
            "raw": timing,
        }
        print(f"Match timing: window={match_timing.get('current_window')} server_ts_utc={match_timing.get('server_ts_utc')}")
    except Exception as exc:
        match_timing = {"error": str(exc)}
        print(f"Match timing check failed: {exc}", file=sys.stderr)

    discovery = discover_fixture(arena_key, fixture_id)
    mapping = fetch_mapping(fixture_id)
    fixture, home, away = fetch_fixture_detail(arena_key, fixture_id)
    moneyline = fetch_polymarket(arena_key, mapping["polymarket_event_slug"])

    country_rows = fetch_country_ids(mapping["home_country"], mapping["away_country"])
    country_ids = [row["country_id"] for row in country_rows.values()]
    priors = fetch_supabase_priors(country_ids)

    data = {
        "discovery": discovery,
        "mapping": mapping,
        "match_timing": match_timing,
        "sportmonks_digest": sportmonks_digest(fixture, home, away),
        "polymarket_digest": polymarket_digest(moneyline, mapping),
        "supabase_digest": supabase_digest(priors, country_rows),
    }

    use_openai = os.getenv("USE_OPENAI", "1") == "1"

    if openai_key and use_openai:
        try:
            prediction, model_meta = call_openai(
                openai_key,
                PREDICT_SYS,
                {
                    "fixture": fixture.get("name"),
                    "home_code": mapping["home_short_code"],
                    "away_code": mapping["away_short_code"],
                    "sportmonks_digest": data["sportmonks_digest"],
                    "supabase_digest": data["supabase_digest"],
                },
            )
            strategy = select_strategy(prediction, data["polymarket_digest"], mapping, args.strategy_mode)
            model_meta = {"prediction": model_meta, "strategy": {"provider": "rules", "strategy_mode": args.strategy_mode}}
        except Exception as exc:
            print(f"OpenAI call failed, using fallback reasoning: {exc}", file=sys.stderr)
            prediction = blended_prediction(fixture, mapping, priors, country_rows, args.model_variant)
            strategy = select_strategy(prediction, data["polymarket_digest"], mapping, args.strategy_mode)
            model_meta = {"provider": "fallback", "model_name": "none", "error": str(exc)}
    elif anthropic_key:
        try:
            prediction, model_meta = call_anthropic(
                anthropic_key,
                PREDICT_SYS,
                {
                    "fixture": fixture.get("name"),
                    "home_code": mapping["home_short_code"],
                    "away_code": mapping["away_short_code"],
                    "sportmonks_digest": data["sportmonks_digest"],
                    "supabase_digest": data["supabase_digest"],
                },
            )
            strategy = select_strategy(prediction, data["polymarket_digest"], mapping, args.strategy_mode)
            model_meta = {"prediction": model_meta, "strategy": {"provider": "rules", "strategy_mode": args.strategy_mode}}
        except Exception as exc:
            print(f"Anthropic call failed, using fallback reasoning: {exc}", file=sys.stderr)
            prediction = blended_prediction(fixture, mapping, priors, country_rows, args.model_variant)
            strategy = select_strategy(prediction, data["polymarket_digest"], mapping, args.strategy_mode)
            model_meta = {"provider": "fallback", "model_name": "none", "error": str(exc)}
    else:
        prediction = blended_prediction(fixture, mapping, priors, country_rows, args.model_variant)
        strategy = select_strategy(prediction, data["polymarket_digest"], mapping, args.strategy_mode)
        model_meta = {"provider": "fallback", "model_name": "none"}

    session_id = f"prematch:{fixture_id}:{uuid.uuid4()}"
    records = build_records(session_id, data, prediction, strategy, model_meta)
    agent_me = None
    try:
        agent_me = get_agent_me(arena_key)
    except Exception as exc:
        agent_me = {"error": str(exc)}
    try:
        order_history = get_order_history(arena_key, fixture_id)
    except Exception as exc:
        order_history = {"error": str(exc)}
    order = maybe_submit_order(
        arena_key,
        fixture_id,
        strategy,
        dry_run,
        agent_me=agent_me if "error" not in agent_me else None,
        match_timing=match_timing if "error" not in (match_timing or {}) else None,
        no_submit=args.no_submit,
    )
    ledger = {"submitted": False, "reason": "--no-submit skips ledger submission"} if args.no_submit else maybe_submit_ledger(arena_key, records)

    output = {
        "session_id": session_id,
        "fixture_id": fixture_id,
        "dry_run": dry_run,
        "data": data,
        "prediction": prediction,
        "model_variant": args.model_variant,
        "strategy_mode": args.strategy_mode,
        "strategy": strategy,
        "order": order,
        "ledger": ledger,
        "agent_me": agent_me,
        "order_history": order_history,
        "records": records,
    }
    os.makedirs("output", exist_ok=True)
    out_path = os.path.join("output", f"prematch_{fixture_id}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, sort_keys=True)

    print(json.dumps({"prediction": prediction, "strategy": strategy, "output": out_path}, indent=2))
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
    except Exception as exc:
        print(f"Agent failed: {exc}", file=sys.stderr)
        sys.exit(1)
