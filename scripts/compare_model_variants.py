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
    MODEL_VARIANTS,
    SUPABASE,
    arena_headers,
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


def passes_current_gates(row, threshold):
    prediction = row["prediction"]
    strategy = row["strategy"]
    return (
        float(strategy.get("edge_pp") or 0) >= threshold
        and prediction.get("confidence_level") in ("medium", "high")
        and float(prediction.get("data_quality") or 0) >= 0.7
        and int(prediction.get("source_agreement") or 0) >= 2
    )


def summarize_variant(rows):
    edges = [float(row["strategy"].get("edge_pp") or 0) for row in rows]
    confidences = [float(row["prediction"].get("confidence_score") or 0) for row in rows]
    threshold_counts = {
        str(threshold): {
            "edge_only": sum(1 for row in rows if float(row["strategy"].get("edge_pp") or 0) >= threshold),
            "current_gates": sum(1 for row in rows if passes_current_gates(row, threshold)),
        }
        for threshold in THRESHOLDS
    }
    top = sorted(rows, key=lambda row: float(row["strategy"].get("edge_pp") or 0), reverse=True)[:12]
    return {
        "fixtures": len(rows),
        "positive_edge_count": sum(1 for edge in edges if edge > 0),
        "negative_edge_count": sum(1 for edge in edges if edge < 0),
        "edge_distribution_pp": stats(edges),
        "confidence_distribution": stats(confidences),
        "confidence_levels": dict(Counter(row["prediction"].get("confidence_level") for row in rows)),
        "threshold_pass_counts": threshold_counts,
        "top_opportunities": [
            {
                "fixture_id": row["fixture_id"],
                "match": row["match"],
                "outcome": row["prediction"]["outcome"],
                "probability": row["prediction"]["probability"],
                "market_probability": row["strategy"].get("market_midpoint"),
                "edge_pp": row["strategy"].get("edge_pp"),
                "confidence_score": row["prediction"].get("confidence_score"),
                "confidence_level": row["prediction"].get("confidence_level"),
                "source_agreement": row["prediction"].get("source_agreement"),
                "opportunity_class": row["strategy"].get("opportunity_class"),
                "should_trade": row["strategy"].get("should_trade"),
            }
            for row in top
        ],
    }


def process_fixture(arena_key, mapping):
    fixture_id = int(mapping["sportmonks_fixture_id"])
    fixture, home, away = fetch_fixture_detail(arena_key, fixture_id)
    moneyline = fetch_polymarket(arena_key, mapping["polymarket_event_slug"])
    country_rows = fetch_country_ids(mapping["home_country"], mapping["away_country"])
    priors = fetch_supabase_priors([row["country_id"] for row in country_rows.values()])
    poly_digest = polymarket_digest(moneyline, mapping)

    rows = []
    for variant in VARIANTS:
        prediction = blended_prediction(fixture, mapping, priors, country_rows, model_variant=variant)
        strategy = normalize_strategy(fallback_strategy(prediction, poly_digest), prediction, poly_digest)
        rows.append(
            {
                "fixture_id": fixture_id,
                "match": mapping.get("sportmonks_match_name"),
                "variant": variant,
                "prediction": prediction,
                "strategy": strategy,
            }
        )
    return rows


def write_markdown(path, report):
    lines = [
        "# Model Variant Comparison",
        "",
        f"Generated: {report['generated_at']}",
        f"Fixtures processed: {report['fixtures_processed']}",
        f"Errors: {len(report['errors'])}",
        "",
        "## Variants",
        "",
    ]
    for name in VARIANTS:
        lines.append(f"- `{name}`: {MODEL_VARIANTS[name]}")

    lines.extend(["", "## Summary", ""])
    for name, summary in report["variants"].items():
        lines.extend(
            [
                f"### {name}",
                f"- Positive edges: {summary['positive_edge_count']}",
                f"- Edge distribution: `{summary['edge_distribution_pp']}`",
                f"- Confidence distribution: `{summary['confidence_distribution']}`",
                f"- Confidence levels: `{summary['confidence_levels']}`",
                "- Threshold pass counts:",
            ]
        )
        for threshold, counts in summary["threshold_pass_counts"].items():
            lines.append(f"  - >= {threshold} pp: edge-only {counts['edge_only']}, current gates {counts['current_gates']}")
        lines.extend(["", "Top opportunities:"])
        for row in summary["top_opportunities"][:8]:
            lines.append(
                f"- {row['fixture_id']} {row['match']}: {row['outcome']} "
                f"p={row['probability']:.3f}, market={row['market_probability']:.3f}, "
                f"edge={row['edge_pp']:+.2f} pp, conf={row['confidence_score']}"
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
    print(f"Comparing variants across {len(mappings)} mapped fixtures...")
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
        "fixtures_processed": max(len(value) for value in rows.values()) if rows else 0,
        "errors": errors,
        "variants": {variant: summarize_variant(rows[variant]) for variant in VARIANTS},
    }

    os.makedirs("output", exist_ok=True)
    json_path = os.path.join("output", "model_variant_comparison.json")
    md_path = os.path.join("output", "model_variant_comparison.md")
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
                    name: {
                        "positive_edge_count": summary["positive_edge_count"],
                        "edge_distribution_pp": summary["edge_distribution_pp"],
                        "confidence_distribution": summary["confidence_distribution"],
                        "threshold_pass_counts": summary["threshold_pass_counts"],
                    }
                    for name, summary in report["variants"].items()
                },
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
