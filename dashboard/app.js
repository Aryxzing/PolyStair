let state = null;
let selected = null;
let sortMode = "edge";
let autoRefresh = true;
let refreshInFlight = false;
let refreshCountdown = 10;
let refreshIntervalSeconds = 10;

const pages = {
  overview: ["Command", "Competition-grade operating view for predictions, risk, and readiness."],
  leaderboard: ["Opportunity Leaderboard", "Sortable fixture ranking by edge, confidence, and grade."],
  tournament: ["Tournament", "World Cup Arena tournament state and qualification."],
  matches: ["Upcoming Matches", "Fixture browser and operator commands."],
  reasoning: ["Reasoning Center", "Submitted reasoning records and live trade rationale."],
  portfolio: ["Portfolio", "Live wallet, positions, and exposure state."],
  "orders-page": ["Order History", "Production Arena order history."],
  performance: ["Performance", "Production trades, capital, and qualification progress."],
  predictions: ["Prediction History", "Public web prediction outcomes."],
  "confidence-page": ["Confidence", "Confidence history and calibration."],
  competition: ["Competition", "Leaderboard and tournament stats."],
  "agent-card": ["Agent Card View", "Screenshot-ready production state for judges and stakeholders."],
  health: ["System Health", "Arena, ledger, orders, exposure, and public API status."],
  system: ["System", "Runtime settings, outputs, and command logs."]
};

const gradeRank = {
  "A+ Opportunity": 5,
  "A Opportunity": 4,
  "B Opportunity": 3,
  "Watchlist": 2,
  "Avoid": 1
};

function money(value) {
  if (value === null || value === undefined || value === "") return "N/A";
  return `$${Number(value).toFixed(2)}`;
}

function fixed(value, digits = 6) {
  if (value === null || value === undefined || value === "") return "N/A";
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(digits) : String(value);
}

function pct(value) {
  if (value === null || value === undefined || value === "") return "N/A";
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function num(value, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function na(value) {
  return value === null || value === undefined || value === "" ? "N/A" : value;
}

function dateTime(value) {
  if (value === null || value === undefined || value === "") return "N/A";
  const n = Number(value);
  const date = Number.isFinite(n) ? new Date(n) : new Date(value);
  if (Number.isNaN(date.getTime())) return "N/A";
  return date.toLocaleString();
}

function rows() {
  return (state?.scan_report?.ranked || []);
}

function selectedRow() {
  const all = rows();
  if (!selected && all.length) selected = all[0].fixture_id;
  return all.find(r => r.fixture_id === selected) || all[0] || null;
}

function setPage(id) {
  document.querySelectorAll(".page").forEach(el => el.classList.toggle("active", el.id === id));
  document.querySelectorAll(".nav").forEach(el => el.classList.toggle("active", el.dataset.page === id));
  document.getElementById("page-title").textContent = pages[id][0];
  document.getElementById("subtitle").textContent = pages[id][1];
}

async function loadStatus() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const response = await fetch("/api/status");
    const next = await response.json();
    state = next;
    if (!selected && rows().length) selected = rows()[0].fixture_id;
    render();
  } catch (error) {
    if (state) {
      state.live_arena = { ...(state.live_arena || {}), ok: false, badge: "Arena refresh warning", error: String(error) };
      renderLiveStatus();
      renderProductionStatus();
    }
  } finally {
    refreshInFlight = false;
    refreshCountdown = refreshIntervalSeconds;
  }
}

function agentStatus() {
  if (!state.checks.arena_key || !state.wallet?.ok) return ["ERROR", "error"];
  if (!state.live_arena?.ok) return ["WARNING", "warning"];
  return ["READY", "ready"];
}

function render() {
  renderLiveStatus();
  renderProductionStatus();
  renderExecutive();
  renderScorecard();
  renderLeaderboard();
  renderReasoning();
  renderPortfolio();
  renderPerformance();
  renderTournament();
  renderMatches();
  renderOrderHistory();
  renderPredictionHistory();
  renderConfidencePage();
  renderCompetition();
  renderHealth();
  renderDemo();
  renderSystem();
  drawAllCharts();
}

function renderLiveStatus() {
  const live = state.live_arena || {};
  const badge = document.getElementById("live-badge");
  const refreshed = document.getElementById("last-refreshed");
  badge.textContent = live.badge || "Arena data warning";
  badge.className = `status-pill ${live.ok ? "ready" : "warning"}`;
  refreshed.textContent = `Last refreshed: ${live.last_refreshed ? new Date(live.last_refreshed).toLocaleString() : "--"}`;
  document.getElementById("auto-refresh-label").textContent =
    refreshIntervalSeconds > 0 ? `Auto-refresh: ${refreshCountdown}s` : "Auto-refresh: OFF";
}

function renderProductionStatus() {
  const runtime = state.runtime || {};
  const metrics = state.metrics || {};
  const live = state.live_arena || {};
  const items = [
    ["Arena", live.ok ? "Live Arena data" : "Warning", live.ok ? "good" : "warning"],
    ["Mode", runtime.mode || (state.env.DRY_RUN === "1" ? "DRY RUN" : "LIVE"), runtime.mode === "LIVE" ? "warning" : "good"],
    ["Execution", (runtime.order_execution_mode || state.env.ORDER_EXECUTION_MODE || "limit").toUpperCase(), runtime.order_execution_mode === "fok" ? "warning" : "neutral"],
    ["Strategy", runtime.strategy_mode || state.env.STRATEGY_MODE || "winner_only", "neutral"],
    ["Model", runtime.model_variant || state.env.MODEL_VARIANT || "baseline", "neutral"],
    ["Ledger", runtime.ledger || (state.env.SUBMIT_LEDGER === "1" ? "CONNECTED" : "LOCAL ONLY"), runtime.submit_ledger ? "good" : "neutral"],
    ["Positions", metrics.open_positions ?? (state.exposure?.positions || []).length, "neutral"],
    ["Filled Orders", metrics.filled_trades ?? 0, "good"],
    ["Rejected Orders", metrics.rejected_trades ?? 0, metrics.rejected_trades ? "warning" : "neutral"],
    ["Refresh", autoRefresh ? `${refreshCountdown}s` : "Paused", autoRefresh ? "neutral" : "warning"]
  ];
  document.getElementById("production-status").innerHTML = items.map(([k, v, c]) =>
    `<div class="status-item ${c}"><span>${k}</span><strong>${v}</strong></div>`
  ).join("");
}

function renderExecutive() {
  const best = selectedRow();
  const [label, cls] = agentStatus();
  const score = state.readiness_score || 0;
  const metrics = state.metrics || {};
  const runtime = state.runtime || {};
  const latestTrade = state.latest?.latest_trade;
  const latestOrder = state.latest?.latest_order || (state.orders?.orders || [])[0];
  const offset = 327 - (327 * score / 100);
  document.getElementById("score-ring").style.strokeDashoffset = offset;
  document.getElementById("readiness").textContent = `${score}`;
  document.getElementById("agent-status").textContent = label;
  document.getElementById("agent-status").className = `status-pill ${cls}`;

  document.getElementById("best-title").textContent = `Production ${runtime.mode || "LIVE"}: ${runtime.strategy_mode || "three_outcome_value"}`;
  document.getElementById("best-copy").innerHTML = metrics.rejected_reconciliation_warning
    ? `<strong>Rejected / Endpoint Reconciliation:</strong> ${metrics.rejected_reconciliation_warning}`
    : latestTrade
    ? `Latest live trade: <strong>${latestTrade.team_code} buy YES</strong>, ${latestTrade.status}, ${money(latestTrade.usd_size_filled)}, avg fill ${na(latestTrade.open_avg_fill_price)}. Latest scan remains available in Leaderboard.`
    : latestOrder
      ? `Latest order: <strong>${latestOrder.team_code} buy YES</strong>, ${latestOrder.status}. Filled size ${money(latestOrder.usd_size_filled)}.`
    : best
      ? `${best.prediction.outcome} at ${pct(best.prediction.probability)} vs market ${pct(best.market[best.prediction.outcome])}. Edge ${best.strategy.edge_pp} pp.`
      : "Waiting for scan and live Arena data.";

  const positions = state.exposure?.positions || [];
  const ledger = state.env.SUBMIT_LEDGER === "1" ? "CONNECTED" : "LOCAL ONLY";
  const kpis = [
    ["Agent Status", label, cls],
    ["Readiness", `${score}/100`, "neutral"],
    ["Available", money(state.wallet?.available_balance_usdc), "good"],
    ["Locked", money(state.wallet?.locked_balance_usdc), "neutral"],
    ["Open Positions", metrics.open_positions ?? positions.length, positions.length ? "warning" : "neutral"],
    ["Current Mode", runtime.mode || (state.env.DRY_RUN === "1" ? "DRY RUN" : "LIVE"), state.env.DRY_RUN === "1" ? "good" : "warning"],
    ["Order Execution", (runtime.order_execution_mode || state.env.ORDER_EXECUTION_MODE || "limit").toUpperCase(), runtime.order_execution_mode === "fok" ? "warning" : "neutral"],
    ["Ledger", ledger, ledger === "CONNECTED" ? "good" : "neutral"],
    ["Attempted Orders", metrics.attempted_orders ?? metrics.total_orders ?? 0, "neutral"],
    ["Filled Orders", metrics.filled_trades ?? 0, "good"],
    ["Rejected Orders", metrics.rejected_trades ?? 0, metrics.rejected_trades ? "warning" : "neutral"],
    ["Last Order Status", metrics.last_order_final_status || "N/A", metrics.last_order_final_status === "rejected" ? "warning" : "neutral"],
    ["10-Bet Progress", `${metrics.qualification_progress ?? 0}/${metrics.qualification_target ?? 10}`, "neutral"],
    ["Qualification", metrics.qualification_status || "N/A", metrics.qualification_progress >= metrics.qualification_target ? "good" : "warning"],
    ["Strategy", runtime.strategy_mode || state.env.STRATEGY_MODE || "winner_only", "neutral"],
    ["Model Variant", runtime.model_variant || state.env.MODEL_VARIANT || "baseline", "neutral"],
    ["Best Edge", best ? `${best.strategy.edge_pp} pp` : "N/A", best && best.strategy.edge_pp > 0 ? "good" : "neutral"]
  ];
  document.getElementById("executive-kpis").innerHTML = kpis.map(([k, v, c]) =>
    `<article class="kpi ${c}"><span>${k}</span><strong>${v}</strong></article>`
  ).join("");
  renderCommandCards();
}

function renderCommandCards() {
  const latestTrade = state.latest?.latest_trade;
  const latestOrder = state.latest?.latest_order || (state.orders?.orders || [])[0];
  const latestPrematch = state.latest_prematch || state.latest?.latest_prematch || {};
  const ledger = latestPrematch.ledger || {};
  const latestScan = selectedRow();
  document.getElementById("command-live-cards").innerHTML = [
    `<section class="panel"><h3>Latest Order Truth</h3>${
      latestTrade ? `<div class="score-row"><span>Outcome</span><strong>${latestTrade.team_code} buy YES</strong></div>
      <div class="score-row"><span>Status</span><strong>${latestTrade.status}</strong></div>
      <div class="score-row"><span>Size</span><strong>${money(latestTrade.usd_size_filled)}</strong></div>
      <div class="score-row"><span>Avg Fill</span><strong>${na(latestTrade.open_avg_fill_price)}</strong></div>` : latestOrder ? `<div class="score-row"><span>Outcome</span><strong>${latestOrder.team_code} buy YES</strong></div>
      <div class="score-row"><span>Status</span><strong>${latestOrder.status}</strong></div>
      <div class="score-row"><span>Filled Size</span><strong>${money(latestOrder.usd_size_filled)}</strong></div>
      <div class="score-row"><span>Reject Reason</span><strong>${na(latestOrder.rejection_reason)}</strong></div>` : `<div class="empty-state">No order returned yet.</div>`
    }</section>`,
    `<section class="panel"><h3>Latest Ledger Submission</h3>
      <div class="score-row"><span>Status</span><strong>${ledger.submitted ? "accepted" : "local/unavailable"}</strong></div>
      <div class="score-row"><span>Errors</span><strong>${ledger.response?.errors ? JSON.stringify(ledger.response.errors) : "N/A"}</strong></div>
      <div class="score-row"><span>Records</span><strong>${ledger.records_count ?? "N/A"}</strong></div>
    </section>`,
    `<section class="panel"><h3>Latest Scan</h3>${
      latestScan ? `<div class="score-row"><span>Fixture</span><strong>${latestScan.fixture_id}</strong></div>
      <div class="score-row"><span>Pick</span><strong>${latestScan.prediction?.outcome}</strong></div>
      <div class="score-row"><span>Edge</span><strong>${latestScan.strategy?.edge_pp} pp</strong></div>` : `<div class="empty-state">Run a scan to populate this card.</div>`
    }</section>`
  ].join("");
}

function renderScorecard() {
  const all = rows();
  const records = all.reduce((sum, r) => sum + (r.records?.length || 0), 0) + (state.latest_prematch?.records?.length || 0);
  const high = all.filter(r => r.prediction?.confidence_level === "high").length;
  const med = all.filter(r => r.prediction?.confidence_level === "medium").length;
  const tradesConsidered = all.filter(r => num(r.strategy?.edge_pp) > 0).length;
  const executed = state.metrics?.filled_trades ?? (state.orders?.orders || []).filter(o => String(o.status || "").toLowerCase() === "filled").length;
  const values = [
    ["Competition Status", `${agentStatus()[0]} / ${state.runtime?.mode || "LIVE"}`],
    ["Fixtures Scanned", state.stats.fixtures_scanned],
    ["Predictions Generated", all.length],
    ["Trades Considered", tradesConsidered],
    ["Trades Executed", executed],
    ["Rejected Orders", state.metrics?.rejected_trades ?? 0],
    ["10-Bet Progress", `${state.metrics?.qualification_progress ?? executed}/${state.metrics?.qualification_target ?? 10}`],
    ["High Confidence Signals", high],
    ["Medium Confidence Signals", med],
    ["No Trade Decisions", all.filter(r => !r.strategy?.should_trade).length],
    ["Reasoning Records", records]
  ];
  document.getElementById("scorecard").innerHTML = values.map(([k, v]) =>
    `<div class="score-row"><span>${k}</span><strong>${v}</strong></div>`
  ).join("");
}

function sortedRows() {
  const filter = (document.getElementById("filter")?.value || "").toLowerCase();
  const all = rows().filter(r => `${r.match} ${r.prediction?.outcome}`.toLowerCase().includes(filter));
  return all.sort((a, b) => {
    if (sortMode === "confidence") return num(b.prediction?.confidence_score) - num(a.prediction?.confidence_score);
    if (sortMode === "grade") return (gradeRank[b.strategy?.opportunity_class] || 0) - (gradeRank[a.strategy?.opportunity_class] || 0);
    return num(b.strategy?.edge_pp) - num(a.strategy?.edge_pp);
  });
}

function renderLeaderboard() {
  const body = document.getElementById("opportunity-rows");
  body.innerHTML = sortedRows().map(r => {
    const edge = num(r.strategy?.edge_pp);
    const cls = edge >= 0 ? "edge-pos" : "edge-neg";
    const isSelected = r.fixture_id === selected ? "selected" : "";
    return `<tr class="${isSelected}" data-fixture="${r.fixture_id}">
      <td><strong>${r.match}</strong><small>${r.fixture_id}</small></td>
      <td>${r.prediction.outcome}</td>
      <td>${pct(r.prediction.probability)}</td>
      <td>${pct(r.market[r.prediction.outcome])}</td>
      <td class="${cls}">${edge.toFixed(2)} pp</td>
      <td>${r.prediction.confidence_score}/100 <small>${r.prediction.confidence_level}</small></td>
      <td>${pct(r.prediction.data_quality)}</td>
      <td>${r.prediction.source_agreement}/3</td>
      <td><span class="grade">${r.strategy.opportunity_class}</span></td>
      <td><span class="decision ${r.strategy.should_trade ? "yes" : "no"}">${r.strategy.should_trade ? "TRADE" : "NO TRADE"}</span></td>
    </tr>`;
  }).join("");
  body.querySelectorAll("tr").forEach(row => row.addEventListener("click", () => {
    selected = Number(row.dataset.fixture);
    renderLeaderboard();
    renderReasoning();
    renderDemo();
    setPage("reasoning");
  }));
}

function renderReasoning() {
  const r = selectedRow();
  if (!r) return;
  const p = r.prediction;
  const s = r.strategy;
  const latest = state.latest_prematch || state.latest?.latest_prematch || {};
  const liveStrategy = latest.strategy || {};
  const livePrediction = latest.prediction || {};
  const liveEdges = liveStrategy.all_outcome_edges || [];
  const liveTrade = state.latest?.latest_trade;
  document.getElementById("reasoning-card").innerHTML = `
    ${liveTrade ? `
      <div class="audit-title live-reasoning">
        <span class="grade">Latest live trade reasoning</span>
        <h3>${liveTrade.team_code} buy YES · ${liveTrade.status}</h3>
        <p>${liveStrategy.rationale || "Live trade reasoning loaded from latest prematch output."}</p>
      </div>
      <div class="audit-grid">
        <div><span>Model Winner</span><strong>${liveStrategy.model_winner || livePrediction.outcome || "N/A"}</strong></div>
        <div><span>Selected Value</span><strong>${liveStrategy.selected_value_outcome || liveStrategy.outcome || liveTrade.team_code}</strong></div>
        <div><span>Executed Outcome</span><strong>${liveStrategy.execution_outcome || liveStrategy.outcome || liveTrade.team_code}</strong></div>
        <div><span>Hybrid Selector</span><strong>${liveStrategy.execution_selector || "N/A"}</strong></div>
        <div><span>Candidate Type</span><strong>${liveStrategy.candidate_type || "N/A"}</strong></div>
        <div><span>Ladder Bucket</span><strong>${liveStrategy.ladder_bucket || "N/A"}</strong></div>
        <div><span>Suggested Size</span><strong>${liveStrategy.suggested_size ? money(liveStrategy.suggested_size) : "N/A"}</strong></div>
        <div><span>Market Favorite</span><strong>${liveStrategy.market_favorite || "N/A"}</strong></div>
        <div><span>Confidence</span><strong>${liveStrategy.confidence_score || livePrediction.confidence_score || "N/A"}/100</strong></div>
      </div>
      ${liveStrategy.execution_reason ? `<p class="muted">${liveStrategy.execution_reason}</p>` : ""}
      <p class="muted">The most likely winner can differ from the value outcome because this mode maximizes model probability minus market midpoint, not raw win probability.</p>
      <h4>All Outcome Edges</h4>
      <ul>${liveEdges.map(x => `<li>${x.outcome}: model ${pct(x.model_probability)}, market ${pct(x.market_midpoint)}, edge ${x.edge_pp} pp</li>`).join("") || "<li>N/A</li>"}</ul>
      <h4>Risk Factors</h4>
      <ul>${(livePrediction.risk_factors || []).map(x => `<li>${x}</li>`).join("") || "<li>N/A</li>"}</ul>
    ` : ""}
    <div class="audit-title">
      <span class="grade">${s.should_trade ? s.opportunity_class : "latest scan watchlist"}</span>
      <h3>${r.match}</h3>
      <p>${s.recommended_action}</p>
    </div>
    <div class="audit-grid">
      <div><span>Prediction</span><strong>${p.outcome} ${pct(p.probability)}</strong></div>
      <div><span>Model Winner</span><strong>${s.model_winner || p.outcome}</strong></div>
      <div><span>Value Candidate</span><strong>${s.selected_value_outcome || "N/A"}</strong></div>
      <div><span>Executed Outcome</span><strong>${s.execution_outcome || s.outcome || "N/A"}</strong></div>
      <div><span>Hybrid Selector</span><strong>${s.execution_selector || "N/A"}</strong></div>
      <div><span>Candidate Type</span><strong>${s.candidate_type || "N/A"}</strong></div>
      <div><span>Ladder Bucket</span><strong>${s.ladder_bucket || "N/A"}</strong></div>
      <div><span>Suggested Size</span><strong>${s.suggested_size ? money(s.suggested_size) : "N/A"}</strong></div>
      <div><span>Market</span><strong>${pct(r.market[p.outcome])}</strong></div>
      <div><span>Edge</span><strong>${s.edge_pp} pp</strong></div>
      <div><span>Confidence</span><strong>${p.confidence_score}/100</strong></div>
    </div>
    ${s.execution_reason ? `<h4>Hybrid Reason</h4><p>${s.execution_reason}</p>` : ""}
    <h4>Prediction Rationale</h4>
    <p>${p.rationale}</p>
    <h4>Trade Rationale</h4>
    <p>${s.no_trade_reason || "Trade gate passed."}</p>
    <h4>Risk Factors</h4>
    <ul>${(p.risk_factors || []).map(x => `<li>${x}</li>`).join("")}</ul>
    <h4>Rejected Alternatives</h4>
    <ul>${(p.rejected_alternatives || []).map(x => `<li>${x.outcome}: ${pct(x.probability)} - ${x.reason}</li>`).join("")}</ul>
  `;
  renderExplainability(r);
}

function componentList(title, data) {
  if (!data) return `<div class="evidence"><h4>${title}</h4><p>N/A</p></div>`;
  return `<div class="evidence"><h4>${title}</h4>${Object.entries(data).map(([k, v]) =>
    `<div class="bar-row"><span>${k}</span><strong>${pct(v)}</strong></div>`
  ).join("")}</div>`;
}

function renderExplainability(r) {
  const p = r.prediction;
  document.getElementById("explainability").innerHTML = `
    ${componentList("Sportmonks View", p.components?.sportmonks_1x2)}
    ${componentList("Bookmaker View", p.components?.bookmaker_consensus)}
    ${componentList("Supabase View", p.components?.supabase_priors)}
    <div class="evidence">
      <h4>Final Decision</h4>
      <div class="bar-row"><span>Final Prediction</span><strong>${p.outcome} ${pct(p.probability)}</strong></div>
      <div class="bar-row"><span>Market Price</span><strong>${pct(r.market[p.outcome])}</strong></div>
      <div class="bar-row"><span>Edge</span><strong>${r.strategy.edge_pp} pp</strong></div>
      <div class="bar-row"><span>Decision</span><strong>${r.strategy.should_trade ? "Trade" : "No trade"}</strong></div>
    </div>
  `;
}

function renderPortfolio() {
  renderWallet();
  renderPositions();
  renderOrders();
}

function renderWallet() {
  const w = state.wallet || {};
  const rows = [
    ["Lifecycle", w.lifecycle_phase || "N/A"],
    ["Net Available (derived)", money(w.available_balance_usdc)],
    ["Arena Reported Available", money(w.reported_available_balance_usdc)],
    ["Open Position Value", money(w.open_position_value_usdc)],
    ["Locked Balance", money(w.locked_balance_usdc)],
    ["Wallet Address", w.address ? `${w.address.slice(0, 10)}...${w.address.slice(-8)}` : "N/A"],
    ["Polymarket", w.polymarket_profile_url ? `<a href="${w.polymarket_profile_url}" target="_blank">Open profile</a>` : "N/A"],
    ["Polyscan", w.polyscan_url ? `<a href="${w.polyscan_url}" target="_blank">Open wallet</a>` : "N/A"]
  ];
  if (!w.ok && w.error) rows.unshift(["Wallet API", w.error]);
  document.getElementById("wallet").innerHTML = rows.map(([k, v]) =>
    `<div class="setting"><span>${k}</span><strong>${v}</strong></div>`
  ).join("");
}

function renderPositions() {
  const exp = state.exposure || {};
  const positions = exp.positions || [];
  const root = document.getElementById("positions");
  if (!exp.ok) {
    root.innerHTML = `<div class="note">${exp.error || "Exposure unavailable"}</div>`;
    return;
  }
  if (!positions.length) {
    root.innerHTML = `<div class="empty-state">No open positions. The agent is clean and ready.</div>`;
    return;
  }
  root.innerHTML = positions.map(p => `
    <div class="position">
      <strong>${p.fixture_id || p.fixture_code || "fixture"} - ${p.team_code}</strong>
      <span class="status-chip ${String(p.status || p.window || "open").toLowerCase()}">${p.status || p.window || "open"}</span>
      <div class="position-grid">
        <div>Fixture<span>${na(p.fixture_id || p.fixture_code)}</span></div>
        <div>Position<span>${na(p.team_code)}</span></div>
        <div>Value<span>${fixed(p.value_usdc, 6)} USDC</span></div>
        <div>Average Cost<span>${fixed(p.avg_cost_usdc, 6)}</span></div>
        <div>Quantity<span>${fixed(p.quantity, 6)}</span></div>
        <div>Current Mark<span>${na(p.mark_price)}</span></div>
        <div>Unrealized PnL<span>${fixed(p.unrealized_pnl_usdc, 6)}</span></div>
        <div>Window<span>${na(p.window)}</span></div>
      </div>
    </div>
  `).join("");
}

function renderOrders() {
  const orderState = state.orders || {};
  const orders = orderState.orders || [];
  const root = document.getElementById("orders");
  if (!root) return;
  if (!orderState.ok) {
    root.innerHTML = `<div class="note">${orderState.error || "Orders unavailable"}</div>`;
    return;
  }
  if (!orders.length) {
    root.innerHTML = `<div class="empty-state">No production orders returned by Arena.</div>`;
    return;
  }
  root.innerHTML = orders.map(o => `
    <div class="position">
      <strong>${o.order_id || "order"} - ${o.team_code}</strong>
      <span class="status-chip ${String(o.status || "").toLowerCase()}">${o.status}</span>
      <div class="position-grid">
        <div>Fixture<span>${na(o.fixture_id)}</span></div>
        <div>Team<span>${na(o.team_code)}</span></div>
        <div>Filled Size<span>${fixed(o.usd_size_filled, 6)} USDC</span></div>
        <div>Avg Fill<span>${fixed(o.open_avg_fill_price, 6)}</span></div>
        <div>Window<span>${na(o.window)}</span></div>
        <div>Side<span>${o.side || "buy_yes (inferred)"}</span></div>
        <div>Settlement<span>${na(o.settlement_stage || o.settlement_status)}</span></div>
        <div>Realized PnL<span>${o.realized_pnl_usdc === null || o.realized_pnl_usdc === undefined ? "Pending" : fixed(o.realized_pnl_usdc, 6)}</span></div>
      </div>
    </div>
  `).join("");
}

function qualificationStatus(progress) {
  if (progress >= 10) return "Qualified threshold met";
  if (progress >= 5) return "In progress";
  return "Early";
}

function renderPerformance() {
  const m = state.metrics || {};
  const ladder = state.qualification_ladder || {};
  const target = m.qualification_target ?? 10;
  const progress = m.qualification_progress ?? 0;
  const pctDone = Math.min(100, (progress / target) * 100);
  document.getElementById("qualification").innerHTML = `
    <div class="qual-head">
      <strong>${progress} / ${target}</strong>
      <span class="status-pill ${progress >= target ? "ready" : progress >= 5 ? "warning" : ""}">${qualificationStatus(progress)}</span>
    </div>
    <div class="progress"><span style="width:${pctDone}%"></span></div>
    <p class="muted">${m.qualification_remaining ?? Math.max(0, target - progress)} remaining. Qualification is tracked automatically by Arena if trades and reasoning ledgers are submitted.</p>
  `;

  const perfRows = [
    ["Attempted Orders", m.attempted_orders ?? m.total_orders ?? "N/A"],
    ["Total Trades", m.total_orders ?? "N/A"],
    ["Filled Trades", m.filled_trades ?? "N/A"],
    ["Rejected Trades", m.rejected_trades ?? 0],
    ["Qualification Status", m.qualification_status || "N/A"],
    ["Ladder Candidates", m.ladder_candidate_count ?? ladder.candidate_count ?? 0],
    ["Ladder Selected", m.ladder_selected_count ?? (ladder.selected_batch || []).length],
    ["Ladder Planned Spend", money(m.ladder_planned_spend_usdc ?? ladder.total_planned_spend ?? 0)],
    ["Ladder Winner/Value", `${m.ladder_winner_count ?? ladder.winner_ladder_count ?? 0}/${m.ladder_value_slot_count ?? ladder.value_slot_count ?? 0}`],
    ["Last Order Status", m.last_order_final_status || "N/A"],
    ["Last Reject Reason", m.last_order_rejection_reason || "N/A"],
    ["Settled Trades", m.settled_trade_count ?? 0],
    ["Wins", m.win_count ?? 0],
    ["Losses", m.loss_count ?? 0],
    ["Open Positions", m.open_positions ?? "N/A"],
    ["Latest Order Timestamp", m.latest_order_timestamp || "N/A"],
    ["Win Rate", m.win_rate === null || m.win_rate === undefined ? "Pending settlement" : pct(m.win_rate)],
    ["Realized PnL", m.realized_pnl_usdc === null || m.realized_pnl_usdc === undefined ? "Pending settlement" : money(m.realized_pnl_usdc)]
  ];
  document.getElementById("performance-summary").innerHTML = perfRows.map(([k, v]) =>
    `<div class="score-row"><span>${k}</span><strong>${v}</strong></div>`
  ).join("");

  const roi = m.roi === null || m.roi === undefined ? "N/A" : `${(Number(m.roi) * 100).toFixed(2)}%`;
  const capitalRows = [
    ["Starting Bankroll", money(m.starting_bankroll_usdc ?? 100)],
    ["Net Available (derived)", money(m.net_available_usdc)],
    ["Arena Reported Available", money(m.arena_reported_available_usdc)],
    ["Locked", money(m.locked_usdc)],
    ["Open Position Value", money(m.open_position_value_usdc)],
    ["Current Equity", money(m.current_equity_usdc)],
    ["Realized PnL", m.realized_pnl_usdc === null || m.realized_pnl_usdc === undefined ? "Pending settlement" : money(m.realized_pnl_usdc)],
    ["Unrealized PnL", money(m.unrealized_pnl_usdc)],
    ["ROI", roi]
  ];
  document.getElementById("capital-metrics").innerHTML = capitalRows.map(([k, v]) =>
    `<div class="setting"><span>${k}</span><strong>${v}</strong></div>`
  ).join("");

  const signalRows = [
    ["Average Fill Price", m.average_fill_price === null || m.average_fill_price === undefined ? "N/A" : fixed(m.average_fill_price, 6)],
    ["Average Edge", m.average_edge_pp === null || m.average_edge_pp === undefined ? "N/A" : `${Number(m.average_edge_pp).toFixed(2)} pp`],
    ["Average Confidence", m.average_confidence === null || m.average_confidence === undefined ? "N/A" : Number(m.average_confidence).toFixed(1)],
    ["Ledger Submissions", m.ledger_submissions ?? "N/A"],
    ["Reasoning Records", m.reasoning_records ?? "N/A"],
    ["Latest Refresh", state.live_arena?.last_refreshed ? new Date(state.live_arena.last_refreshed).toLocaleString() : "N/A"]
  ];
  document.getElementById("signal-metrics").innerHTML = signalRows.map(([k, v]) =>
    `<div class="setting"><span>${k}</span><strong>${v}</strong></div>`
  ).join("");
}

function renderQualificationInto(id) {
  const m = state.metrics || {};
  const target = m.qualification_target ?? 10;
  const progress = m.qualification_progress ?? 0;
  const pctDone = Math.min(100, (progress / target) * 100);
  const el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = `
    <div class="qual-head"><strong>${progress} / ${target}</strong><span class="status-pill ${progress >= target ? "ready" : progress >= 5 ? "warning" : ""}">${qualificationStatus(progress)}</span></div>
    <div class="progress"><span style="width:${pctDone}%"></span></div>
    <p class="muted">${m.qualification_status || "Status unknown"}. Estimated remaining: ${m.qualification_remaining ?? Math.max(0, target - progress)}. Rejected orders do not count toward qualification.</p>
    ${m.rejected_reconciliation_warning ? `<div class="note">${m.rejected_reconciliation_warning}</div>` : ""}
  `;
}

function renderTournament() {
  const t = state.web?.tournament || {};
  const data = t.data || {};
  const rounds = data.rounds || [];
  const rows = [
    ["Tournament Name", data.name || data.tournament_name || "N/A"],
    ["Status", data.status || "N/A"],
    ["Slug", data.slug || "N/A"],
    ["Start Time", dateTime(data.start_at)],
    ["End Time", dateTime(data.end_at)],
    ["Current Round", data.current_round || rounds[0]?.name || "N/A"],
    ["Round Count", Array.isArray(rounds) ? rounds.length : "N/A"]
  ];
  document.getElementById("tournament-card").innerHTML = t.ok
    ? rows.map(([k, v]) => `<div class="setting"><span>${k}</span><strong>${v}</strong></div>`).join("")
    : `<div class="note">Tournament API unavailable: ${t.error || "N/A"}</div>`;
  renderQualificationInto("qualification-home");
}

function fixtureTeams(f) {
  const homeObj = f.home_team || f.teams?.home || f.teams?.[0] || {};
  const awayObj = f.away_team || f.teams?.away || f.teams?.[1] || {};
  const home = homeObj.name || f.home || f.home_name || f.home_country || "Home";
  const away = awayObj.name || f.away || f.away_name || f.away_country || "Away";
  return [home, away];
}

function renderMatches() {
  const root = document.getElementById("matches-list");
  const filter = (document.getElementById("match-filter")?.value || "").toLowerCase();
  const limit = Number(document.getElementById("match-limit")?.value || 25);
  const fixtures = [...(state.web?.bracket?.fixtures || [])].sort((a, b) => Number(a.kickoff_at || a.starting_at || 0) - Number(b.kickoff_at || b.starting_at || 0));
  const rows = fixtures.filter(f => JSON.stringify(f).toLowerCase().includes(filter)).slice(0, limit);
  if (!state.web?.bracket?.ok) {
    root.innerHTML = `<div class="note">Bracket API unavailable: ${state.web?.bracket?.error || "N/A"}</div>`;
    return;
  }
  if (!rows.length) {
    root.innerHTML = `<div class="empty-state">No upcoming fixtures returned by bracket API.</div>`;
    return;
  }
  root.innerHTML = rows.map(f => {
    const [home, away] = fixtureTeams(f);
    const homeCode = f.home_team?.code || f.teams?.home?.code || "";
    const awayCode = f.away_team?.code || f.teams?.away?.code || "";
    const fid = f.fixture_id || f.id;
    const cmd = `python scripts\\agent_prematch.py --fixture ${fid} --model-variant ${state.runtime?.model_variant || "research_602020"}`;
    return `<div class="match-card">
      <div><strong>${home} ${homeCode ? `(${homeCode})` : ""} vs ${away} ${awayCode ? `(${awayCode})` : ""}</strong><small>${fid} · ${f._round_label || f.round?.name || f.round_name || f.round || "Round N/A"} · ${f.status || "N/A"} · ${dateTime(f.kickoff_at || f.starting_at)}</small></div>
      <code>${cmd}</code>
      <button class="copy-command" data-command="${cmd.replace(/"/g, "&quot;")}">Copy</button>
    </div>`;
  }).join("");
  root.querySelectorAll(".copy-command").forEach(btn => btn.addEventListener("click", () => navigator.clipboard?.writeText(btn.dataset.command)));
}

function renderOrderHistory() {
  const root = document.getElementById("orders-history");
  const orders = state.orders?.orders || [];
  if (!state.orders?.ok) {
    root.innerHTML = `<div class="note">${state.orders?.error || "Orders unavailable"}</div>`;
    return;
  }
  root.innerHTML = orders.length ? orders.map(o => `
    <div class="position">
      <strong>${o.order_id || "order"} · ${o.team_code}</strong>
      <span class="status-chip ${String(o.status || "").toLowerCase()}">${o.status || "N/A"}</span>
      <div class="position-grid">
        <div>Fixture<span>${na(o.fixture_id)}</span></div>
        <div>Outcome<span>${na(o.team_code)}</span></div>
        <div>Side<span>${o.side || "buy_yes (inferred)"}</span></div>
        <div>Filled Price<span>${fixed(o.open_avg_fill_price, 6)}</span></div>
        <div>Size<span>${fixed(o.usd_size_filled, 6)} USDC</span></div>
        <div>Window<span>${na(o.window)}</span></div>
        <div>Settlement<span>${na(o.settlement_stage || o.settlement_status)}</span></div>
        <div>Settled Outcome<span>${na(o.settled_outcome)}</span></div>
        <div>Realized PnL<span>${o.realized_pnl_usdc === null || o.realized_pnl_usdc === undefined ? "Pending" : fixed(o.realized_pnl_usdc, 6)}</span></div>
      </div>
    </div>`).join("") : `<div class="empty-state">No orders returned by Arena.</div>`;
}

function renderPredictionHistory() {
  const root = document.getElementById("prediction-history");
  const res = state.web?.recent_predictions || {};
  const data = res.data || {};
  const predictions = data.predictions || (Array.isArray(data) ? data : []);
  if (!res.ok) {
    root.innerHTML = `<div class="note">Recent predictions unavailable: ${res.error || "N/A"}</div>`;
    return;
  }
  if (!predictions.length) {
    root.innerHTML = `<div class="empty-state">No tournament predictions recorded yet.</div>`;
    return;
  }
  root.innerHTML = `<div class="score-row"><span>Wins</span><strong>${data.wins ?? "N/A"}</strong></div>
    <div class="score-row"><span>Losses</span><strong>${data.losses ?? "N/A"}</strong></div>` +
    predictions.map(p => `<div class="position"><strong>${p.fixture_id || p.match || "Prediction"}</strong><pre>${JSON.stringify(p, null, 2)}</pre></div>`).join("");
}

function renderConfidencePage() {
  const res = state.web?.confidence_series || {};
  const data = res.data || {};
  const series = data.series || data.confidence || (Array.isArray(data) ? data : []);
  const empty = document.getElementById("confidence-empty");
  if (!res.ok || !series.length) {
    empty.textContent = res.ok ? "Confidence history will appear after tournament predictions." : `Confidence API unavailable: ${res.error || "N/A"}`;
    drawBarChart("confidence-series-chart", "Confidence History", {});
    return;
  }
  empty.textContent = "";
  drawBarChart("confidence-series-chart", "Confidence History", Object.fromEntries(series.slice(-12).map((x, i) => [x.fixture_id || i + 1, x.confidence_score || x.confidence || 0])));
}

function renderCompetition() {
  const res = state.web?.leaderboard_stats || {};
  const data = res.data || {};
  const rows = [
    ["Board Status", data.board_status || "Leaderboard not live yet"],
    ["Active Agents", data.active_agents ?? "N/A"],
    ["Average Win Rate", data.avg_win_rate === undefined || data.avg_win_rate === null ? "N/A" : pct(data.avg_win_rate)],
    ["Reasoning Leader", data.reasoning_leader || "N/A"]
  ];
  document.getElementById("competition-stats").innerHTML = res.ok
    ? rows.map(([k, v]) => `<div class="setting"><span>${k}</span><strong>${v}</strong></div>`).join("")
    : `<div class="note">Leaderboard not live yet. ${res.error || ""}</div>`;
}

function renderHealth() {
  const health = state.health || {};
  document.getElementById("health-grid").innerHTML = Object.entries(health).map(([name, h]) => `
    <div class="health-card ${h.ok ? "good" : "warning"}">
      <span>${name}</span>
      <strong>${h.ok ? "OK" : "WARN"}</strong>
      <small>${h.latency_ms === null || h.latency_ms === undefined ? "latency N/A" : `${h.latency_ms} ms`}</small>
      ${h.error ? `<p>${h.error}</p>` : ""}
    </div>
  `).join("");
}

function renderDemo() {
  const metrics = state.metrics || {};
  const runtime = state.runtime || {};
  const positions = state.exposure?.positions || [];
  const livePosition = positions[0];
  document.getElementById("demo-title").textContent = "PolyStair";
  document.getElementById("demo-copy").textContent = "A data-grounded value discovery agent for World Cup prediction markets.";
  document.getElementById("demo-best").innerHTML = `
    <div class="mega">${runtime.mode || "LIVE"}</div>
    <div class="score-row"><span>Ledger</span><strong>${runtime.ledger || "CONNECTED"}</strong></div>
    <div class="score-row"><span>Execution</span><strong>${(runtime.order_execution_mode || "limit").toUpperCase()}</strong></div>
    <div class="score-row"><span>Filled Trades</span><strong>${metrics.filled_trades ?? 0}</strong></div>
    <div class="score-row"><span>Rejected Orders</span><strong>${metrics.rejected_trades ?? 0}</strong></div>
    <div class="score-row"><span>Settled Trades</span><strong>${metrics.settled_trade_count ?? 0}</strong></div>
    <div class="score-row"><span>Wins / Losses</span><strong>${metrics.win_count ?? 0} / ${metrics.loss_count ?? 0}</strong></div>
    <div class="score-row"><span>Win Rate</span><strong>${metrics.win_rate === null || metrics.win_rate === undefined ? "Pending" : pct(metrics.win_rate)}</strong></div>
    <div class="score-row"><span>Open Positions</span><strong>${metrics.open_positions ?? positions.length}</strong></div>
    <div class="score-row"><span>Net Available</span><strong>${money(metrics.net_available_usdc)}</strong></div>
    <div class="score-row"><span>Realized PnL</span><strong>${metrics.realized_pnl_usdc === null || metrics.realized_pnl_usdc === undefined ? "Pending" : money(metrics.realized_pnl_usdc)}</strong></div>
    <div class="score-row"><span>Unrealized PnL</span><strong>${money(metrics.unrealized_pnl_usdc)}</strong></div>
    <div class="score-row"><span>Qualification</span><strong>${metrics.qualification_progress ?? 0}/${metrics.qualification_target ?? 10} - ${metrics.qualification_status || "N/A"}</strong></div>
  `;
  document.getElementById("demo-risks").innerHTML = `
    <ul>
      <li>Blends Sportmonks, bookmaker consensus, and historical priors.</li>
      <li>Scans all three outcomes for model-vs-market mispricing.</li>
      <li>Trades only when edge, confidence, data quality, and source agreement pass gates.</li>
    </ul>
  `;
  document.getElementById("demo-performance").innerHTML = livePosition ? `
    <div class="score-row"><span>Live Position</span><strong>${livePosition.team_code} buy YES</strong></div>
    <div class="score-row"><span>Fixture</span><strong>${livePosition.fixture_id}</strong></div>
    <div class="score-row"><span>Value</span><strong>${money(livePosition.value_usdc)}</strong></div>
    <div class="score-row"><span>Avg Cost</span><strong>${fixed(livePosition.avg_cost_usdc, 6)}</strong></div>
    <div class="score-row"><span>Quantity</span><strong>${fixed(livePosition.quantity, 6)}</strong></div>
    <div class="score-row"><span>Unrealized PnL</span><strong>${money(livePosition.unrealized_pnl_usdc)}</strong></div>
  ` : `<div class="empty-state">No live position returned by Arena.</div>`;
}

function renderSystem() {
  document.getElementById("settings").innerHTML = Object.entries(state.env).map(([k, v]) =>
    `<div class="setting"><span>${k}</span><strong>${v ?? "not set"}</strong></div>`
  ).join("");
  const latest = state.latest_prematch || {};
  document.getElementById("latest-run").textContent = JSON.stringify({
    file: latest._file,
    fixture_id: latest.fixture_id,
    dry_run: latest.dry_run,
    prediction: latest.prediction,
    strategy: latest.strategy,
    ledger: latest.ledger,
    order: latest.order
  }, null, 2);
  document.getElementById("files").innerHTML = state.files.map(f =>
    `<div class="file"><span>${f.name}</span><small>${Math.round(f.size / 1024)} KB</small></div>`
  ).join("");
}

function drawBarChart(id, title, values, formatter = v => String(v)) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.font = "13px system-ui";
  ctx.fillStyle = "#9aa7b8";
  ctx.fillText(title, 14, 22);
  const entries = Object.entries(values);
  const max = Math.max(...entries.map(([, v]) => Math.abs(num(v))), 1);
  const width = (canvas.width - 34) / Math.max(entries.length, 1);
  entries.forEach(([label, value], i) => {
    const h = Math.abs(num(value)) / max * 98;
    const x = 16 + i * width;
    const y = 135 - h;
    ctx.fillStyle = num(value) >= 0 ? "#2bb673" : "#e05d5d";
    ctx.fillRect(x, y, Math.max(14, width - 12), h || 2);
    ctx.fillStyle = "#d8dee9";
    ctx.fillText(String(label).slice(0, 8), x, 162);
    ctx.fillText(formatter(value), x, Math.max(38, y - 6));
  });
}

function countBy(items, fn) {
  return items.reduce((acc, item) => {
    const key = fn(item) || "N/A";
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, {});
}

function drawAllCharts() {
  const all = rows();
  drawBarChart("edge-chart", "Edge Distribution", Object.fromEntries(all.slice(0, 8).map(r => [String(r.fixture_id).slice(-3), r.strategy.edge_pp])), v => `${num(v).toFixed(1)}`);
  drawBarChart("confidence-chart", "Confidence Distribution", countBy(all, r => r.prediction.confidence_level));
  drawBarChart("grade-chart", "Opportunity Grades", countBy(all, r => r.strategy.opportunity_class));
  drawBarChart("outcome-chart", "Prediction Outcomes", countBy(all, r => r.prediction.outcome));
  drawBarChart("trade-chart", "Trade vs No-Trade", countBy(all, r => r.strategy.should_trade ? "Trade" : "No"));
  drawBarChart("quality-chart", "Data Quality", Object.fromEntries(all.slice(0, 8).map(r => [String(r.fixture_id).slice(-3), r.prediction.data_quality || 0])), v => pct(v));
}

async function postAndShow(url) {
  const output = document.getElementById("command-output");
  output.textContent = "Running command...";
  const response = await fetch(url, { method: "POST" });
  const result = await response.json();
  output.textContent = [result.stdout, result.stderr].filter(Boolean).join("\n") || JSON.stringify(result, null, 2);
  await loadStatus();
  setPage("system");
}

document.querySelectorAll(".nav").forEach(btn => btn.addEventListener("click", () => setPage(btn.dataset.page)));
document.getElementById("refresh").addEventListener("click", loadStatus);
document.getElementById("refresh-interval").addEventListener("change", event => {
  refreshIntervalSeconds = Number(event.target.value);
  autoRefresh = refreshIntervalSeconds > 0;
  refreshCountdown = refreshIntervalSeconds || 0;
  renderLiveStatus();
  renderProductionStatus();
});
document.getElementById("screenshot-mode").addEventListener("click", () => {
  document.body.classList.toggle("screenshot");
});
document.getElementById("run-scan").addEventListener("click", () => postAndShow("/api/run-scan?limit=12"));
document.getElementById("filter").addEventListener("input", renderLeaderboard);
document.getElementById("match-filter")?.addEventListener("input", renderMatches);
document.getElementById("match-limit")?.addEventListener("change", renderMatches);
document.querySelectorAll(".sortbar button").forEach(btn => btn.addEventListener("click", () => {
  sortMode = btn.dataset.sort;
  renderLeaderboard();
}));
document.querySelectorAll("[data-export]").forEach(btn => btn.addEventListener("click", () => exportData(btn.dataset.export)));

loadStatus();

setInterval(() => {
  if (!autoRefresh || refreshIntervalSeconds <= 0) {
    renderLiveStatus();
    renderProductionStatus();
    return;
  }
  refreshCountdown -= 1;
  if (refreshCountdown <= 0) {
    loadStatus();
  } else {
    renderLiveStatus();
    renderProductionStatus();
  }
}, 1000);

function downloadJson(name, payload) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

function exportData(kind) {
  const m = state.metrics || {};
  if (kind === "performance") return downloadJson("polystair_performance.json", m);
  if (kind === "trades") return downloadJson("polystair_trade_history.json", state.orders?.orders || []);
  if (kind === "qualification") return downloadJson("polystair_qualification.json", {
    progress: m.qualification_progress,
    target: m.qualification_target,
    remaining: m.qualification_remaining,
    note: "Qualification is tracked automatically by Arena if trades and reasoning ledgers are submitted."
  });
  return downloadJson("polystair_dashboard_snapshot.json", state);
}
