const controls = {
  limit: document.getElementById("limit"),
  sort: document.getElementById("sort"),
  quoteCoin: document.getElementById("quoteCoin"),
  minHoldVol: document.getElementById("minHoldVol"),
  minVolume24: document.getElementById("minVolume24"),
  minAbsFundingPct: document.getElementById("minAbsFundingPct"),
  symbolQuery: document.getElementById("symbolQuery"),
  groupBy: document.getElementById("groupBy"),
  onlyEnabled: document.getElementById("onlyEnabled"),
  includeApiBlocked: document.getElementById("includeApiBlocked"),
  pollSec: document.getElementById("pollSec"),
  refreshBtn: document.getElementById("refreshBtn"),
};

const rowsBody = document.getElementById("rowsBody");
const statsText = document.getElementById("statsText");
const sourceText = document.getElementById("sourceText");

let timer = null;

function fmt(n, max = 2) {
  if (n == null) return "-";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: max });
}

function formatTimeToFunding(seconds) {
  if (seconds == null || Number.isNaN(Number(seconds))) return "-";
  const s = Math.max(0, Number(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h ${String(m).padStart(2, "0")}m`;
}

function formatSettle(ts) {
  if (!ts) return "-";
  return new Date(ts).toISOString().replace("T", " ").slice(0, 19);
}

function buildQuery() {
  const params = new URLSearchParams();
  params.set("limit", String(controls.limit.value || 60));
  params.set("sort", String(controls.sort.value || "abs_desc"));
  params.set("quote_coin", String(controls.quoteCoin.value || "USDT"));
  params.set("min_hold_vol", String(controls.minHoldVol.value || 0));
  params.set("min_volume24_usd", String(controls.minVolume24.value || 0));
  params.set("min_abs_funding_pct", String(controls.minAbsFundingPct.value || 0));
  if (String(controls.symbolQuery.value || "").trim()) {
    params.set("symbol_query", String(controls.symbolQuery.value).trim());
  }
  params.set("only_enabled", String(Boolean(controls.onlyEnabled.checked)));
  params.set("include_api_blocked", String(Boolean(controls.includeApiBlocked.checked)));
  return params.toString();
}

function buildRow(row) {
  const tr = document.createElement("tr");
  const rateCls = row.funding_rate_pct >= 0 ? "rate-pos" : "rate-neg";
  tr.innerHTML = `
    <td>${row.symbol}</td>
    <td class="${rateCls}">${Number(row.funding_rate_pct).toFixed(4)}%</td>
    <td>${fmt(row.hold_vol)}</td>
    <td>$${fmt(row.volume24_usd, 0)}</td>
    <td>${formatTimeToFunding(row.time_to_funding_sec)}</td>
    <td>${formatSettle(row.next_settle_time)}</td>
  `;
  return tr;
}

function groupKey(row, mode) {
  switch (mode) {
    case "sign":
      return row.funding_rate_pct >= 0 ? "Positive funding" : "Negative funding";
    case "quote_coin":
      return row.quote_coin || "Unknown quote";
    case "settle_window": {
      const sec = Number(row.time_to_funding_sec);
      if (!Number.isFinite(sec)) return "No settle time";
      if (sec < 3600) return "< 1 hour";
      if (sec < 4 * 3600) return "1-4 hours";
      if (sec < 8 * 3600) return "4-8 hours";
      return "> 8 hours";
    }
    default:
      return "All";
  }
}

function renderRows(rows) {
  rowsBody.innerHTML = "";
  const mode = String(controls.groupBy.value || "none");

  if (mode === "none") {
    for (const row of rows) rowsBody.appendChild(buildRow(row));
    return;
  }

  const groups = new Map();
  for (const row of rows) {
    const key = groupKey(row, mode);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }

  for (const [key, bucket] of groups.entries()) {
    const hdr = document.createElement("tr");
    hdr.className = "group-row";
    hdr.innerHTML = `<td colspan="6">${key} • ${bucket.length}</td>`;
    rowsBody.appendChild(hdr);
    for (const row of bucket) rowsBody.appendChild(buildRow(row));
  }
}

async function refreshData() {
  statsText.textContent = "Loading...";
  try {
    const query = buildQuery();
    const res = await fetch(`api/v1/funding/top?${query}`);
    const data = await res.json();
    if (!res.ok || data.success !== true) {
      throw new Error(data.detail || data.error || `HTTP ${res.status}`);
    }

    const rows = Array.isArray(data.rows) ? data.rows : [];
    renderRows(rows);
    statsText.textContent = `Returned ${data.stats.returned} / ${data.stats.totalAfterFilter} rows`;

    const tickerUpdated = data.source?.tickerUpdatedAtMs
      ? new Date(data.source.tickerUpdatedAtMs).toISOString().replace("T", " ").slice(0, 19)
      : "-";
    sourceText.textContent = `MEXC cache: ticker @ ${tickerUpdated} | lastError: ${data.source?.lastError || "none"}`;
  } catch (err) {
    rowsBody.innerHTML = "";
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="6">${String(err)}</td>`;
    rowsBody.appendChild(tr);
    statsText.textContent = "Failed to load";
    sourceText.textContent = "";
  }
}

function restartPolling() {
  if (timer) clearInterval(timer);
  const pollSec = Math.max(2, Math.min(120, Number(controls.pollSec.value || 8)));
  timer = setInterval(refreshData, pollSec * 1000);
}

for (const key of ["limit", "sort", "quoteCoin", "minHoldVol", "minVolume24", "minAbsFundingPct", "symbolQuery", "groupBy", "onlyEnabled", "includeApiBlocked", "pollSec"]) {
  const el = controls[key];
  el.addEventListener("change", () => {
    if (key === "pollSec") restartPolling();
    refreshData();
  });
}

controls.refreshBtn.addEventListener("click", async () => {
  await fetch("api/v1/refresh", { method: "POST" }).catch(() => {});
  refreshData();
});

restartPolling();
refreshData();
