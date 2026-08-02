"""Build deterministic, source-cited reasoning payloads for Arena ledger records."""

from __future__ import annotations

from typing import Any


FRAMEWORK_VERSION = "reasoning_score_v1"
FRAMEWORK_STEPS = [
    "Estimate outcome probabilities using fixed source weights.",
    "Compare model probability to market midpoint only after prediction is complete.",
    "Apply confidence/source agreement/data quality gates.",
    "Size position using bankroll guardrails.",
    "Reject if edge or confidence is insufficient.",
]


def _market_prices(data: dict[str, Any]) -> dict[str, Any]:
    return dict((data.get("polymarket_digest") or {}).get("implied_win_prob") or {})


def _source_claim(source_name: str, label: str, values: Any, top: Any) -> dict[str, Any]:
    values_path = f"data_inputs.{source_name}"
    top_path = f"source_tops.{source_name}"
    return {
        "claim": f"{label} selected {top} as its top outcome.",
        "source_fields": [values_path, top_path],
        "values": {values_path: values, top_path: top},
    }


def build_reasoning_trace(
    data: dict[str, Any],
    prediction: dict[str, Any],
    strategy: dict[str, Any],
    *,
    phase: str,
) -> dict[str, Any]:
    """Return a scoring-ready trace using only values already computed by the agent."""
    mapping = data.get("mapping") or {}
    sportmonks = data.get("sportmonks_digest") or {}
    components = prediction.get("components") or {}
    source_tops = prediction.get("source_tops") or {}
    probabilities = prediction.get("probabilities") or {}
    selected = prediction.get("outcome")
    market_prices = _market_prices(data)
    selected_trade = strategy.get("execution_outcome") or strategy.get("outcome")
    market_midpoint = strategy.get("market_midpoint")

    claims = [
        _source_claim(
            "sportmonks_1x2",
            "Sportmonks 1X2",
            components.get("sportmonks_1x2"),
            source_tops.get("sportmonks_1x2"),
        ),
        _source_claim(
            "bookmaker_consensus",
            "Bookmaker consensus",
            components.get("bookmaker_consensus"),
            source_tops.get("bookmaker_consensus"),
        ),
        _source_claim(
            "supabase_priors",
            "Supabase priors",
            components.get("supabase_priors"),
            source_tops.get("supabase_priors"),
        ),
        {
            "claim": f"The fixed blend selected {selected} at probability {prediction.get('probability')}.",
            "source_fields": ["prediction.selected_outcome", "prediction.probability", "prediction.probabilities"],
            "values": {
                "prediction.selected_outcome": selected,
                "prediction.probability": prediction.get("probability"),
                "prediction.probabilities": probabilities,
            },
        },
        {
            "claim": (
                f"Confidence is {prediction.get('confidence_score')}/100 with source agreement "
                f"{prediction.get('source_agreement')}/3 and data quality {prediction.get('data_quality')}."
            ),
            "source_fields": [
                "quality.confidence_score",
                "quality.source_agreement",
                "quality.data_quality",
            ],
            "values": {
                "quality.confidence_score": prediction.get("confidence_score"),
                "quality.source_agreement": prediction.get("source_agreement"),
                "quality.data_quality": prediction.get("data_quality"),
            },
        },
    ]
    if market_midpoint is not None:
        claims.append(
            {
                "claim": (
                    f"The selected trade {selected_trade} has model-minus-market edge "
                    f"{strategy.get('edge_pp')} pp at market midpoint {market_midpoint}."
                ),
                "source_fields": [
                    "trade_decision.selected_trade",
                    "trade_decision.edge_pp",
                    "trade_decision.market_midpoint",
                ],
                "values": {
                    "trade_decision.selected_trade": selected_trade,
                    "trade_decision.edge_pp": strategy.get("edge_pp"),
                    "trade_decision.market_midpoint": market_midpoint,
                },
            }
        )

    return {
        "framework_version": FRAMEWORK_VERSION,
        "reasoning_phase": phase,
        "match_summary": {
            "fixture_id": str((data.get("discovery") or {}).get("fixture_id") or ""),
            "match": sportmonks.get("fixture") or (data.get("polymarket_digest") or {}).get("fixture"),
            "home": mapping.get("home_short_code"),
            "away": mapping.get("away_short_code"),
        },
        "data_inputs": {
            "bookmaker_consensus": components.get("bookmaker_consensus"),
            "sportmonks_1x2": components.get("sportmonks_1x2"),
            "supabase_priors": components.get("supabase_priors"),
            "market_prices": market_prices,
            "market_excluded_from_prediction": True,
        },
        "source_tops": {
            "bookmaker_consensus": source_tops.get("bookmaker_consensus"),
            "sportmonks_1x2": source_tops.get("sportmonks_1x2"),
            "supabase_priors": source_tops.get("supabase_priors"),
        },
        "prediction": {
            "selected_outcome": selected,
            "probability": prediction.get("probability"),
            "probabilities": probabilities,
            "rejected_alternatives": prediction.get("rejected_alternatives") or [],
        },
        "quality": {
            "confidence_score": prediction.get("confidence_score"),
            "confidence_level": prediction.get("confidence_level"),
            "source_agreement": prediction.get("source_agreement"),
            "data_quality": prediction.get("data_quality"),
            "component_disagreement": prediction.get("component_disagreement"),
        },
        "grounded_claims": claims,
        "decision_framework": {
            "steps": list(FRAMEWORK_STEPS),
            "weights": dict(prediction.get("model_weights") or {}),
        },
        "trade_decision": {
            "should_trade": bool(strategy.get("should_trade")),
            "selected_trade": selected_trade,
            "edge_pp": strategy.get("edge_pp"),
            "market_midpoint": market_midpoint,
            "market_prices": market_prices,
            "size_usdc": strategy.get("size_usdc"),
            "no_trade_reason": strategy.get("no_trade_reason"),
            "strategy_mode": strategy.get("strategy_mode"),
        },
        "risk_factors": list(prediction.get("risk_factors") or []),
        "consistency_note": "Same fixed framework applied across matches.",
    }


def resolve_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def check_grounded_claims(payload: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    claims = payload.get("grounded_claims") or []
    for index, claim in enumerate(claims, 1):
        values = claim.get("values") if isinstance(claim, dict) else {}
        for path in claim.get("source_fields") or []:
            actual = resolve_path(payload, path)
            cited = values.get(path) if isinstance(values, dict) else None
            if actual != cited:
                problems.append(f"claim {index}: cited value for {path} does not match payload")
    return problems
