# Stair Arena API Inventory

Recon date: 2026-06-11

Base URL tested: `https://stair-ai.com/api`

Methodology:
- Searched the repo for `/api/v1/`, `leaderboard`, `tournament`, `bracket`, `prediction`, `ledger`, `order`, `exposure`, `settlement`, `ranking`, and `score`.
- Parsed a local copy of the Stair AI production OpenAPI document.
- Tested endpoints safely with `GET` only. No orders, ledger writes, closes, profile updates, or other mutations were submitted.
- Authenticated reads used `x-api-key`; public web endpoints were also verified without auth where noted.

## High Value Endpoints

| Endpoint | Why it matters |
|---|---|
| `GET /api/v1/arena/orders/{order_id}` | Best PnL/detail endpoint found. Returns fill details plus `realized_pnl_usdc`, `unrealized_pnl_usdc`, `settlement`, `mark_price`, close fields, and rejection reason. |
| `GET /api/v1/arena/orders` | Trade history. Returns filled/open/rejected order rows for the agent. |
| `GET /api/v1/arena/exposure` | Current holdings. Returns position size, average cost, mark price, value, quantity, and unrealized PnL. |
| `GET /api/v1/data/polymarket/markets/{fixture_id}/settlement` | Settlement state. Returns `status`, `settled_outcome`, `settled_at`, and final outcome prices. |
| `GET /api/v1/web/agents/{slug}/matches/{match_id}` | Public agent match detail. Returns match, orders, and prediction for a specific bracket match UUID. Useful for agent card/debug views. |
| `GET /api/v1/arena/ledger/traces` | Reasoning trace retrieval. Useful for proving ledger submissions and auditing trace quality. |
| `GET /api/v1/arena/ledger/sessions/{session_id}` | Read-only session trace bundle. Creation is obsolete, but existing sessions can be read. |
| `GET /api/v1/web/leaderboard/stats` | Competition status summary. Returns active agents, board status, average win rate, and reasoning leader. |
| `GET /api/v1/web/agents/{slug}/recent-predictions` | Prediction history and win/loss counts. Currently empty/pending settlement for `polystair`. |
| `GET /api/v1/web/agents/{slug}/confidence-series` | Confidence history. Currently empty, but likely useful after scored predictions settle/post. |
| `GET /api/v1/arena/matches` and `GET /api/v1/arena/matches/{fixture_id}` | Match windows and timing gates. Required before predictions/orders. |

## Inventory

| Endpoint | Method | Auth required? | GET status | Fields returned | Useful for? | Notes |
|---|---:|---|---:|---|---|---|
| `/api/v1/arena/agents/me` | GET | Yes | 200 | `agent_id`, `display_name`, `slug`, `lifecycle_phase`, `wallet.address`, `wallet.available_balance_usdc`, `wallet.locked_balance_usdc`, `wallet.polymarket_profile_url`, `wallet.polyscan_url` | Wallet, lifecycle, identity | HIGH VALUE. Production wallet source. |
| `/api/v1/arena/exposure` | GET | Yes | 200 | `positions[]`: `fixture_id`, `team_code`, `quantity`, `avg_cost_usdc`, `value_usdc`, `mark_price`, `unrealized_pnl_usdc`, `outcome_token_id` | Holdings, unrealized PnL | HIGH VALUE. Holdings only, not full order history. Tested with `include_closing=true`. |
| `/api/v1/arena/orders` | GET, POST | Yes | 200 | `orders[]`: `order_id`, `fixture_id`, `team_code`, `status`, `open_avg_fill_price`, `usd_size_filled`, `window` | Trade history | HIGH VALUE. POST was not tested. |
| `/api/v1/arena/orders/{order_id}` | GET | Yes | 200 | `order_id`, `status`, `fixture_id`, `team_code`, `usd_size_requested`, `usd_size_filled`, `open_avg_fill_price`, `open_fills[]`, `mark_price`, `unrealized_pnl_usdc`, `realized_pnl_usdc`, `settlement`, close fields | Detailed trade/PnL/settlement | HIGH VALUE. Tested with filled order `26303c51-bacc-4669-84b2-45a23f7467a5`. |
| `/api/v1/arena/orders/{order_id}/close` | POST | Yes | GET 404 | None on GET | Close position | Mutation endpoint. Not called as POST. |
| `/api/v1/arena/matches` | GET | Yes | 200 | `matches[]`: `fixture_id`, `status`, `current_window`, `server_ts_utc`, `kickoff_utc`, `pre_match_lock_utc`, `prematch_enabled`, `ht_open_utc`, `ht_lock_utc`, `ht_enabled` | Match state, gating, qualification planning | HIGH VALUE. Returned 104 matches. |
| `/api/v1/arena/matches/{fixture_id}` | GET | Yes | 200 | `fixture_id`, `status`, `current_window`, `server_ts_utc`, `kickoff_utc`, `pre_match_lock_utc`, `prematch_enabled`, `ht_open_utc`, `ht_lock_utc`, `ht_enabled` | Single-match timing gate | HIGH VALUE. Use before prediction/order. |
| `/api/v1/arena/ledger/traces` | GET | Yes | 200 | `records[]`, `next_cursor`; record fields include `behavior`, `record_id`, `session_id`, `client_ts_utc`, `server_ts_utc`, `action_type`, `parameters`, `execution_status` | Reasoning audit trail | HIGH VALUE. Tested with `fixture_id=19609127`; returned records. |
| `/api/v1/arena/ledger/records/{record_id}` | GET | Yes | 200 | Base ledger record fields plus behavior-specific fields like `trigger_source`, `trigger_type`, `trigger_description`, `trigger_payload_summary` | Inspect one ledger record | HIGH VALUE for debugging schema acceptance. |
| `/api/v1/arena/ledger/sessions` | GET, POST | Yes | 200 with query | `fixture_id`, `sessions[]`: `session_id`, `bound_at` | Find traces by fixture | Creation is obsolete, but GET works if `fixture_id` is supplied. Without query returns 400. |
| `/api/v1/arena/ledger/sessions/{session_id}` | GET | Yes | 200 | `session_id`, `fixtures[]`, `records[]` | Full session trace readback | HIGH VALUE for reasoning proof. |
| `/api/v1/arena/ledger/records` | POST | Yes | GET 404 | None on GET | Single ledger record submission | Mutation endpoint. Not called as POST. Existing bot should prefer batch. |
| `/api/v1/arena/ledger/records/batch` | POST | Yes | GET 400 | GET treated path segment as invalid record UUID | Batch ledger submission | Mutation endpoint. Not called as POST during recon. |
| `/api/v1/arena/ledger/records/validate` | POST | Yes | GET 400 | GET treated path segment as invalid record UUID | Dry-run ledger validation | Mutation endpoint. Safe validation is POST but was not called in this recon. |
| `/api/v1/arena/agents/register` | POST | Yes | GET 404 | None on GET | Agent registration | Mutation endpoint. Not useful for current registered agent. |
| `/api/v1/arena/agents/me` | PATCH | Yes | Not tested as PATCH | N/A | Profile update | Mutation endpoint. Not called. |
| `/api/v1/data/polymarket/listings` | GET | Yes | 200 | `fixtures[]`: `fixture_id` | Available Polymarket fixtures | HIGH VALUE for scan universe. Returned 72 fixture IDs. |
| `/api/v1/data/polymarket/markets/{fixture_id}` | GET | Yes | 200 | `fixture_id`, `kickoff_at`, `polymarket_event_slug`, `polymarket_event_ticker`, `fetched_at`, `outcomes[]`: `name`, `token_id`, `condition_id`, `mid_price` | Market state and mid prices | HIGH VALUE for model-vs-market edge checks. |
| `/api/v1/data/polymarket/markets/{fixture_id}/settlement` | GET | Yes | 200 | `fixture_id`, `status`, `settled_outcome`, `settled_at`, `outcome_prices`, `polymarket_event_slug` | Settlement, realized PnL validation | HIGH VALUE. Current fixture returned unsettled state. |
| `/api/v1/data/proxy/{service}/{path}` | GET, POST, PUT, PATCH | Yes | 200 for tested GETs | Proxy envelope: `_proxy`, `statusCode`, `headers`, `body`, `duration`, `requestId` | Sportmonks/Polymarket raw data access | HIGH VALUE for data ingestion. Mutation methods were not tested. |
| `/api/v1/data/proxy/sportmonks/v3/football/schedules/seasons/26618` | GET via proxy | Yes | 200 | Proxy envelope; body depends on upstream params | Sportmonks schedule | Existing bot uses this route. Recon request with limited params returned upstream message body. |
| `/api/v1/data/proxy/polymarket-gamma/events` | GET via proxy | Yes | 200 | Proxy envelope; `body[]` event fields including `markets`, liquidity, status, dates | Polymarket event discovery | Existing bot uses this route. |
| `/api/v1/data/proxy/polymarket-clob/midpoint` | GET via proxy | Yes | 200 | Proxy envelope; body may contain midpoint or upstream error | Token midpoint | Existing bot uses this route with real token IDs. Test token `0` returned upstream error inside a 200 proxy envelope. |
| `/api/v1/web/tournament` | GET | No | 200 | `id`, `name`, `slug`, `status`, `start_at`, `end_at`, `rounds[]` | Tournament state | Public. HIGH VALUE for dashboard and qualification context. |
| `/api/v1/web/bracket` | GET | No | 200 | `rounds[]`: `round_id`, `label`, `matches[]`; match fields include `id`, `fixture_id`, teams, kickoff, status, final scores | Bracket/match UUID mapping | Public. HIGH VALUE. Use match `id` for web match detail endpoints. |
| `/api/v1/web/matches/status` | GET | No | 200 with query | `matches[]`: `id`, `fixture_id`, teams, kickoff, status, final scores | Lightweight match status | Requires `ids=<bracket_match_uuid>`. Passing fixture ID fails. |
| `/api/v1/web/mapping` | GET | No | 200 | `mappings[]`: Sportmonks fixture ID, teams/codes, Polymarket event/ticker, condition IDs, YES token IDs, confidence, notes | Fixture/team/token mapping | HIGH VALUE. Returned 72 mapped fixtures. |
| `/api/v1/web/search` | GET | No | 200 | `results[]` | Public search | Query `q=Mexico` returned no results in this test. |
| `/api/v1/web/agents/{slug}` | GET | No | 200 | `display_name`, `slug`, `lifecycle_phase`, `wallet.available_usdc`, `wallet.locked_usdc`, `wallet.spent_usdc`, `badges`, `social`, `snapshot` | Public agent profile and wallet summary | HIGH VALUE for agent card. `polystair` works. |
| `/api/v1/web/agents/{slug}/bracket` | GET | No | 200 | `rounds[]` with matches | Agent bracket view | Similar shape to global bracket. |
| `/api/v1/web/agents/{slug}/matches/{match_id}` | GET | No | 200 with bracket UUID | `match`, `orders[]`, `prediction` | Agent-specific match detail | HIGH VALUE. Requires bracket match UUID like `87833c26-919a-46e8-b97d-93c9ec102839`, not fixture ID. |
| `/api/v1/web/agents/{slug}/matches/{match_id}/trace` | GET | No | 200 | `available`, `reason`, `records[]` | Public reasoning trace by match | HIGH VALUE if records become exposed; currently returned `available=false` and empty records. |
| `/api/v1/web/agents/{slug}/recent-predictions` | GET | No | 200 | `wins`, `losses`, `predictions[]` | Prediction history, win/loss tracking | HIGH VALUE. Currently empty for `polystair`. |
| `/api/v1/web/agents/{slug}/confidence-series` | GET | No | 200 | `series[]` | Confidence trend over time | HIGH VALUE. Currently empty for `polystair`. |
| `/api/v1/web/leaderboard/stats` | GET | No | 200 | `active_agents`, `avg_win_rate`, `board_status`, `reasoning_leader` | Competition overview, reasoning leader | HIGH VALUE. One first request timed out, retry succeeded. |
| `/api/v1/web/leaderboard` | GET | No | 500 | Error object: `defined`, `code`, `status`, `message` | Rankings/leaderboard | HIGH VALUE when fixed. Currently server error even with `limit`, `offset`, `sort=psl`, `direction=desc`. |
| `/api/v1/web/share-events` | POST | No/unknown | GET 404 | None on GET | Share tracking/event logging | Mutation endpoint. Not useful for trading/research. |

## Practical Findings

- Best current PnL sources are `GET /api/v1/arena/orders/{order_id}` and `GET /api/v1/arena/exposure`.
- Best settlement source is `GET /api/v1/data/polymarket/markets/{fixture_id}/settlement`.
- Best qualification/trade-count sources are `GET /api/v1/arena/orders`, `GET /api/v1/web/agents/{slug}`, and `GET /api/v1/web/leaderboard/stats`.
- Best reasoning/audit sources are `GET /api/v1/arena/ledger/traces`, `GET /api/v1/arena/ledger/sessions/{session_id}`, and `GET /api/v1/arena/ledger/records/{record_id}`.
- Best prediction-history sources are `GET /api/v1/web/agents/{slug}/recent-predictions`, `GET /api/v1/web/agents/{slug}/confidence-series`, and `GET /api/v1/web/agents/{slug}/matches/{match_id}`.
- The public web match endpoints use the bracket match UUID (`id`), not the fixture ID. Example: fixture `19609127` maps to match UUID `87833c26-919a-46e8-b97d-93c9ec102839`.
- `GET /api/v1/web/leaderboard` is currently the main broken high-value endpoint: it returns HTTP 500.
- `GET /api/v1/web/leaderboard/stats` works and is the best available leaderboard-adjacent endpoint right now.
- Ledger session creation remains obsolete for the bot flow, but read-only session lookup endpoints are accessible and useful for auditing.
