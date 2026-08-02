import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

SUPABASE = "https://ezvbmtvrvzageqixvdak.supabase.co"
SUPABASE_KEY = "REMOVED_SUPABASE_KEY"
ARENA = os.getenv("ARENA_BASE_URL", "https://stair-ai.com").rstrip("/")
ARENA_V1 = os.getenv("ARENA_API_PREFIX", "/api/v1").rstrip("/")
SPORTMONKS_SEASON_ID = 26618


def status(name, ok, detail=""):
    marker = "OPTIONAL" if detail == "optional" and not ok else "OK" if ok else "FAIL"
    suffix = f" - {detail}" if detail else ""
    print(f"{name}: {marker}{suffix}")


def main():
    load_dotenv()

    arena_key = os.getenv("ARENA_KEY")
    anthropic_key = os.getenv("ANTHROPIC_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")

    print(f"ARENA_BASE_URL: {ARENA}")
    status("ARENA_KEY", bool(arena_key))
    status("OPENAI_API_KEY", bool(openai_key))
    status("ANTHROPIC_KEY", bool(anthropic_key), "optional")
    status("Model provider", bool(openai_key or anthropic_key or os.getenv("USE_OPENAI") == "0"), "OpenAI/Anthropic or local rules")

    catalog_url = f"{SUPABASE}/rest/v1/catalog_full"
    catalog_params = {
        "select": "table_name,category,row_count,table_description",
        "order": "category,table_name",
        "limit": "3",
    }
    catalog_headers = {"apikey": SUPABASE_KEY}
    r = requests.get(catalog_url, params=catalog_params, headers=catalog_headers, timeout=15)
    r.raise_for_status()
    rows = r.json()
    status("Supabase catalog", isinstance(rows, list), f"{len(rows)} rows returned")

    mapping_url = f"{SUPABASE}/rest/v1/data_sportmonks_polymarket_mapping"
    r = requests.get(
        mapping_url,
        params={
            "sportmonks_fixture_id": "eq.19609127",
            "select": "polymarket_event_slug",
            "limit": "1",
        },
        headers={"apikey": SUPABASE_KEY, "Accept-Profile": "world_cup_arena"},
        timeout=15,
    )
    r.raise_for_status()
    mapping = r.json()
    slug = mapping[0]["polymarket_event_slug"] if mapping else None
    status("Supabase mapping", bool(slug), slug or "no mapping")

    if arena_key:
        h = {"x-api-key": arena_key}
        schedule_url = f"{ARENA}/api/v1/data/proxy/sportmonks/v3/football/schedules/seasons/{SPORTMONKS_SEASON_ID}"
        r = requests.get(schedule_url, headers=h, timeout=20)
        status("Arena Sportmonks proxy", r.ok, f"HTTP {r.status_code}")

        if slug:
            r = requests.get(
                f"{ARENA}/api/v1/data/proxy/polymarket-gamma/events",
                headers=h,
                params={"slug": slug},
                timeout=20,
            )
            r.raise_for_status()
            event = (r.json().get("body") or [None])[0]
            token = None
            for market in event.get("markets") or []:
                ids = market.get("clobTokenIds")
                if ids:
                    token = json.loads(ids)[0]
                    break
            if token:
                r = requests.get(
                    f"{ARENA}/api/v1/data/proxy/polymarket-clob/midpoint",
                    headers=h,
                    params={"token_id": token},
                    timeout=20,
                )
                status("Polymarket midpoint", r.ok, f"HTTP {r.status_code}")
            else:
                status("Polymarket midpoint", False, "no token found")

        r = requests.get(f"{ARENA}{ARENA_V1}/arena/agents/me", headers=h, timeout=20)
        status("Arena agent wallet", r.ok, f"HTTP {r.status_code}")
        r = requests.get(f"{ARENA}{ARENA_V1}/arena/exposure", headers=h, params={"include_closing": "true"}, timeout=20)
        status("Arena exposure", r.ok, f"HTTP {r.status_code}")
        r = requests.get(f"{ARENA}{ARENA_V1}/arena/orders", headers=h, timeout=20)
        status("Arena order history", r.ok, f"HTTP {r.status_code}")
        r = requests.get(f"{ARENA}{ARENA_V1}/arena/matches/19609127", headers=h, timeout=20)
        status("Arena match timing", r.ok, f"HTTP {r.status_code}")
        print("Order submission is not tested by smoke_check.")
    else:
        status("Arena Sportmonks proxy", False, "set ARENA_KEY to test")

    if not arena_key or not (openai_key or anthropic_key or os.getenv("USE_OPENAI") == "0"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
