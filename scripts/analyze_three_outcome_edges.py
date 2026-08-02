import json
import math
import os
import statistics
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from agent_prematch import (
    SUPABASE,
    arena_supabase_headers,
    blended_prediction,
    fetch_country_ids,
    fetch_fixture_detail,
    fetch_polymarket,
    fetch_supabase_priors,
    polymarket_digest,
)


VARIANTS = ("baseline", "research_602020")
THRESHOLDS = (2, 3, 4, 5, 6)


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


def favorite_type(outcome, market_mids):
    if not market_mids:
        return "unknown"
    return "market_favorite" if outcome == max(market_mids.items(), key=lambda item: item[1])[0] else "non_favorite"


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


def passes_gates(edge_pp, prediction, threshold):
    return (
        edge_pp >= threshold
        and prediction.get("confidence_level") in ("medium", "high")
        and float(prediction.get("data_quality") or 0) >= 0.7
        and int(prediction.get("source_agreement") or 0) >= 2
    )


def summarize_edges(edges):
    positive = [edge for edge in edges if edge["edge_pp"] > 0]
    by_outcome = Counter(edge["outcome_type"] for edge in positive)
    by_favorite = Counter(edge["favorite_type"] for edge in positive)
    by_confidence = Counter(edge["prediction"]["confidence_level"] for edge in positive)
    threshold_counts = {
        str(threshold): {
            "edge_only": sum(1 for edge in edges if edge["edge_pp"] >= threshold),
            "current_prediction_confidence_gates": sum(
                1 for edge in edges if passes_gates(edge["edge_pp"], edge["prediction"], threshold)
            ),
        }
        for threshold in THRESHOLDS
    }
    top = sorted(positive, key=lambda edge: edge["edge_pp"], reverse=True)[:20]
    return {
        "total_outcome_edges": len(edges),
        "positive_outcome_edges": len(positive),
        "positive_by_outcome_type": dict(by_outcome),
        "positive_by_market_favorite_type": dict(by_favorite),
        "positive_by_prediction_confidence": dict(by_confidence),
        "edge_distribution_pp": stats(edge["edge_pp"] for edge in edges),
        "positive_edge_distribution_pp": stats(edge["edge_pp"] for edge in positive),
        "threshold_counts": threshold_counts,
        "top_20_positive_edges": top,
    }


def process_fixture(arena_key, mapping):
    fixture_id = int(mapping["sportmonks_fixture_id"])
    fixture, home, away = fetch_fixture_detail(arena_key, fixture_id)
    moneyline = fetch_polymarket(arena_key, mapping["polymarket_event_slug"])
    market = polymarket_digest(moneyline, mapping)["implied_win_prob"]
    country_rows = fetch_country_ids(mapping["home_country"], mapping["away_country"])
    priors = fetch_supabase_priors([row["country_id"] for row in country_rows.values()])
    outcome_order = [mapping["home_short_code"], "draw", mapping["away_short_code"]]

    rows = []
    for variant in VARIANTS:
        prediction = blended_prediction(fixture, mapping, priors, country_rows, model_variant=variant)
        for outcome in outcome_order:
            model_probability = prediction["probabilities"].get(outcome)
            market_midpoint = market.get(outcome)
            if model_probability is None or market_midpoint is None:
                continue
            rows.append(
                {
                    "fixture_id": fixture_id,
                    "match": mapping.get("sportmonks_match_name"),
                    "variant": variant,
                    "outcome": outcome,
                    "outcome_type": outcome_type(outcome, mapping),
                    "favorite_type": favorite_type(outcome, market),
                    "model_probability": round(float(model_probability), 4),
                    "market_midpoint": round(float(market_midpoint), 4),
                    "edge_pp": round((float(model_probability) - float(market_midpoint)) * 100, 2),
                    "is_predicted_outcome": outcome == prediction["outcome"],
                    "prediction": {
                        "outcome": prediction["outcome"],
                        "confidence_score": prediction["confidence_score"],
                        "confidence_level": prediction["confidence_level"],
                        "data_quality": prediction["data_quality"],
                        "source_agreement": prediction["source_agreement"],
                    },
                }
            )
    return rows


def write_markdown(path, report):
    lines = [
        "# Three-Outcome Edge Report",
        "",
        f"Generated: {report['generated_at']}",
        f"Fixtures processed: {report['fixtures_processed']}",
        f"Errors: {len(report['errors'])}",
        "",
    ]
    for variant, summary in report["variants"].items():
        lines.extend(
            [
                f"## {variant}",
                "",
                f"- Total outcome edges: {summary['total_outcome_edges']}",
                f"- Positive outcome edges: {summary['positive_outcome_edges']}",
                f"- Positive by outcome type: `{summary['positive_by_outcome_type']}`",
                f"- Positive by market favorite type: `{summary['positive_by_market_favorite_type']}`",
                f"- Positive by prediction confidence: `{summary['positive_by_prediction_confidence']}`",
                f"- Edge distribution: `{summary['edge_distribution_pp']}`",
                f"- Positive edge distribution: `{summary['positive_edge_distribution_pp']}`",
                "",
                "Threshold counts:",
            ]
        )
        for threshold, counts in summary["threshold_counts"].items():
            lines.append(
                f"- >= {threshold} pp: edge-only {counts['edge_only']}, "
                f"current prediction confidence gates {counts['current_prediction_confidence_gates']}"
            )
        lines.extend(["", "Top positive outcome-edges:"])
        for edge in summary["top_20_positive_edges"]:
            lines.append(
                f"- {edge['fixture_id']} {edge['match']}: {edge['outcome']} "
                f"({edge['outcome_type']}, {edge['favorite_type']}) "
                f"model={edge['model_probability']:.3f}, market={edge['market_midpoint']:.3f}, "
                f"edge={edge['edge_pp']:+.2f} pp, pred={edge['prediction']['outcome']}, "
                f"conf={edge['prediction']['confidence_score']} {edge['prediction']['confidence_level']}"
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
    print(f"Analyzing all three outcomes across {len(mappings)} mapped fixtures...")
    rows = {variant: [] for variant in VARIANTS}
    errors = []
    workers = int(os.getenv("COMPARE_WORKERS", "8"))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_fixture, arena_key, mapping): mapping for mapping in mappings}
        for index, future in enumerate(as_completed(futures), start=1):
            mapping = futures[future]
            try:
                for row in future.result():
                    rows[row["variant"]].append(row)
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

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixtures_available": len(mappings),
        "fixtures_processed": len(mappings) - len(errors),
        "errors": errors,
        "variants": {variant: summarize_edges(rows[variant]) for variant in VARIANTS},
    }

    os.makedirs("output", exist_ok=True)
    json_path = os.path.join("output", "three_outcome_edge_report.json")
    md_path = os.path.join("output", "three_outcome_edge_report.md")
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
                        "positive_outcome_edges": summary["positive_outcome_edges"],
                        "positive_by_outcome_type": summary["positive_by_outcome_type"],
                        "positive_by_market_favorite_type": summary["positive_by_market_favorite_type"],
                        "positive_by_prediction_confidence": summary["positive_by_prediction_confidence"],
                        "threshold_counts": summary["threshold_counts"],
                    }
                    for variant, summary in report["variants"].items()
                },
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
