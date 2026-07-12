# Playbook: Remediate a scoped Superset issue

## Overview

You are remediating exactly one narrowly-scoped GitHub issue in the repository
named in the prompt. The issue body names the exact file(s) and the required
change. Your job is the minimal correct fix, verified with the repository's
own tooling, delivered as a small reviewable PR.

## Procedure

1. Read the GitHub issue referenced in the prompt. The issue body names the
   exact file(s), the required change, and the verification commands.
2. Create a branch named `devin/issue-<number>-<short-slug>` off the default
   branch.
3. Make the minimal change the issue describes. Touch only the file(s) named
   in the issue. For test-migration issues, also delete the stale
   `eslint-disable-next-line no-restricted-globals` TODO comment in the same
   file.
4. Run the verification commands listed in the issue. At minimum:
   - the affected jest test file(s): `npm run test -- <file>` from
     `superset-frontend/`
   - `pre-commit run --files <each touched file>` from the repository root
5. Fix any failures and re-run until everything passes.
6. Open a pull request against the default branch of the same repository
   (not upstream apache/superset). The PR title must follow Conventional
   Commits, e.g. `test(dashboard): migrate X test off describe() block` or
   `refactor(home): ...`. The PR body must contain `Fixes #<issue-number>`
   and a checklist of the verification commands you ran with their results.
7. Report the outcome via structured output.

## Specifications

- The PR diff stays strictly within the file(s) named in the issue.
- Every verification command listed in the issue passes before the PR is
  opened.
- The PR body references the originating issue with `Fixes #<number>`.
- Structured output is filled truthfully: `success: true` only if the PR is
  open and all checks passed.

## Advice and Pointers

- The repository's contributor conventions live in `CLAUDE.md` at the repo
  root; the test-style convention is "use `test()` instead of `describe()`".
- When unwrapping a `describe()` block, hoist any describe-scoped `const`
  setup to module scope and dedent the `test()` calls; preserve `beforeEach`
  and mock setup semantics exactly.
- If pre-commit's whole-project type-check hook is prohibitively slow, run the
  fast hooks on the touched files and note it in structured output `notes`.

## Forbidden Actions

- Never push without the verification commands passing on the touched files.
- Never modify files not named in the issue.
- Never force-push, never push directly to the default branch.
- Never open a PR against upstream apache/superset.
- If the fix cannot be completed as scoped (e.g. it genuinely requires
  touching other files), do NOT expand scope: stop, report
  `success: false` with the reason in `blockers`, and leave the branch
  unpushed.
