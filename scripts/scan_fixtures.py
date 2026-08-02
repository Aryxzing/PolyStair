import json
import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from agent_prematch import (
    ARENA,
    DEFAULT_FIXTURE_ID,
    SPORTMONKS_SEASON_ID,
    SUPABASE,
    arena_headers,
    arena_supabase_headers,
    build_records,
    call_anthropic,
    call_openai,
    fallback_strategy,
    fetch_country_ids,
    fetch_fixture_detail,
    fetch_polymarket,
    fetch_supabase_priors,
    polymarket_digest,
    normalize_strategy,
    blended_prediction,
    sportmonks_digest,
    supabase_digest,
    unwrap_proxy,
    PREDICT_SYS,
    STRATEGY_SYS,
)


def fetch_mapped_fixtures(limit):
    response = requests.get(
        f"{SUPABASE}/rest/v1/data_sportmonks_polymarket_mapping",
        headers=arena_supabase_headers(),
        params={
            "select": "sportmonks_fixture_id,polymarket_event_slug,home_country,home_short_code,away_country,away_short_code,sportmonks_match_name,sportmonks_kickoff_utc",
            "polymarket_event_slug": "not.is.null",
            "order": "sportmonks_kickoff_utc.asc",
            "limit": str(limit),
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def fetch_schedule_count(arena_key):
    response = requests.get(
        f"{ARENA}/api/v1/data/proxy/sportmonks/v3/football/schedules/seasons/{SPORTMONKS_SEASON_ID}",
        headers=arena_headers(arena_key),
        timeout=30,
    )
    response.raise_for_status()
    body = unwrap_proxy(response.json())
    return len(body.get("data", []))


def run_model(openai_key, anthropic_key, system, payload):
    if openai_key and os.getenv("USE_OPENAI", "1") == "1":
        return call_openai(openai_key, system, payload)
    if anthropic_key:
        return call_anthropic(anthropic_key, system, payload)
    raise RuntimeError("No model key configured")


def process_fixture(arena_key, openai_key, anthropic_key, mapping):
    fixture_id = int(mapping["sportmonks_fixture_id"])
    fixture, home, away = fetch_fixture_detail(arena_key, fixture_id)
    moneyline = fetch_polymarket(arena_key, mapping["polymarket_event_slug"])

    country_rows = fetch_country_ids(mapping["home_country"], mapping["away_country"])
    country_ids = [row["country_id"] for row in country_rows.values()]
    priors = fetch_supabase_priors(country_ids)

    data = {
        "mapping": mapping,
        "sportmonks_digest": sportmonks_digest(fixture, home, away),
        "polymarket_digest": polymarket_digest(moneyline, mapping),
        "supabase_digest": supabase_digest(priors, country_rows),
    }

    if not ((openai_key and os.getenv("USE_OPENAI", "1") == "1") or anthropic_key):
        prediction = blended_prediction(fixture, mapping, priors, country_rows)
        strategy = fallback_strategy(prediction, data["polymarket_digest"])
        strategy = normalize_strategy(strategy, prediction, data["polymarket_digest"])
        model_meta = {"provider": "rules", "model_name": "supabase-priors-v1"}
    else:
        try:
            prediction, prediction_meta = run_model(
                openai_key,
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
            strategy, strategy_meta = run_model(
                openai_key,
                anthropic_key,
                STRATEGY_SYS,
                {"prediction": prediction, "polymarket_digest": data["polymarket_digest"]},
            )
            strategy = normalize_strategy(strategy, prediction, data["polymarket_digest"])
            model_meta = {"prediction": prediction_meta, "strategy": strategy_meta}
        except Exception as exc:
            prediction = blended_prediction(fixture, mapping, priors, country_rows)
            strategy = fallback_strategy(prediction, data["polymarket_digest"])
            strategy = normalize_strategy(strategy, prediction, data["polymarket_digest"])
            model_meta = {"provider": "fallback", "model_name": "none", "error": str(exc)}

    session_id = f"prematch:{fixture_id}:scan"
    records = build_records(session_id, data, prediction, strategy, model_meta)

    market_midpoint = data["polymarket_digest"]["implied_win_prob"].get(strategy.get("outcome"))
    summary = {
        "fixture_id": fixture_id,
        "match": mapping.get("sportmonks_match_name") or fixture.get("name"),
        "predicted_outcome": prediction.get("outcome"),
        "bot_probability": prediction.get("probability"),
        "market_midpoint": market_midpoint,
        "edge_pp": strategy.get("edge_pp"),
        "confidence": prediction.get("confidence_level"),
        "confidence_score": prediction.get("confidence_score"),
        "source_agreement": prediction.get("source_agreement"),
        "data_quality": prediction.get("data_quality"),
        "opportunity_class": strategy.get("opportunity_class"),
        "should_trade": strategy.get("should_trade"),
        "no_trade_reason": strategy.get("no_trade_reason"),
        "risk_factors": prediction.get("risk_factors", []),
        "recommended_action": strategy.get("recommended_action"),
    }

    return {
        "fixture_id": fixture_id,
        "match": mapping.get("sportmonks_match_name") or fixture.get("name"),
        "kickoff_utc": mapping.get("sportmonks_kickoff_utc") or fixture.get("starting_at"),
        "prediction": prediction,
        "strategy": strategy,
        "summary": summary,
        "market": data["polymarket_digest"]["implied_win_prob"],
        "records": records,
        "data": data,
    }


def edge_sort_key(result):
    strategy = result["strategy"]
    return float(strategy.get("edge_pp") or 0.0)


def has_complete_market(result):
    market = result.get("market") or {}
    return bool(market) and any(value is not None for value in market.values())


def write_markdown_report(path, report):
    candidates = [r for r in report["ranked"] if r["strategy"].get("should_trade")]
    watchlist = [
        r for r in report["ranked"]
        if not r["strategy"].get("should_trade") and float(r["strategy"].get("edge_pp") or 0) > 0
    ]
    rejected = [r for r in report["ranked"] if float(r["strategy"].get("edge_pp") or 0) <= 0]
    best = report["ranked"][0] if report["ranked"] else None
    lines = [
        "# Stair AI Bot Scan Report",
        "",
        f"Generated: {report['generated_at']}",
        f"Fixtures scanned: {report['fixtures_scanned']}",
        "",
        "## Executive Summary",
        "",
    ]
    if best:
        lines.extend(
            [
                f"Top opportunity: **{best['match']}** ({best['strategy'].get('opportunity_class')}).",
                f"Prediction: **{best['prediction']['outcome']}** at {best['prediction']['probability']:.2%}; "
                f"market midpoint {best['strategy'].get('market_midpoint')}; edge {best['strategy'].get('edge_pp')} pp.",
                f"Recommended action: {best['strategy'].get('recommended_action')}",
                "",
            ]
        )
    else:
        lines.extend(["No ranked fixtures available.", ""])

    lines.extend(
        [
            "## Current Settings",
            "",
            f"- Trade edge threshold: {os.getenv('TRADE_EDGE_THRESHOLD_PP', '6')} pp",
            f"- Max staging trade size: ${os.getenv('MAX_TRADE_USDC', '2')}",
            f"- Dry run: {os.getenv('DRY_RUN', '1')}",
            f"- OpenAI enabled: {os.getenv('USE_OPENAI', '0')}",
            "",
            "## Top Opportunities",
            "",
        ]
    )
    if not candidates:
        lines.append("No opportunities passed the live-trade gate.")
    for result in candidates[:10]:
        prediction = result["prediction"]
        strategy = result["strategy"]
        lines.extend(
            [
                f"- {result['fixture_id']} | {result['match']}",
                f"  Prediction: {prediction['outcome']} at {prediction['probability']:.2%} ({prediction['confidence_level']})",
                f"  Class: {strategy.get('opportunity_class')} | Edge: {strategy['edge_pp']} pp | Size: ${strategy['size_usdc']}",
                f"  Agreement/Data: {prediction.get('source_agreement')}/3 sources, quality {prediction.get('data_quality')}",
                f"  Rationale: {strategy['rationale']}",
            ]
        )

    lines.extend(["", "## Watchlist / Low Confidence Opportunities", ""])
    if not watchlist:
        lines.append("No positive-edge watchlist items.")
    for result in watchlist[:10]:
        prediction = result["prediction"]
        strategy = result["strategy"]
        lines.append(
            f"- {result['fixture_id']} | {result['match']} | {prediction['outcome']} "
            f"{prediction['probability']:.2%} | market {strategy.get('market_midpoint')} | "
            f"edge {strategy['edge_pp']} pp | class={strategy.get('opportunity_class')} | "
            f"confidence={prediction['confidence_level']} ({prediction.get('confidence_score')}/100) | "
            f"reason={strategy.get('no_trade_reason')}"
        )

    lines.extend(["", "## Rejected / No-Trade Opportunities", ""])
    for result in rejected[:10]:
        prediction = result["prediction"]
        strategy = result["strategy"]
        lines.append(
            f"- {result['fixture_id']} | {result['match']} | {prediction['outcome']} "
            f"{prediction['probability']:.2%} | edge {strategy['edge_pp']} pp | reason={strategy.get('no_trade_reason', 'negative edge')}"
        )

    lines.extend(["", "## Methodology", ""])
    lines.extend(
        [
            "- Prediction excludes Polymarket prices.",
            "- Blended probability = 45% Sportmonks 1X2 + 35% bookmaker Fulltime Result consensus + 20% Supabase priors.",
            "- Polymarket midpoint is used only after prediction to compute model-vs-market edge.",
            "- Trade gate requires medium/high confidence, data quality >= 0.7, source agreement >= 2, and edge above threshold.",
        ]
    )

    lines.extend(["", "## Reasoning Highlights", ""])
    for result in report["ranked"][:5]:
        prediction = result["prediction"]
        strategy = result["strategy"]
        lines.append(
            f"- {result['match']}: {prediction['outcome']} {prediction['probability']:.2%}; "
            f"sources {prediction.get('source_tops')}; edge {strategy.get('edge_pp')} pp; "
            f"action: {strategy.get('recommended_action')}"
        )

    lines.extend(["", "## Risk Notes", ""])
    risk_lines = []
    for result in report["ranked"][:10]:
        for risk in result["prediction"].get("risk_factors", []):
            risk_lines.append(f"- {result['match']}: {risk}")
    lines.extend(risk_lines or ["No major risk notes generated."])

    if report["errors"]:
        lines.extend(["", "## Errors", ""])
        for error in report["errors"]:
            lines.append(f"- {error['fixture_id']}: {error['error']}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    load_dotenv()
    arena_key = os.getenv("ARENA_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    anthropic_key = os.getenv("ANTHROPIC_KEY")
    limit = int(os.getenv("SCAN_LIMIT", "5"))

    if not arena_key:
        print("ARENA_KEY is missing. Add it to .env first.", file=sys.stderr)
        return 2

    print(f"Scanning up to {limit} mapped fixtures...")
    schedule_count = fetch_schedule_count(arena_key)
    mappings = fetch_mapped_fixtures(limit)
    if not mappings:
        print(f"No mapped fixtures found; try fixture {DEFAULT_FIXTURE_ID} directly.")
        return 1

    results = []
    for mapping in mappings:
        fixture_id = mapping["sportmonks_fixture_id"]
        print(f"- {fixture_id}: {mapping.get('sportmonks_match_name')}")
        try:
            results.append(process_fixture(arena_key, openai_key, anthropic_key, mapping))
        except Exception as exc:
            results.append({"fixture_id": fixture_id, "error": str(exc), "mapping": mapping})

    ranked = sorted([r for r in results if "strategy" in r and has_complete_market(r)], key=edge_sort_key, reverse=True)
    incomplete_market = [r for r in results if "strategy" in r and not has_complete_market(r)]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schedule_items_seen": schedule_count,
        "fixtures_scanned": len(results),
        "ranked": ranked,
        "top_10": [r.get("summary") for r in ranked[:10]],
        "operator_summary": {
            "top_opportunity": ranked[0].get("summary") if ranked else None,
            "why": ranked[0]["prediction"].get("rationale") if ranked else None,
            "main_risks": ranked[0]["prediction"].get("risk_factors") if ranked else [],
            "recommended_action": ranked[0]["strategy"].get("recommended_action") if ranked else "No ranked opportunities.",
        },
        "incomplete_market": incomplete_market,
        "errors": [r for r in results if "error" in r],
    }

    os.makedirs("output", exist_ok=True)
    out_path = os.path.join("output", "scan_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
    md_path = os.path.join("output", "scan_report.md")
    write_markdown_report(md_path, report)

    print("\nTop opportunities:")
    for result in ranked[:5]:
        strategy = result["strategy"]
        prediction = result["prediction"]
        print(
            f"{result['fixture_id']} | {result['match']} | pred={prediction['outcome']} "
            f"{prediction['probability']:.2f} | edge_pp={strategy.get('edge_pp')} | "
            f"trade={strategy.get('should_trade')}"
        )
    if incomplete_market:
        print("\nSkipped incomplete market data:")
        for result in incomplete_market:
            print(f"{result['fixture_id']} | {result['match']} | market={result.get('market')}")
    print(f"\nWrote {out_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
