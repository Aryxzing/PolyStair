import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_prematch import build_fok_order_payload


def validate_payload(payload):
    errors = []
    if not isinstance(payload.get("fixture_id"), str):
        errors.append("fixture_id must be a string")
    if not isinstance(payload.get("team_code"), str):
        errors.append("team_code must be a string")
    if not isinstance(payload.get("usd_size"), str):
        errors.append("usd_size must be a decimal string")
    else:
        try:
            float(payload["usd_size"])
        except ValueError:
            errors.append("usd_size must parse as a decimal")
    if not isinstance(payload.get("worst_price"), (int, float)):
        errors.append("worst_price must be a number")
    elif not (0 < float(payload["worst_price"]) < 1):
        errors.append("worst_price must be between 0 and 1")
    if not isinstance(payload.get("idempotency_key"), str) or not payload["idempotency_key"]:
        errors.append("idempotency_key must be a non-empty string")
    return errors


def main():
    os.environ.setdefault("FOK_PRICE_BUFFER_PP", "1.0")
    fixture_id = os.getenv("TEST_FOK_FIXTURE_ID", "19609127")
    strategy = {
        "outcome": os.getenv("TEST_FOK_TEAM_CODE", "ZAF"),
        "size_usdc": float(os.getenv("TEST_FOK_USD_SIZE", "1.00")),
        "limit_price": float(os.getenv("TEST_FOK_LIMIT_PRICE", "0.105")),
    }
    payload = build_fok_order_payload(fixture_id, strategy)
    errors = validate_payload(payload)
    print(json.dumps({"payload": payload, "valid": not errors, "errors": errors}, indent=2, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
