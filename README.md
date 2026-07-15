# devin-remediator-superset

Event-driven automation that drains a codebase-migration backlog using
[Devin](https://devin.ai) as the remediation engine. Pointed at a fork of
[apache/superset](https://github.com/willhoff2/superset), it files scoped
GitHub issues for backlog work nobody will staff: the repo's own contributor
docs mandate a `describe()`→`test()` test migration (**208 files** carry the
TODO marker) plus `any`-type cleanup. It launches one budget-capped Devin
session per issue and reports outcomes on a status dashboard.

Superset's pins are intentionally frozen mid-SQLAlchemy-2.0 migration.
Dependabot already owns the upgrade lane, so CVEs were skipped. Convention
debt is the category that is verifiable with the repo's own tooling and
unbounded in supply.

## Architecture

```
  candidates.yaml (verified static)  or  --live-scan (shallow clone + grep)
                          │
                   ┌──────▼──────┐
                   │   Scanner   │  CLI, run on demand or scheduled ·
                   └──────┬──────┘  dedupes by file path · caps open issues
                          │
            [ GitHub Issues · label: devin-remediate ]
                          │
                          │  polls for the label
                   ┌──────▼──────┐
                   │ Dispatcher  │  at-most-once (SQLite row before session)
                   └──────┬──────┘  concurrency gate (3) · ACU cap (5 / 8)
                          │
                          │  creates: playbook · tags ·
                          │  structured-output schema · max_acu_limit
                          │
            [ Devin session · Devin's cloud, warm repo snapshot ]
              makes the change · verifies (jest + pre-commit)
                    before pushing · opens the PR
                          │
                          │  polls to terminal state
                   ┌──────▼──────┐
                   │   Monitor   │  success = structured_output.success
                   └──────┬──────┘  and the PR exists on the fork
                          │
                          │  comments outcome on issue · relabels ·
                          │  archives session
                          │
            [ Pull Request · "Fixes #n" closes the issue on merge ]

  every state transition ──►  SQLite tasks + JSONL event log
  Dashboard reads it ───────►  / (HTML) · /api/state (JSON)
```

Dispatcher, Monitor, and Dashboard run as asyncio tasks in one container
process. The Scanner is a separate CLI (`make scan`) to identify and create issues for Devin.

- **Trigger**: polling. `src/sources.py` keeps the
  `WebhookSource` seam where real-time GitHub webhooks slot in.
- **At-most-once dispatch**: the SQLite task row (PK = issue number) is
  created *before* the Devin session, so a crash mid-dispatch can strand a
  row (cleared by `scripts/reset_demo.py`) but never double-spend an issue.
  Session creation retries only 429 (no session existed) instead of
  risking a duplicate.
- **Success signal**: Devin's `structured_output` (schema-enforced) + PR
  existence (the reported PR is fetched from GitHub and must be open or
  merged in the fork), not fork CI, which is disabled on forks and too slow
  to poll live. A flag-gated CI path (`CI_CHECKS_ENABLED`) polls PR check runs and
  retries exactly once via a follow-up session message.
- **Cost guardrails**: every session carries a hard spend ceiling — $11.25
  default / $18.00 for `effort:medium` issues (5 / 8 ACUs via
  `max_acu_limit`) — plus a `MAX_CONCURRENT_SESSIONS` gate; the dashboard
  shows actual cost vs. cap per task.

## Quickstart

Prerequisites (one-time, in the Devin console + GitHub):

1. A Devin org with a **service user** token (`cog_...`): Settings → create
   service user. Note your org ID from Settings → General.
2. The **Devin GitHub App** installed with access to your target fork, and
   the repo's machine setup/onboarding completed (a warm snapshot with
   `node_modules` is what keeps per-session cost sane on a repo this size).
3. A GitHub **PAT** on the fork: issues read/write + PR read for the
   pipeline; add PR write + contents write if you want `make reset`
   (closing PRs, deleting branches) to work with the same token.

Then:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env        # fill in DEVIN_API_KEY, DEVIN_ORG_ID, GITHUB_TOKEN, GITHUB_REPO

# validate credentials + create the playbook (writes DEVIN_PLAYBOOK_ID to use in .env)
.venv/bin/python -m scripts.setup validate
.venv/bin/python -m scripts.setup playbook

# go/no-go gate: measures whether Devin can run jest + pre-commit from the
# warm snapshot in minutes. Run this before trusting anything downstream.
make smoke

# start the runner (dispatcher + monitor + dashboard on :8090)
make run

# file the remediation issues (the "event" that feeds the pipeline)
make scan                                    # pre-verified static candidates
.venv/bin/python -m src.scanner --live-scan  # or: discover by grepping a fresh clone
.venv/bin/python -m src.scanner --dry-run    # print what would be filed
```

Watch [http://localhost:8090](http://localhost:8090): issues appear, sessions
dispatch (capped at 3 concurrent), PRs land, the backlog bar moves.

## Verified run

The full pipeline has run end-to-end against the real Devin API: six issues
filed, six sessions dispatched, six PRs opened, zero failures. Each PR stayed
within the file named in its issue and reports the checks run in-session:

| Issue | PR                                                  | Change                                                    |
| ----- | --------------------------------------------------- | --------------------------------------------------------- |
| #86   | [#97](https://github.com/willhoff2/superset/pull/97) | `getLeafComponentIdFromPath.test.ts` off `describe()` |
| #87   | [#95](https://github.com/willhoff2/superset/pull/95) | `parseCookie.test.ts` off `describe()`                |
| #88   | [#94](https://github.com/willhoff2/superset/pull/94) | `newQueryTabName.test.ts` off `describe()`            |
| #89   | [#92](https://github.com/willhoff2/superset/pull/92) | `findParentId.test.ts` off `describe()`               |
| #90   | [#93](https://github.com/willhoff2/superset/pull/93) | `home/types.ts`: `Array<any>` -> `string[]`         |
| #91   | [#96](https://github.com/willhoff2/superset/pull/96) | `standardizedFormData.ts`: remove explicit `any`      |

## Observability — "how would I know this is working?"

- **Dashboard** (`/`): active / succeeded / failed counts, estimated spend
  and estimated savings, per-task issue → session → PR → duration →
  cost vs. cap, backlog progress (`N / 208` files remediated), live event
  feed.
- **`/api/state`**: the same as JSON, for anything downstream.
- **Structured logs**: every state transition is a JSONL event in
  `data/events.jsonl` (and mirrored in SQLite); `LOG_FORMAT=json` for
  log-pipeline-ready stdout.
- **On GitHub**: every issue gets session-start and outcome comments;
  successes are relabeled `remediated-pending-merge` (the runner never closes
  issues; `Fixes #n` in the PR body closes them on merge). Terminal sessions
  are archived; Devin sessions never exit on their own (verified against
  the real API; see `session_reached_outcome` in `src/devin_client.py`).

## Re-running the demo from scratch

Rehearsal runs accumulate state; `scripts/reset_demo.py` returns everything
to a fresh start:

```bash
make reset            # dry run: prints the full plan
make reset ARGS=--yes # close Devin PRs, delete devin/* branches,
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

The dashboard's interactive behavior (card filters, popovers, collapse) is
outside pytest's reach; `node scripts/verify_dashboard_ui.mjs` drives it in
headless Chrome against a running server and exits non-zero on any failed
check (`CHROME_BIN` overrides the browser path off macOS).

## Running tests

```bash
make test
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
make mock
```

Note: only the Devin side is mocked. `make mock` still uses the real
GitHub API, so it posts comments and labels on the fork's issues (the
reset script clears them).

(It sets `PR_VERIFY_ENABLED=false` because the mock reports PR URLs that
don't exist on GitHub; the real pipeline verifies them.)

## Production hardening (next steps)

- **Your infrastructure**: one outbound-only container, two secrets;
  harden with Postgres, a secret manager, and a GitHub App token.
- **WebhookSource**: real-time dispatch from GitHub `issues` webhooks
  (HMAC-verified) instead of polling; the seam already exists.
- **Independent review loop**: Devin's native PR-review API
  (`POST /v3/.../pr-reviews`), already wired in behind
  `DEVIN_REVIEW_ENABLED` (one call per successful PR); off by default for
  ACU economics until per-review cost is measured.
- **Devin Schedules** for periodic `--live-scan` sweeps; native Jira/Linear
  triggers where tickets originate outside GitHub.
- **Enterprise consumption API** for org-level ACU/cost dashboards.
- **Failure alerting**: Slack webhook or equivalent on `task_failed`
  events to notify the team.
- **Reconcile-by-tag**: recover ambiguous session creates via their
  `gh-issue-N` tag; the tag filter is already verified against the real
  API (`scripts/probe_tag_filter.py`).

## Repo layout

```
src/
  main.py          # asyncio entrypoint: dispatcher + monitor + dashboard
  config.py        # fail-fast env config
  sources.py       # EventSource seam: PollingSource / WebhookSource (stub)
  scanner.py       # event producer: candidates -> labeled GitHub issues
  dispatcher.py    # issues -> Devin sessions (dedupe, gates, ACU caps)
  monitor.py       # sessions -> outcomes (structured_output, CI retry path)
  dashboard.py     # status page + /api/state
  static/          # dashboard assets (html/css/js)
  devin_client.py  # Devin v3 Organization API (thin, typed, backoff)
  github_client.py # GitHub REST (thin, typed, backoff)
  store.py         # SQLite tasks + JSONL events
scripts/
  setup.py         # validate | playbook | smoke (real-API gates)
  mock_devin.py    # local v3 API mock for zero-cost flow rehearsal
  reset_demo.py    # return fork + local state to a fresh start
  session_dump.py  # dump a session's full JSON (debugging)
  session_msg.py   # send a message to a session (debugging)
  verify_dashboard_ui.mjs  # headless-Chrome check of dashboard interactions
playbook.md        # the Devin playbook (procedure, specs, forbidden actions)
candidates.yaml    # verified remediation candidates (static scanner mode)
```
