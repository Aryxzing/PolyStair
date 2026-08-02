import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from agent_prematch import (
    SUPABASE,
    arena_supabase_headers,
    blended_prediction,
    fallback_strategy,
    fetch_country_ids,
    fetch_fixture_detail,
    fetch_polymarket,
    fetch_supabase_priors,
    normalize_strategy,
    polymarket_digest,
)


VARIANTS = ("baseline", "research_602020")
THRESHOLD_PP = 6.0


def fetch_mapped_fixtures():
    response = requests.get(
        f"{SUPABASE}/rest/v1/data_sportmonks_polymarket_mapping",
        headers=arena_supabase_headers(),
        params={
            "select": (
                "sportmonks_fixture_id,polymarket_event_slug,home_country,home_short_code,"
                "away_country,away_short_code,sportmonks_match_name,sportmonks_kickoff_utc"
            ),
            "polymarket_event_slug": "not.is.null",
            "order": "sportmonks_kickoff_utc.asc",
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def outcome_type(outcome, mapping):
    if outcome == "draw":
        return "draw"
    if outcome == mapping["home_short_code"]:
        return "home"
    if outcome == mapping["away_short_code"]:
        return "away"
    return "unknown"


def stats(values):
    values = sorted(float(value) for value in values if value is not None)
    if not values:
        return {"count": 0}

    def percentile(q):
        if len(values) == 1:
            return values[0]
        pos = (len(values) - 1) * q
        low = math.floor(pos)
        high = math.ceil(pos)
        if low == high:
            return values[low]
        return values[low] * (high - pos) + values[high] * (pos - low)

    return {
        "count": len(values),
        "min": round(values[0], 3),
        "p25": round(percentile(0.25), 3),
        "median": round(percentile(0.5), 3),
        "mean": round(statistics.mean(values), 3),
        "p75": round(percentile(0.75), 3),
        "max": round(values[-1], 3),
    }


def passes_gates(edge_pp, prediction):
    return (
        edge_pp >= THRESHOLD_PP
        and prediction.get("confidence_level") in ("medium", "high")
        and float(prediction.get("data_quality") or 0) >= 0.7
        and int(prediction.get("source_agreement") or 0) >= 2
    )


def process_fixture(arena_key, mapping):
    fixture_id = int(mapping["sportmonks_fixture_id"])
    fixture, _, _ = fetch_fixture_detail(arena_key, fixture_id)
    moneyline = fetch_polymarket(arena_key, mapping["polymarket_event_slug"])
    market = polymarket_digest(moneyline, mapping)["implied_win_prob"]
    country_rows = fetch_country_ids(mapping["home_country"], mapping["away_country"])
    priors = fetch_supabase_priors([row["country_id"] for row in country_rows.values()])
    outcomes = [mapping["home_short_code"], "draw", mapping["away_short_code"]]

    rows = []
    for variant in VARIANTS:
        prediction = blended_prediction(fixture, mapping, priors, country_rows, model_variant=variant)
        winner_strategy = normalize_strategy(fallback_strategy(prediction, {"implied_win_prob": market}), prediction, {"implied_win_prob": market})
        rows.append(
            {
                "strategy_name": "winner_only",
                "variant": variant,
                "fixture_id": fixture_id,
                "match": mapping["sportmonks_match_name"],
                "outcome": prediction["outcome"],
                "outcome_type": outcome_type(prediction["outcome"], mapping),
                "model_probability": prediction["probability"],
                "market_midpoint": winner_strategy.get("market_midpoint"),
                "edge_pp": winner_strategy.get("edge_pp"),
                "qualifies": passes_gates(float(winner_strategy.get("edge_pp") or 0), prediction),
                "prediction": prediction,
            }
        )

        for outcome in outcomes:
            model_probability = prediction["probabilities"].get(outcome)
            market_midpoint = market.get(outcome)
            if model_probability is None or market_midpoint is None:
                continue
            edge_pp = round((float(model_probability) - float(market_midpoint)) * 100, 2)
            rows.append(
                {
                    "strategy_name": "three_outcome_value",
                    "variant": variant,
                    "fixture_id": fixture_id,
                    "match": mapping["sportmonks_match_name"],
                    "outcome": outcome,
                    "outcome_type": outcome_type(outcome, mapping),
                    "model_probability": round(float(model_probability), 4),
                    "market_midpoint": round(float(market_midpoint), 4),
                    "edge_pp": edge_pp,
                    "qualifies": passes_gates(edge_pp, prediction),
                    "is_predicted_outcome": outcome == prediction["outcome"],
                    "prediction": prediction,
                }
            )
    return rows


def summarize(rows):
    qualifying = [row for row in rows if row["qualifies"]]
    by_fixture = defaultdict(list)
    for row in qualifying:
        by_fixture[row["fixture_id"]].append(row)
    best_per_fixture = [max(values, key=lambda row: row["edge_pp"]) for values in by_fixture.values()]
    ranked = sorted(qualifying, key=lambda row: row["edge_pp"], reverse=True)

    return {
        "opportunities": len(qualifying),
        "expected_trade_count_all_qualifying": len(qualifying),
        "expected_trade_count_one_per_fixture": len(best_per_fixture),
        "edge_distribution_pp": stats(row["edge_pp"] for row in qualifying),
        "confidence_distribution": stats(row["prediction"].get("confidence_score") for row in qualifying),
        "confidence_levels": dict(Counter(row["prediction"].get("confidence_level") for row in qualifying)),
        "outcome_type_distribution": dict(Counter(row["outcome_type"] for row in qualifying)),
        "predicted_outcome_distribution": dict(Counter(row.get("is_predicted_outcome", True) for row in qualifying)),
        "top_50_opportunities": [
            {
                "fixture_id": row["fixture_id"],
                "match": row["match"],
                "outcome": row["outcome"],
                "outcome_type": row["outcome_type"],
                "model_probability": row["model_probability"],
                "market_midpoint": row["market_midpoint"],
                "edge_pp": row["edge_pp"],
                "is_predicted_outcome": row.get("is_predicted_outcome", True),
                "prediction_outcome": row["prediction"].get("outcome"),
                "confidence_score": row["prediction"].get("confidence_score"),
                "confidence_level": row["prediction"].get("confidence_level"),
                "source_agreement": row["prediction"].get("source_agreement"),
            }
            for row in ranked[:50]
        ],
    }


def enough_tournament_trades(summary):
    return summary["expected_trade_count_one_per_fixture"] > 10


def write_markdown(path, report):
    lines = [
        "# Research Strategy: three_outcome_value",
        "",
        f"Generated: {report['generated_at']}",
        f"Fixtures processed: {report['fixtures_processed']}",
        f"Threshold: {THRESHOLD_PP:.1f} pp",
        f"Errors: {len(report['errors'])}",
        "",
    ]
    for variant, strategies in report["variants"].items():
        lines.extend([f"## {variant}", ""])
        for strategy_name, summary in strategies.items():
            lines.extend(
                [
                    f"### {strategy_name}",
                    f"- Opportunities: {summary['opportunities']}",
                    f"- Expected trade count, all qualifying outcomes: {summary['expected_trade_count_all_qualifying']}",
                    f"- Expected trade count, one per fixture: {summary['expected_trade_count_one_per_fixture']}",
                    f"- Enough tournament trades >10: {summary['enough_tournament_trades']}",
                    f"- Edge distribution: `{summary['edge_distribution_pp']}`",
                    f"- Confidence distribution: `{summary['confidence_distribution']}`",
                    f"- Confidence levels: `{summary['confidence_levels']}`",
                    f"- Outcome types: `{summary['outcome_type_distribution']}`",
                    "",
                    "Top opportunities:",
                ]
            )
            for row in summary["top_50_opportunities"][:20]:
                lines.append(
                    f"- {row['fixture_id']} {row['match']}: {row['outcome']} ({row['outcome_type']}) "
                    f"model={row['model_probability']:.3f}, market={row['market_midpoint']:.3f}, "
                    f"edge={row['edge_pp']:+.2f} pp, predicted={row['prediction_outcome']}, "
                    f"conf={row['confidence_score']} {row['confidence_level']}, source_agreement={row['source_agreement']}"
                )
            lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def main():
    load_dotenv()
    os.environ["USE_OPENAI"] = "0"
    os.environ["DRY_RUN"] = "1"
    arena_key = os.getenv("ARENA_KEY")
    if not arena_key:
        print("ARENA_KEY is missing.", file=sys.stderr)
        return 2

    mappings = fetch_mapped_fixtures()
    print(f"Researching winner_only vs three_outcome_value across {len(mappings)} fixtures...")
    rows = {variant: {"winner_only": [], "three_outcome_value": []} for variant in VARIANTS}
    errors = []
    workers = int(os.getenv("COMPARE_WORKERS", "8"))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_fixture, arena_key, mapping): mapping for mapping in mappings}
        for index, future in enumerate(as_completed(futures), start=1):
            mapping = futures[future]
            try:
                for row in future.result():
                    rows[row["variant"]][row["strategy_name"]].append(row)
                if index % 12 == 0 or index == len(futures):
                    print(f"Processed {index}/{len(futures)}")
            except Exception as exc:
                errors.append(
                    {
                        "fixture_id": mapping.get("sportmonks_fixture_id"),
                        "match": mapping.get("sportmonks_match_name"),
                        "error": str(exc),
                    }
                )

    variants = {}
    for variant, strategy_rows in rows.items():
        variants[variant] = {}
        for strategy_name, values in strategy_rows.items():
            summary = summarize(values)
            summary["enough_tournament_trades"] = enough_tournament_trades(summary)
            variants[variant][strategy_name] = summary

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixtures_available": len(mappings),
        "fixtures_processed": len(mappings) - len(errors),
        "errors": errors,
        "variants": variants,
    }

    os.makedirs("output", exist_ok=True)
    json_path = os.path.join("output", "three_outcome_value_strategy_report.json")
    md_path = os.path.join("output", "three_outcome_value_strategy_report.md")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
    write_markdown(md_path, report)

    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print(
        json.dumps(
            {
                "fixtures_processed": report["fixtures_processed"],
                "errors": len(errors),
                "variants": {
                    variant: {
                        strategy: {
                            "opportunities": summary["opportunities"],
                            "one_per_fixture": summary["expected_trade_count_one_per_fixture"],
                            "enough_tournament_trades": summary["enough_tournament_trades"],
                            "outcome_type_distribution": summary["outcome_type_distribution"],
                            "confidence_levels": summary["confidence_levels"],
                        }
                        for strategy, summary in strategies.items()
                    }
                    for variant, strategies in variants.items()
                },
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
