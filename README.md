# PolyStair

Local workspace for building a Stair AI World Cup Agent Arena bot.

Production arena base URL is `https://stair-ai.com`. The old `https://staging.stair-ai.com` URL is deprecated for community access.

## Security

Keep real credentials in `.env` only. Do not commit `.env`, `.venv/`, `output/`, ZIP exports, logs, raw account reports, or local API downloads. See `SECURITY.md` before publishing or sharing the repository.

## Ready Check

```powershell
.\.venv\Scripts\python.exe scripts\smoke_check.py
```

The smoke check validates Stair reads, Supabase mapping, Polymarket midpoint, agent wallet, and arena exposure. It does not place orders.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Then fill in `ARENA_KEY`. OpenAI can stay disabled for the local blended model.

For OpenAI, set:

```text
OPENAI_API_KEY=sk-...
USE_OPENAI=0
ARENA_BASE_URL=https://stair-ai.com
OPENAI_MODEL=gpt-4.1-mini
```

## Prematch Agent

Dry-run the default opener fixture:

```powershell
.\.venv\Scripts\python.exe scripts\agent_prematch.py
```

The script writes its full data bundle, prediction, strategy, and ledger-shaped records to `output/prematch_19609127.json`.

To use another fixture:

```powershell
$env:FIXTURE_ID="19609127"
.\.venv\Scripts\python.exe scripts\agent_prematch.py
```

Orders stay disabled unless `DRY_RUN=0`, the strategy says `should_trade=true`, balance is available, size is <= `$1`, and you type `CONFIRM LIVE ORDER`.

Ledger traces are always saved locally when `SUBMIT_LEDGER=0`. Remote ledger submission posts one batch to `/api/v1/arena/ledger/records/batch` when `SUBMIT_LEDGER=1`; optional validation posts to `/api/v1/arena/ledger/records/validate` when `VALIDATE_LEDGER=1`.

## Reasoning Score Checks

Before submitting a live prematch trace, validate the latest local output:

```powershell
.\.venv\Scripts\python.exe scripts\reasoning_score_self_check.py --latest
```

Audit all historical local reasoning artifacts:

```powershell
.\.venv\Scripts\python.exe scripts\audit_reasoning_traces.py
```

Save a read-only Arena account report:

```powershell
.\.venv\Scripts\python.exe scripts\arena_account_report.py --save
```

## Scan Fixtures

Rank the first few mapped fixtures by model-vs-market edge:

```powershell
$env:SCAN_LIMIT="3"
.\.venv\Scripts\python.exe scripts\scan_fixtures.py
```

The scanner writes `output/scan_report.json`.

## Dashboard

Start the local operator dashboard:

```powershell
.\.venv\Scripts\python.exe scripts\dashboard_server.py
```

Then open:

```text
http://127.0.0.1:8787
```
