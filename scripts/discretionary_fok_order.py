import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv


ARENA_V1 = "/api/v1"
FILLED_STATUSES = {"completed", "filled", "settled", "closed"}
TERMINAL_STATUSES = {"completed", "filled", "settled", "closed", "rejected", "unfilled", "refunded"}


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def arena_headers(arena_key):
    return {"x-api-key": arena_key}


def get_json(url, *, headers, params=None, timeout=20):
    response = requests.get(url, headers=headers, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def post_json(url, *, headers, payload, timeout=20):
    response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


def fetch_orders(arena_base_url, arena_key):
    return get_json(
        f"{arena_base_url}{ARENA_V1}/arena/orders",
        headers=arena_headers(arena_key),
        timeout=20,
    ).get("orders") or []


def order_is_filled(order):
    return (
        str(order.get("status") or "").lower() in FILLED_STATUSES
        and safe_float(order.get("usd_size_filled")) > 0
    )


def already_filled_fixture(orders, fixture_id):
    return [
        order for order in orders
        if str(order.get("fixture_id")) == str(fixture_id) and order_is_filled(order)
    ]


def extract_order_id(response):
    if not isinstance(response, dict):
        return None
    for key in ("order_id", "id"):
        if response.get(key):
            return response[key]
    for key in ("order", "data"):
        nested = response.get(key)
        if isinstance(nested, dict):
            found = extract_order_id(nested)
            if found:
                return found
    return None


def summarize_order(payload, fallback=None):
    fallback = fallback or {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "order_id": payload.get("order_id") or payload.get("id") or fallback.get("order_id"),
        "status": payload.get("status") or fallback.get("status"),
        "fixture_id": payload.get("fixture_id") or fallback.get("fixture_id"),
        "team_code": payload.get("team_code") or fallback.get("team_code"),
        "usd_size_filled": payload.get("usd_size_filled") or fallback.get("usd_size_filled"),
        "open_avg_fill_price": payload.get("open_avg_fill_price") or fallback.get("open_avg_fill_price"),
        "rejection_reason": (
            payload.get("rejection_reason")
            or payload.get("failure_reason")
            or payload.get("error")
            or fallback.get("rejection_reason")
        ),
    }


def poll_order(arena_base_url, arena_key, order_id):
    deadline = time.monotonic() + 30
    final = {}
    while True:
        detail = get_json(
            f"{arena_base_url}{ARENA_V1}/arena/orders/{order_id}",
            headers=arena_headers(arena_key),
            timeout=20,
        )
        final = detail
        status = str((summarize_order(detail).get("status") or "")).lower()
        if status in TERMINAL_STATUSES:
            return final
        if time.monotonic() >= deadline:
            return final
        time.sleep(2)


def build_payload(args):
    usd = safe_float(args.usd, None)
    worst_price = safe_float(args.worst_price, None)
    if usd is None or usd <= 0:
        raise ValueError("--usd must be a positive number")
    if worst_price is None or worst_price <= 0 or worst_price >= 1:
        raise ValueError("--worst-price must be between 0 and 1")
    return {
        "fixture_id": str(args.fixture),
        "team_code": str(args.team),
        "usd_size": f"{usd:.2f}",
        "worst_price": round(worst_price, 4),
        "idempotency_key": str(uuid.uuid4()),
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Place one manual Arena FOK order with duplicate-fill protection.")
    parser.add_argument("--fixture", required=True, help="Arena/Sportmonks fixture_id.")
    parser.add_argument("--team", required=True, help="Team code or draw.")
    parser.add_argument("--usd", required=True, help="USDC stake size.")
    parser.add_argument("--worst-price", required=True, help="FOK worst price cap.")
    parser.add_argument("--reason", required=True, help="Human reason for the discretionary order.")
    parser.add_argument("--dry-run", action="store_true", help="Print payload only. Do not submit.")
    return parser.parse_args()


def main():
    load_dotenv(Path(".env"))
    args = parse_args()
    arena_base_url = os.getenv("ARENA_BASE_URL", "https://stair-ai.com").rstrip("/")
    arena_key = os.getenv("ARENA_KEY")
    if not arena_key:
        print("ARENA_KEY is missing.", file=sys.stderr)
        return 2

    print("DISCRETIONARY LIVE FOK ORDER")
    print("This is not part of qualification ladder.")
    print(f"reason={args.reason}")

    payload = build_payload(args)
    orders = fetch_orders(arena_base_url, arena_key)
    filled = already_filled_fixture(orders, payload["fixture_id"])
    if filled:
        print(f"Fixture {payload['fixture_id']} already has positive filled/completed order history.")
        print(json.dumps([summarize_order(order) for order in filled], indent=2, sort_keys=True))
        print("ALREADY_FILLED_ABORT")
        return 0

    if args.dry_run:
        print("DRY_RUN_ONLY")
        print(json.dumps({"endpoint": f"{arena_base_url}{ARENA_V1}/arena/orders_fok", "payload": payload}, indent=2, sort_keys=True))
        return 0

    response = post_json(
        f"{arena_base_url}{ARENA_V1}/arena/orders_fok",
        headers=arena_headers(arena_key),
        payload=payload,
        timeout=20,
    )
    order_id = extract_order_id(response)
    final = poll_order(arena_base_url, arena_key, order_id) if order_id else response
    summary = summarize_order(final, fallback={**response, **payload, "order_id": order_id})
    print(f"order_id={summary.get('order_id')}")
    print(f"status={summary.get('status')}")
    print(f"fixture_id={summary.get('fixture_id')}")
    print(f"team_code={summary.get('team_code')}")
    print(f"usd_size_filled={summary.get('usd_size_filled')}")
    print(f"open_avg_fill_price={summary.get('open_avg_fill_price')}")
    print(f"rejection_reason={summary.get('rejection_reason')}")
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
        print(f"Discretionary order failed: {exc}", file=sys.stderr)
        sys.exit(1)
