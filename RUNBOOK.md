# PolyStair Runbook

Current mode: blended non-LLM model with OpenAI disabled for cost/quota safety.

Production mode uses `ARENA_BASE_URL=https://stair-ai.com`. The old staging host is deprecated for community access.

## What Works

- Stair API key works for data reads.
- Sportmonks fixture detail works.
- Supabase mapping and priors work.
- Polymarket Gamma + CLOB midpoint reads work.
- Agent wallet endpoint works at `/api/v1/arena/agents/me` when enabled for the key.
- Exposure endpoint is `/api/v1/arena/exposure` and represents holdings, not full order history.
- Order history is available at `/api/v1/arena/orders`.
- Match timing is checked through `/api/v1/arena/matches/{fixture_id}` before prediction/order flow.
- Local blended prediction, strategy, and ledger-shaped records work.
- Fixture scanning report and dashboard work.

## Staging Reality

- Production wallet is expected to be funded with 100 USDC.
- LLM provider keys are builder-provided.
- Orders are live at `/api/v1/arena/orders`, but default remains `DRY_RUN=1` and local max size is `$1`.
- `/api/v1/ledger/sessions` was removed.
- Ledger batch endpoint is `/api/v1/arena/ledger/records/batch` and remains configurable via `LEDGER_BATCH_ENDPOINT`.
- Optional dry-run validation uses `/api/v1/arena/ledger/records/validate` when `VALIDATE_LEDGER=1`.

## Safe Commands

Reasoning score preflight before any live submission:

```powershell
.\.venv\Scripts\python.exe scripts\reasoning_score_self_check.py --latest
```

Historical reasoning audit and account report:

```powershell
.\.venv\Scripts\python.exe scripts\audit_reasoning_traces.py
.\.venv\Scripts\python.exe scripts\arena_account_report.py --save
```

Single fixture:

```powershell
$env:USE_OPENAI="0"
$env:DRY_RUN="1"
$env:FIXTURE_ID="19609157"
.\.venv\Scripts\python.exe scripts\agent_prematch.py
```

Scan fixtures:

```powershell
$env:USE_OPENAI="0"
$env:SCAN_LIMIT="12"
.\.venv\Scripts\python.exe scripts\scan_fixtures.py
```

Dashboard:

```powershell
.\.venv\Scripts\python.exe scripts\dashboard_server.py
```

Open `http://127.0.0.1:8787`.

## Latest Blended Scan

- Fixture: `19609157`
- Match: Côte d'Ivoire vs Ecuador
- Prediction: long `CIV`
- Model probability: `0.349`
- Market midpoint: `0.265`
- Edge: `+8.4 pp`
- Trade gate: blocked because confidence is low

Do not disable `DRY_RUN` unless the trade gate passes and you intentionally confirm a tiny staging order.

## Files To Show

- `output/scan_report.md`
- `output/scan_report.json`
- `output/prematch_19609157.json`
