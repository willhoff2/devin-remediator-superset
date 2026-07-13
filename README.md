# devin-remediator-superset

Event-driven automation that drains a codebase-migration backlog using
[Devin](https://devin.ai) as the remediation engine. Pointed at a fork of
[apache/superset](https://github.com/willhoff2/superset), it files scoped
GitHub issues for backlog work nobody will staff: the repo's own contributor
docs mandate a `describe()`→`test()` test migration (**208 files** carry the
TODO marker) plus `any`-type cleanup. It launches one budget-capped Devin
session per issue and reports outcomes on a status dashboard.

Why this workload and not CVEs or dependency bumps? Superset's pins are
intentionally frozen mid-SQLAlchemy-2.0 migration, and Dependabot already
owns the upgrade lane. Convention debt is the category that is verifiable
with the repo's own tooling, unbounded in supply, and staffed by no one.
Full decision history in [docs/spec.md](docs/spec.md).

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

- **Trigger**: polling (a sanctioned event type for this workflow: no public
  endpoint, nothing to break in a demo). `src/sources.py` keeps the
  `WebhookSource` seam where real-time GitHub webhooks slot in.
- **At-most-once dispatch**: the SQLite task row (PK = issue number) is
  created *before* the Devin session, so a crash mid-dispatch can strand a
  row (cleared by `scripts/reset_demo.py`) but never double-spend an issue.
- **Success signal**: Devin's `structured_output` (schema-enforced) + PR
  existence (the reported PR is fetched from GitHub and must be open or
  merged in the fork), not fork CI, which is disabled on forks and too slow
  to poll live. A flag-gated CI path (`CI_CHECKS_ENABLED`) polls PR check runs and
  retries exactly once via a follow-up session message.
- **Cost guardrails**: per-session `max_acu_limit` (5 default / 8 for
  `effort:medium` issues), `MAX_CONCURRENT_SESSIONS` gate, and the dashboard
  shows ACUs actual-vs-cap per task.

## Quickstart

Prerequisites (one-time, in the Devin console + GitHub):

1. A Devin org with a **service user** token (`cog_...`): Settings → create
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
  `data/events.jsonl` (and mirrored in SQLite); `LOG_FORMAT=json` for
  log-pipeline-ready stdout.
- **On GitHub**: every issue gets session-start and outcome comments;
  successes are relabeled `remediated-pending-merge` (the runner never closes
  issues; `Fixes #n` in the PR body closes them on merge). Terminal sessions
  are archived; Devin sessions never exit on their own. The verified
  lifecycle is documented in [docs/devin-platform.md](docs/devin-platform.md).

## Re-running the demo from scratch

Rehearsal runs accumulate state; `scripts/reset_demo.py` returns everything
to a fresh start:

```bash
python -m scripts.reset_demo        # dry run: prints the full plan
python -m scripts.reset_demo --yes  # close Devin PRs, delete devin/* branches,
                                    # DELETE remediation issues, wipe data/
```

Issues are deleted, not closed, because the scanner dedupes by file path
across issues in **all** states, so a closed rehearsal issue would block
re-filing forever. (Verified working with the runner's own fine-grained PAT
when its owner is the repo admin. Fallback if deletion ever fails: bump
`ISSUE_LABEL`/`DONE_LABEL` to fresh names, e.g. `devin-remediate-v2`; old
issues become invisible to scanner and dispatcher without touching them.)

The script never force-pushes: if a rehearsal PR was merged, it reports that
master moved off the frozen SHA and prints the reset command for you to run
deliberately. Simplest policy: **don't merge PRs during rehearsals**, so
master never moves and candidates stay valid. Devin sessions need no reset
(the monitor archives them; spent ACUs are spent either way).

## Development

Common tasks are wrapped in the Makefile: `make test`, `make lint` (ruff),
`make run` (docker compose), `make mock` (rehearsal against the local mock),
`make scan`, `make smoke`, `make reset`.

## Running tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/
```

The suite covers the store's idempotency, dispatcher dedupe/concurrency/ACU
guardrails and create-failure classification (reject/transient/ambiguous),
monitor success/failure finalization, dashboard state assembly, and scanner
filing dedupe, all against fake API clients. The real-API integration path
is `scripts/setup.py` (`validate` / `smoke`).

To rehearse the **full flow with zero Devin API traffic**, run against the
local mock (`scripts/mock_devin.py`), which enforces the verified
create-session payload contract (unknown keys 400 exactly like production;
the OpenAPI spec over-promises) and mimics the verified session lifecycle
(`running/working` → `running/finished`, never `exit`):

```bash
.venv/bin/uvicorn scripts.mock_devin:app --port 9095 &
DEVIN_API_BASE=http://127.0.0.1:9095 PR_VERIFY_ENABLED=false \
  POLL_INTERVAL_ISSUES=3 POLL_INTERVAL_SESSIONS=1 python -m src.main
```

(`PR_VERIFY_ENABLED=false` because the mock reports PR URLs that don't
exist on GitHub; the real pipeline verifies them.)

## Production hardening (next steps)

- **WebhookSource**: real-time dispatch from GitHub `issues` webhooks
  (HMAC-verified) instead of polling; the seam already exists.
- **Independent review loop**: Devin's native PR-review API
  (`POST /v3/.../pr-reviews`), already wired in behind
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
scripts/
  setup.py         # validate | playbook | smoke (real-API gates)
  mock_devin.py    # local v3 API mock for zero-cost flow rehearsal
  reset_demo.py    # return fork + local state to a fresh start
  session_dump.py  # dump a session's full JSON (debugging)
  session_msg.py   # send a message to a session (debugging)
playbook.md        # the Devin playbook (procedure, specs, forbidden actions)
candidates.yaml    # verified remediation candidates (static scanner mode)
docs/              # working spec + verified Devin platform notes
```
