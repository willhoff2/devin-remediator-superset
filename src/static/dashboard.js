const fmtDur = s => s == null ? "—"
  : s < 90 ? s + "s"
  : s < 5400 ? Math.round(s / 60) + "m"
  : Math.round(s / 3600) + "h";
const money = (v, estimated) => (estimated ? "~$" : "$") + v.toFixed(2);
// Issue titles etc. come from GitHub and are attacker-influenceable.
const esc = s => String(s).replace(/[&<>"']/g,
  c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

// Mirrors store.ACTIVE_STATUSES.
const ACTIVE = ["dispatched", "session_running", "retrying"];
let state = null;
let filter = null; // null | "active" | "succeeded" | "failed"

const matches = t => filter == null
  || (filter === "active" ? ACTIVE.includes(t.status) : t.status === filter);

function render() {
  const summary = state.summary;
  document.title = `Devin Remediator — ${state.repo}`;
  document.getElementById("repo").textContent = state.repo;

  const cards = [
    { label: "Active", value: summary.active, key: "active" },
    { label: "Succeeded", value: summary.succeeded, key: "succeeded" },
    { label: "Failed", value: summary.failed, key: "failed" },
    { label: `${summary.cost_estimated ? "Est. cost" : "Cost"} <button class="info"
        data-pop="costPop" aria-expanded="false" aria-label="How cost is estimated">&#9432;</button>`,
      value: money(summary.total_cost_usd, summary.cost_estimated) },
    { label: `Est. savings <button class="info" data-pop="savedPop"
        aria-expanded="false" aria-label="How savings are estimated">&#9432;</button>`,
      value: summary.est_saved_usd > 0
        ? `~$${Math.round(summary.est_saved_usd)}<span class="sub"> / ~${fmtDur(summary.saved_time_s)}</span>`
        : "—" },
  ];
  document.getElementById("cards").innerHTML = cards.map(({ label, value, key }) => {
    const selected = key !== undefined && key === filter; // money cards have no key
    return `<div class="card${key ? ` ${key}` : ""}${selected ? " sel" : ""}"
                 ${key ? `data-filter="${key}"` : ""}>
      <div class="n">${value}</div><div class="l label">${label}</div>
    </div>`;
  }).join("");

  const backlogPct = 100 * summary.backlog_done / summary.backlog_total;
  document.getElementById("bar").style.width = backlogPct + "%";
  document.getElementById("backlog-done").textContent = summary.backlog_done;
  document.getElementById("backlog-total").textContent = summary.backlog_total;
  document.getElementById("backlog-pct").textContent = backlogPct.toFixed(1) + "%";
  const extra = summary.succeeded - summary.backlog_done;
  document.getElementById("backlog-extra").textContent =
    extra > 0 ? `+${extra} succeeded outside backlog` : "";

  document.getElementById("cihead").hidden = !summary.ci_checks_enabled;
  const tasks = state.tasks.filter(matches);
  document.getElementById("rows").innerHTML = tasks.length ? tasks.map(t => `<tr>
    <td class="title"><a href="${esc(t.issue_url)}">${esc(t.title)}</a></td>
    <td>${esc(t.category)}</td>
    <td><span class="badge ${esc(t.status)}">${esc(t.status).replace(/_/g, " ")}</span></td>
    <td>${t.session_url ? `<a href="${esc(t.session_url)}">session</a>` : "—"}</td>
    <td>${t.pr_url ? `<a href="${esc(t.pr_url)}">PR</a>` : "—"}</td>
    ${summary.ci_checks_enabled ? `<td>${t.ci_status ?? "—"}</td>` : ""}
    <td class="num">${fmtDur(t.duration_s)}</td>
    <td class="num">${t.cost_usd == null ? "—" : money(t.cost_usd, t.cost_basis === "estimated")}<span class="cap"> / ${money(t.cost_cap_usd, false)} cap</span></td>
  </tr>`).join("")
    : `<tr><td class="empty" colspan="${summary.ci_checks_enabled ? 8 : 7}">No ${filter ? esc(filter) + " " : ""}tasks</td></tr>`;

  document.getElementById("events").innerHTML = state.events.map(e =>
    `<div><span class="t">${new Date(e.ts * 1000).toLocaleTimeString()}</span>${esc(e.event)}` +
    `${e.issue_number ? " #" + esc(e.issue_number) : ""}</div>`).join("");
  document.getElementById("updated").textContent =
    "updated " + new Date(state.generated_at * 1000).toLocaleTimeString();
}

document.getElementById("cards").addEventListener("click", e => {
  const card = e.target.closest("[data-filter]");
  if (!card) return;
  filter = filter === card.dataset.filter ? null : card.dataset.filter;
  render();
});

// One delegated handler serves every ⓘ popover; the ⓘ inside the saved
// card survives the 5s innerHTML rebuild because nothing binds to it.
document.addEventListener("click", e => {
  const btn = e.target.closest("[data-pop]");
  for (const pop of document.querySelectorAll(".pop")) {
    const isTarget = btn && btn.dataset.pop === pop.id;
    if (isTarget && pop.hidden) {
      pop.hidden = false;
      const btnRect = btn.getBoundingClientRect();
      pop.style.top = (btnRect.bottom + 8) + "px";
      pop.style.left = Math.max(12, btnRect.right - pop.offsetWidth) + "px";
      btn.setAttribute("aria-expanded", "true");
    } else if (isTarget || !pop.contains(e.target)) {
      pop.hidden = true;
      if (isTarget) btn.setAttribute("aria-expanded", "false");
    }
  }
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    for (const pop of document.querySelectorAll(".pop")) pop.hidden = true;
  }
});

async function refresh() {
  state = await (await fetch("/api/state")).json();
  render();
}
refresh();
setInterval(refresh, 5000);
