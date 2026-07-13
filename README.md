# devin-remediator-superset

Event-driven automation that drains a codebase-migration backlog using
[Devin](https://devin.ai) as the remediation engine. Pointed at a fork of
[apache/superset](https://github.com/willhoff2/superset), it files scoped
GitHub issues for mechanical-but-unstaffable debt (the repo's own contributor
docs mandate a `describe()`→`test()` test migration — **208 files** carry the
TODO marker — plus `any`-type cleanup), launches one budget-capped Devin
session per issue, and reports outcomes on a dashboard an engineering leader
can read at a glance.

## Architecture

```
            ┌────────────────────── runner container ──────────────────────┐
            │                                                               │
 candidates │  Scanner ──files──► GitHub Issues (label: devin-remediate)    │
 .yaml or   │                          │                                    │
 --live-scan│  Dispatcher ◄──polls─────┘                                    │
            │      │  dedupe (SQLite PK) · concurrency gate · ACU cap       │
            │      └─────creates──► Devin session (playbook, tags,          │
            │                       structured_output_schema)               │
            │  Monitor ──polls sessions──► terminal state                   │
            │      │  structured_output = success signal                    │
            │      └── comments outcome on issue · relabels                 │
            │  Dashboard ── / (HTML) · /api/state (JSON)                    │
            │  State: SQLite tasks + JSONL event log                        │
            └───────────────────────────────────────────────────────────────┘
```

- **Trigger**: polling (a sanctioned event type for this workflow — no public
  endpoint, nothing to break in a demo). `src/sources.py` keeps the
  `WebhookSource` seam where real-time GitHub webhooks slot in.
- **Exactly-once dispatch**: the SQLite task row (PK = issue number) is
  created *before* the Devin session, so even a crash mid-dispatch can't
  double-spend an issue.
- **Success signal**: Devin's `structured_output` (schema-enforced) + PR
  existence — not fork CI, which is disabled on forks and too slow to poll
  live. A flag-gated CI path (`CI_CHECKS_ENABLED`) polls PR check runs and
  retries exactly once via a follow-up session message.
- **Cost guardrails**: per-session `max_acu_limit` (5 default / 8 for
  `effort:medium` issues), `MAX_CONCURRENT_SESSIONS` gate, and the dashboard
  shows ACUs actual-vs-cap per task.

## Quickstart

Prerequisites (one-time, in the Devin console + GitHub):

1. A Devin org with a **service user** token (`cog_...`) — Settings → create
   service user. Note your org ID from Settings → General.
2. The **Devin GitHub App** installed with access to your target fork, and
   the repo's machine setup/onboarding completed (a warm snapshot with
   `node_modules` is what keeps per-session cost sane on a repo this size).
3. A GitHub **PAT** with issues read/write + PR read on the fork.

Then:

```bash
cp .env.example .env        # fill in DEVIN_API_KEY, DEVIN_ORG_ID, GITHUB_TOKEN, GITHUB_REPO

# validate credentials + create the playbook (writes DEVIN_PLAYBOOK_ID to use in .env)
python -m scripts.setup validate
python -m scripts.setup playbook

# go/no-go gate: measures whether Devin can run jest + pre-commit from the
# warm snapshot in minutes. Run this before trusting anything downstream.
python -m scripts.setup smoke

# start the runner (dispatcher + monitor + dashboard on :8090)
docker compose up --build

# file the remediation issues (the "event" that feeds the pipeline)
python -m src.scanner              # static verified candidates (demo-safe)
python -m src.scanner --live-scan  # or: discover by grepping a fresh clone
python -m src.scanner --dry-run    # print what would be filed
```

Watch [http://localhost:8090](http://localhost:8090): issues appear, sessions
dispatch (capped at 3 concurrent), PRs land, the backlog bar moves.

## Observability — "how would I know this is working?"

- **Dashboard** (`/`): active / succeeded / failed counts, per-task
  issue → session → PR → duration → ACUs (vs cap), backlog progress
  (`N / 208` files remediated), live event feed.
- **`/api/state`**: the same as JSON, for anything downstream.
- **Structured logs**: every state transition is a JSONL event in
  `data/events.jsonl` (and mirrored in SQLite) — `LOG_FORMAT=json` for
  log-pipeline-ready stdout.
- **On GitHub**: every issue gets session-start and outcome comments;
  successes are relabeled `remediated-pending-merge` (the runner never closes
  issues — `Fixes #n` in the PR body closes them on merge). Finished sessions
  are archived (Devin sessions never exit on their own — see
  `docs/devin-platform.md` in the parent workspace for the verified
  lifecycle).

## Re-running the demo from scratch

Rehearsal runs accumulate state; `scripts/reset_demo.py` returns everything
to a fresh start:

```bash
python -m scripts.reset_demo        # dry run: prints the full plan
python -m scripts.reset_demo --yes  # close Devin PRs, delete devin/* branches,
                                    # DELETE remediation issues, wipe data/
```

Issues are deleted, not closed, because the scanner dedupes by file path
across issues in **all** states — a closed rehearsal issue would block
re-filing forever. (Verified working with the runner's own fine-grained PAT
when its owner is the repo admin. Fallback if deletion ever fails: bump
`ISSUE_LABEL`/`DONE_LABEL` to fresh names, e.g. `devin-remediate-v2` — old
issues become invisible to scanner and dispatcher without touching them.)

The script never force-pushes: if a rehearsal PR was merged, it reports that
master moved off the frozen SHA and prints the reset command for you to run
deliberately. Simplest policy: **don't merge PRs during rehearsals** — then
master never moves and candidates stay valid. Devin sessions need no reset
(the monitor archives them; spent ACUs are spent either way).

## Running tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/
```

The suite covers the store's idempotency, dispatcher dedupe/concurrency/ACU
guardrails, monitor success/failure finalization, dashboard state assembly,
and scanner filing dedupe — all against fake API clients. The real-API
integration path is `scripts/setup.py` (`validate` / `smoke`).

## Production hardening (next steps)

- **WebhookSource**: real-time dispatch from GitHub `issues` webhooks
  (HMAC-verified) instead of polling — the seam already exists.
- **Independent review loop**: Devin's native PR-review API
  (`POST /v3/.../pr-reviews`) — already wired in behind
  `DEVIN_REVIEW_ENABLED` (one call per successful PR); off by default for
  ACU economics until per-review cost is measured.
- **Devin Schedules** for periodic `--live-scan` sweeps; native Jira/Linear
  triggers where tickets originate outside GitHub.
- **Enterprise consumption API** for org-level ACU/cost dashboards.

## Repo layout

```
src/
  main.py          # asyncio entrypoint: dispatcher + monitor + dashboard
  config.py        # fail-fast env config
  sources.py       # EventSource seam: PollingSource / WebhookSource (stub)
  scanner.py       # event producer: candidates -> labeled GitHub issues
  dispatcher.py    # issues -> Devin sessions (dedupe, gates, ACU caps)
  monitor.py       # sessions -> outcomes (structured_output, CI retry path)
  dashboard.py     # HTML status page + /api/state
  devin_client.py  # Devin v3 Organization API (thin, typed, backoff)
  github_client.py # GitHub REST (thin, typed, backoff)
  store.py         # SQLite tasks + JSONL events
scripts/setup.py   # validate | playbook | smoke (real-API gates)
playbook.md        # the Devin playbook (procedure, specs, forbidden actions)
candidates.yaml    # verified remediation candidates (static scanner mode)
```
