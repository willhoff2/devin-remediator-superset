"""Leader-facing status page + JSON state API.

Answers the engineering-leader question directly: what's in flight, what
succeeded/failed, how long each fix took, and what it cost (ACUs actual vs
cap), plus progress against the known migration backlog.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .config import Config
from .store import ACTIVE_STATUSES, Store, TaskStatus


def _task_view(task: dict[str, Any]) -> dict[str, Any]:
    end = task["completed_at"] or time.time()
    duration = int(end - task["dispatched_at"]) if task["dispatched_at"] else None
    return {**task, "duration_s": duration}


def build_state(store: Store, cfg: Config) -> dict[str, Any]:
    tasks = [_task_view(t) for t in store.list_tasks()]
    succeeded = [t for t in tasks if t["status"] == TaskStatus.SUCCEEDED]
    return {
        "generated_at": time.time(),
        "repo": cfg.github_repo,
        "summary": {
            "active": sum(1 for t in tasks if t["status"] in ACTIVE_STATUSES),
            "succeeded": len(succeeded),
            "failed": sum(1 for t in tasks if t["status"] == TaskStatus.FAILED),
            "total_acus": round(
                sum(t["acus_consumed"] or 0 for t in tasks), 2
            ),
            "backlog_done": len(succeeded),
            "backlog_total": cfg.backlog_total,
            "ci_checks_enabled": cfg.ci_checks_enabled,
        },
        "tasks": tasks,
        "events": store.recent_events(25),
    }


_PAGE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Devin Remediator — {repo}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font: 14px/1.5 -apple-system, "Segoe UI", sans-serif; margin: 0;
         background: #0d1117; color: #e6edf3; }}
  header {{ padding: 20px 28px 0; }}
  h1 {{ font-size: 18px; margin: 0 0 2px; }}
  h1 small {{ color: #8b949e; font-weight: normal; }}
  main {{ padding: 16px 28px 40px; max-width: 1200px; }}
  .cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0 24px; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
           padding: 12px 18px; min-width: 120px; }}
  .card .n {{ font-size: 26px; font-weight: 600; }}
  .card .l {{ color: #8b949e; font-size: 12px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #21262d;
            white-space: nowrap; }}
  td.title {{ white-space: normal; }}
  th {{ color: #8b949e; font-size: 12px; text-transform: uppercase; }}
  a {{ color: #58a6ff; text-decoration: none; }}
  .badge {{ padding: 2px 9px; border-radius: 12px; font-size: 12px; }}
  .succeeded {{ background: #1a7f37; color: #fff; }}
  .failed {{ background: #b62324; color: #fff; }}
  .session_running, .dispatched, .retrying {{ background: #9e6a03; color: #fff; }}
  .issue_filed {{ background: #30363d; }}
  .bar {{ background: #21262d; border-radius: 6px; height: 10px; width: 260px;
          display: inline-block; vertical-align: middle; }}
  .bar i {{ display: block; height: 100%; border-radius: 6px; background: #1f6feb; }}
  #events {{ margin-top: 28px; color: #8b949e; font-size: 12px;
             font-family: ui-monospace, monospace; }}
  #events div {{ padding: 2px 0; }}
</style>
</head>
<body>
<header>
  <h1>Devin Remediator <small>— {repo}</small></h1>
  <div class="l" id="updated" style="color:#8b949e;font-size:12px"></div>
</header>
<main>
  <div class="cards" id="cards"></div>
  <div>Backlog (describe-migration files): <span class="bar"><i id="bar"></i></span>
       <span id="backlog"></span></div>
  <table>
    <thead><tr>
      <th>Issue</th><th class="title">Title</th><th>Category</th><th>Status</th>
      <th>Session</th><th>PR</th><th id="cihead" hidden>CI</th>
      <th>Duration</th><th>ACUs (cap)</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div id="events"></div>
</main>
<script>
const fmt = s => s == null ? "—" : (s < 90 ? s + "s" : Math.round(s/60) + "m");
async function refresh() {{
  const state = await (await fetch("/api/state")).json();
  const s = state.summary;
  document.getElementById("cards").innerHTML = [
    ["Active", s.active], ["Succeeded", s.succeeded], ["Failed", s.failed],
    ["Total ACUs", s.total_acus]
  ].map(([l, n]) => `<div class="card"><div class="n">${{n}}</div><div class="l">${{l}}</div></div>`).join("");
  document.getElementById("bar").style.width = (100 * s.backlog_done / s.backlog_total) + "%";
  document.getElementById("backlog").textContent = `${{s.backlog_done}} / ${{s.backlog_total}} remediated`;
  document.getElementById("cihead").hidden = !s.ci_checks_enabled;
  document.getElementById("rows").innerHTML = state.tasks.map(t => `<tr>
    <td><a href="${{t.issue_url}}">#${{t.issue_number}}</a></td>
    <td class="title">${{t.title}}</td><td>${{t.category}}</td>
    <td><span class="badge ${{t.status}}">${{t.status}}</span></td>
    <td>${{t.session_url ? `<a href="${{t.session_url}}">session</a>` : "—"}}</td>
    <td>${{t.pr_url ? `<a href="${{t.pr_url}}">PR</a>` : "—"}}</td>
    ${{s.ci_checks_enabled ? `<td>${{t.ci_status ?? "—"}}</td>` : ""}}
    <td>${{fmt(t.duration_s)}}</td>
    <td>${{t.acus_consumed ?? "—"}} (${{t.acu_cap}})</td>
  </tr>`).join("");
  document.getElementById("events").innerHTML = state.events.map(e =>
    `<div>${{new Date(e.ts * 1000).toLocaleTimeString()}} — ${{e.event}}` +
    `${{e.issue_number ? " #" + e.issue_number : ""}}</div>`).join("");
  document.getElementById("updated").textContent =
    "updated " + new Date(state.generated_at * 1000).toLocaleTimeString();
}}
refresh(); setInterval(refresh, 5000);
</script>
</body>
</html>
"""


def build_app(store: Store, cfg: Config) -> FastAPI:
    app = FastAPI(title="devin-remediator", docs_url=None, redoc_url=None)

    @app.get("/api/state")
    def api_state() -> dict[str, Any]:
        return build_state(store, cfg)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _PAGE.format(repo=cfg.github_repo)

    return app
