# End-to-end rehearsal

`scripts/rehearse.sh` runs the whole night shift once, for real, against a new private sandbox repo, and
checks each coordination feature with a PASS/FAIL line and evidence. Run it before a release and after any
change to the lane, picker or publish code.

## What it proves

| Step | Feature | Proven when |
|---|---|---|
| a | A plain repo with CI (job `test`) is enough to start | the sandbox exists and CI is green on `main` |
| b | `gh nightshift init` writes the files, labels, settings, variables, protection, night log and secrets | `init` prints the lines below, with no `WARNING` you did not expect |
| c | The planner validates a plan, assigns IDs, writes `Blocked by:` and creates issues in dependency order | three issues `[A1]`, `[W1]`, `[W2]`; `[W1]` says `Blocked by: #<A1>` |
| d | One lane builds, publishes, passes CI and the review gate, merges, and hands off across lanes | `A's PR merged`, `hand-off ... posted`, `picked with the hand-off in its task file` all PASS |
| e | An agent blocked on another lane files a ticket there instead of building it, and its issue waits | `forced need ... filed #N in lane api` PASS |
| f | The kill switch works | `night shift paused` PASS |

Ticket roles: **A** = `[A1]` (lane `api`), **B** = `[W1]` (lane `web`, blocked by A),
**C** = `[W2]` (lane `web`, forces a need in lane `api`). Lane `web`'s ID prefix is `W` (the first letter
of the lane name, unless `planner.id_prefixes` says otherwise).

## Prerequisites

- `gh` logged in as the repo owner, with the classic `repo` and `workflow` scopes (pushing
  `.github/workflows/*` needs `workflow`): `gh auth refresh -s workflow`. This is *your* token, not the PAT.
- The extension: `gh extension install paawandesai/gh-nightshift`, or from this checkout `gh extension install .`,
  or `NS="$PWD/gh-nightshift" scripts/rehearse.sh ...`.
- The tool repo must be reachable from the sandbox's Actions: `paawandesai/gh-nightshift` public, and the ref
  you pass as `--tool-ref` pushed there. Until v0.1 is merged, use `--tool-ref build/v0.1` (the default `main`
  only works once the reusable workflows are on `main`).
- Four secrets. `init` prompts for each missing one with a hidden prompt and stores it with `gh secret set`.
  The names are the ones in the generated caller workflows:

  | Secret | Used by | Value |
  |---|---|---|
  | `AGENT_PAT` | `night-shift.yml` (`agent_pat`), `night-shift-planner.yml` | fine-grained PAT for the sandbox only: **Contents, Issues, Pull requests: Read and write; Actions: Read; Workflows: No access** (metadata read is automatic) |
  | `CLAUDE_TOKEN_API` | `night-shift.yml` job `lane-api`; planner | `claude setup-token` of the api lane owner |
  | `CLAUDE_TOKEN_WEB` | `night-shift.yml` job `lane-web` | `claude setup-token` of the web lane owner (can be the same person for the rehearsal) |
  | `CLAUDE_TOKEN_REVIEW` | `night-shift-review.yml` | `claude setup-token`, ideally someone who does not own a lane |

- Actions minutes on the account (private repos use the plan's minutes; see Timing and cost).
- Branch protection on a private repo needs GitHub Pro, Team or Enterprise. On a free plan `init` prints a
  `WARNING: branch protection failed` line; the rehearsal still works because the picker itself waits for
  every check in `required_checks` before merging, but `main` is then not protected.

## How to run

```bash
scripts/rehearse.sh paawandesai/ns-sandbox                                    # dry run: prints everything, runs nothing
scripts/rehearse.sh --live --tool-ref build/v0.1 paawandesai/ns-sandbox        # the real rehearsal
NS_AGENT_PAT=github_pat_... scripts/rehearse.sh --live ... paawandesai/ns-sandbox   # also let doctor check the PAT
scripts/rehearse.sh --live --from d --workdir /tmp/ns-rehearsal.AbC123 paawandesai/ns-sandbox  # resume at step d
```

Dry run is the default. It touches neither the network nor the disk: it prints each command (`+ ...`), each
file it would write, the plan JSON and the REST calls each check would make, with `<A>`, `<B>`, `<C>` in place
of issue numbers. `--live` creates the repo, stores secrets and starts paid agent runs. Each live wait polls
every 30 s for up to `--timeout` minutes (default 60) and dispatches the lane again every few minutes, so a
missed trigger does not stall the run. The script exits non-zero if any check failed, and never deletes anything.

Dry-run head (real output):

```text
DRY RUN: printing the commands for a rehearsal against paawandesai/ns-sandbox; nothing is executed.
Run again with --live to do it for real.

==== a. create the private sandbox paawandesai/ns-sandbox and push a tiny Python project ====
+ gh repo create paawandesai/ns-sandbox --private --description gh-nightshift\ rehearsal\ sandbox\ \(safe\ to\ delete\)
+ mkdir -p /tmp/ns-rehearsal.XXXXXX
+ gh repo clone paawandesai/ns-sandbox /tmp/ns-rehearsal.XXXXXX/ns-sandbox
[dry-run] would write README.md (3 lines)
[dry-run] would write api/__init__.py (1 lines)
...
[dry-run] would write .github/workflows/ci.yml (21 lines)
+ (cd /tmp/ns-rehearsal.XXXXXX/ns-sandbox && git add -A)
+ (cd /tmp/ns-rehearsal.XXXXXX/ns-sandbox && git commit -m Sandbox:\ tiny\ Python\ project\ with\ api/\ and\ web/\ and\ CI)
+ (cd /tmp/ns-rehearsal.XXXXXX/ns-sandbox && git push -u origin HEAD:main)
```

## Steps and expected output

Output marked *real* was produced by running the CLI offline (files-only, or with a fake GitHub transport for
the REST calls). Output marked *shape* comes from live runs only: numbers, logins and URLs will differ.

### a. Sandbox repo

Creates the private repo, clones it, writes `api/` and `web/` (a module, a test and a `conftest.py` each, so
`cd api && pytest` and `cd web && pytest` work and `web` can import `api`), and `.github/workflows/ci.yml`
(name `CI`, one job named `test`, on `push` to `main` and on `pull_request`). One commit, pushed to `main`.

Expected: `gh repo create` prints the repo URL; `git push` prints `* [new branch] HEAD -> main`. The first CI run
on `main` goes green in about a minute (check: `gh api repos/OWNER/NAME/actions/runs --jq '.workflow_runs[0].conclusion'`).

### b. `gh nightshift init --lanes api,web --yes`

`init --tool-ref REF` writes the files (pinned to `REF`) and does the GitHub side except branch protection. The
script commits and pushes the files, then runs `gh nightshift setup`, which adds protection. Protecting `main`
first would refuse that push (required checks and `enforce_admins`).

The files (*real*, `python3 -m nightshift init --root <tmpdir> --repo o/r --files-only --lanes api,web --yes`):

```text
wrote .night-shift/config.yml
wrote .github/workflows/night-shift.yml
wrote .github/workflows/night-shift-review.yml
wrote .github/ISSUE_TEMPLATE/night-shift-ticket.md
wrote docs/NIGHT_SHIFT_ISSUES.md
wrote AGENT_RULES.md
wrote .github/workflows/night-shift-planner.yml
```

The generated config (*real*):

```yaml
version: 1
main_branch: main
ci_workflow: ci.yml
required_checks: [test, review / verdict]
fix_lane: api
merge:
  mode: strict
tool_ref: main
lanes:
  - name: api
    paths: [api/]
    claude_token_secret: CLAUDE_TOKEN_API
    model: sonnet
    setup:
      commands: |
        cd api && pip install -r requirements.txt
    test_command: "cd api && pytest"
  - name: web
    paths: [web/]
    claude_token_secret: CLAUDE_TOKEN_WEB
    model: sonnet
    setup:
      commands: |
        cd web && pip install -r requirements.txt
    test_command: "cd web && pytest"
limits:
  max_attempts: 3
  infra_cap: 6
  max_needs_per_issue: 2
  max_notes_per_issue: 2
planner:
  docs: [docs/PRD.md]
  max_tickets_per_run: 8
  refill_below: 0
```

The GitHub side (*real*, an earlier `--dry-run` run on the existing config; the `gh api` bodies are cut here; since
then `init` prints `branch protection: skipped for now; run gh nightshift setup after pushing the new files` in
place of the protection call, which `setup` makes after the push).
Live, each `[dry-run] gh api ...` line is not printed and each `[dry-run] gh secret set` line is a hidden prompt:

```text
.night-shift/config.yml exists; using it (pass --force to start over)
[dry-run] would overwrite .night-shift/config.yml (36 lines)
kept .github/workflows/night-shift.yml (exists; use --force to overwrite)
kept .github/workflows/night-shift-review.yml (exists; use --force to overwrite)
kept .github/ISSUE_TEMPLATE/night-shift-ticket.md (exists; use --force to overwrite)
kept docs/NIGHT_SHIFT_ISSUES.md (exists; use --force to overwrite)
kept AGENT_RULES.md (exists; use --force to overwrite)
kept .github/workflows/night-shift-planner.yml (exists; use --force to overwrite)
[dry-run] gh api -X POST repos/paawandesai/ns-sandbox/labels --input - <<'JSON'
...
label created: lane:api
label created: lane:web
label created: wave:0
... (wave:1 to wave:9)
label created: night-safe
label created: blocked
label created: needs-human
label created: human
label created: proposed
label created: agent
label created: agent:in-progress
label created: waiting
label created: fix-main
label created: model:opus
label created: model:haiku
label created: review-error
label created: night-log
label created: frozen
repo settings: auto-merge on, delete branch on merge, squash merge on
variable AGENTS_ENABLED=false (turn on with `gh nightshift start`)
variable FIX_LANE=api
protected main: required test, review / verdict, strict=true, admins included
night-log issue #0 created (pin it)
[dry-run] gh secret set AGENT_PAT -R paawandesai/ns-sandbox   # value read from a hidden prompt (fine-grained PAT for this repo only: Contents, Issues, Pull requests read/write; Actions read; NO Workflows)
[dry-run] gh secret set CLAUDE_TOKEN_API -R paawandesai/ns-sandbox   # value read from a hidden prompt (`claude setup-token` from the owner of lane(s) api)
[dry-run] gh secret set CLAUDE_TOKEN_REVIEW -R paawandesai/ns-sandbox   # value read from a hidden prompt (`claude setup-token` from someone who does not own a lane (review gate))
[dry-run] gh secret set CLAUDE_TOKEN_WEB -R paawandesai/ns-sandbox   # value read from a hidden prompt (`claude setup-token` from the owner of lane(s) web)
would set AGENT_PAT
would set CLAUDE_TOKEN_API
would set CLAUDE_TOKEN_REVIEW
would set CLAUDE_TOKEN_WEB

Next: commit and push the new files (your gh token needs the `workflow` scope), run `gh nightshift doctor`, label a few issues night-safe, then `gh nightshift start`.
```

Live, the night-log line has a real number (`night-log issue #1 created (pin it)`) and each secret line reads
`secret AGENT_PAT set`. The closing "Next: commit and push" hint is already done by the script. On a free plan
the protection line is instead `WARNING: branch protection failed (...). Private repos on free plans cannot use it.`

### c. Plan three tickets from a plan file

The script writes `plan.json` (format: `PLAN_SCHEMA` in `nightshift/planner.py`: `tickets[]` with `key`, `title`,
`lane`, `wave`, `goal`, `do`, `acceptance`, `files`, optional `interface`, `blocked_by`, `requirements`; and
`coverage[]`), then runs `gh nightshift plan --plan-file plan.json`. No model is called, so the tickets are the
same every time. `web-page` has `blocked_by: ["api-greet"]`; both carry an `interface` (the validator requires it
on both ends of a dependency). Titles carry no ID; the planner assigns `[A1]`, `[W1]`, `[W2]`.

Expected (*real*, against an empty repo):

```text
created #1 [api] w0 [A1] Add greet() to the api package
created #2 [web] w1 [W1] Render a greeting page from api greet()
created #3 [web] w2 [W2] Show the account balance on the web page
coverage: complete (every requirement is done, ticketed or deferred with a reason)
planner notes: Rehearsal plan: web-page waits on api-greet; web-balance forces a need in lane api.
```

Live, the night-log issue is `#1`, so the tickets are `#2`, `#3`, `#4`. The planner also comments on the night log
with a `<!-- ns:{"complete":true,...,"type":"plan"} -->` marker. B's body ends with (*real*):

```text
## Interface
web/page.py: render_page(name: str) -> str, uses api.greeting.greet

Covers: R2 greeting page

Blocked by: #2
```

The script then prints `issues: A=#2 (api)  B=#3 (web, Blocked by A)  C=#4 (web, forces a need)` and takes the
`night-safe` label off C, so the web lane cannot pick C before step e.

### d. Doctor, start, one lane run, hand-off

`gh nightshift doctor` (*real* output for a correctly set-up sandbox, fake transport):

```text
✓ .night-shift/config.yml is valid (2 lanes: api, web)
! workflows follow gh-nightshift@main, a moving branch
    fix: pin tool_ref to a release tag or commit SHA in .night-shift/config.yml
✓ gh authenticated as paawandesai
! PAT permissions not checked
    fix: re-run with --pat-env VAR (VAR holds the PAT); the lane workflow also checks it on every run
✓ all 4 secrets exist
✓ labels exist
✓ branch protection is strict (PRs are tested against current main)
! AGENTS_ENABLED=false
    fix: `gh nightshift start` when main is green

✓ paawandesai/gh-nightshift@main is reachable

No problems found.
```

With `NS_AGENT_PAT` set the PAT lines become `✓ PAT authenticates as <login>` and
`! fine-grained PAT: GitHub does not expose its permissions, so Workflows cannot be checked from here`.
Warnings are expected; any `✗` line is a real problem (the script continues so you see everything).

`gh nightshift start` (*real*): `variable AGENTS_ENABLED=true`.

Then `gh workflow run night-shift.yml -R OWNER/NAME -f lane=api` (prints
`✓ Created workflow_dispatch event for night-shift.yml at main`). What happens, in order (*shape*):

1. Run 1, `lane-api / pick`: log line `{"mode": "issue", "issue": "2", "reason": "picked #2: [A1] Add greet() to the api package (model sonnet)", ...}`.
   Issue #2 gets `agent:in-progress` and a comment `<!-- ns:{"issue":2,"lane":"api","type":"attempt"} -->`
   `Night shift lane `api` started an attempt: <run url>`. The `lane-web` job in the same run is skipped
   (`manual run limited to lane api`).
2. `agent` builds on branch `agent/api/issue-2`; `publish` pushes it and opens a PR whose body starts
   `Closes #2` and contains `## Hand-off` with a `<!-- ns:{"data":{...},"type":"handoff-data"} -->` marker.
3. CI (`test`) and `night-shift-review` (`review / verdict`) run on the PR. When they finish, `workflow_run`
   starts night-shift again; the api picker squash-merges the PR, deletes the branch, comments on the PR
   (`Night shift: merged; hand-off sent to #3.`) and on B:

   ```text
   <!-- ns:{"from":2,"lane":"api","pr":5,"to":3,"type":"handoff"} -->
   **Night shift hand-off:** #2 ([A1] Add greet() to the api package) merged in PR #5 (lane `api`). This issue no longer waits on it.

   **Interface as built (functions, endpoints):**
   - api/greeting.py: greet(name: str) -> str
   ...
   Differences from the ticket: none reported by the agent (not verified mechanically; compare the two interfaces above).
   ```

   The marker's keys are sorted, so it starts `<!-- ns:{"from":` and ends `"type":"handoff"} -->`. It is
   trusted only because the PAT's login posted it.
4. The merge's push to `main` starts every lane. The web picker sees #2 closed and picks #3; its
   `task-web` artifact holds `task.md`, which contains, after the ticket (*real* rendering):

   ```text
   ## Coordination (posted by the night shift; trusted)

   <!-- ns:{"from":2,"lane":"api","pr":5,"to":3,"type":"handoff"} -->
   **Night shift hand-off:** #2 ([A1] Add greet() to the api package) merged in PR #5 (lane `api`). ...
   ```

Expected checks (*shape*):

```text
PASS  A's PR merged (issue #2)
      PR #5 merged_at=2026-11-10T02:14:09Z https://github.com/paawandesai/ns-sandbox/pull/5
      issue #2 state=closed
PASS  hand-off from #2 posted on #3
      comment 2468013579 by paawandesai: <!-- ns:{"from":2,"lane":"api","pr":5,"to":3,"type":"handoff"} -->
PASS  #3 picked with the hand-off in its task file
      run https://github.com/paawandesai/ns-sandbox/actions/runs/11800000000
      /tmp/ns-rehearsal.AbC123/task-web-11800000000/task.md:
        27:## Coordination (posted by the night shift; trusted)
        29:<!-- ns:{"from":2,"lane":"api","pr":5,"to":3,"type":"handoff"} -->
```

### e. A forced need

C (`[W2]`) asks for `web/balance.py` built on `api.accounts.get_balance(user_id: int) -> int`, which does not
exist. Its Do section says `api/` belongs to lane api and asks for status `waiting` with a need if
`api/accounts.py` is missing (worker prompt rule 8 says the same). The script also posts an owner comment
repeating it; the agent sees it under `## Discussion` as data. Then it puts `night-safe` back on C and
dispatches the web lane. The web lane only picks C after B's PR has merged (one open agent PR per lane).

What happens (*shape*): the agent reports `waiting` with one `needs` entry for lane `api`. `publish` then

- creates `[A2] <need title>` in lane `api` (`night-safe` if the need has an Interface, else `proposed`), body
  ending `Requested by #4 (lane `web`), which waits on it.`;
- appends `Blocked by: #6` to C's body (or adds that line);
- labels C `waiting`, edits the attempt comment to `<!-- ns:{"issue":4,"type":"waiting"} -->` `Attempt not counted: waiting on #6.`;
- comments on C (*real* format):

  ```text
  <!-- ns:{"from":4,"lane":"api","to":6,"type":"need"} -->
  **Night shift: waiting on another lane.** This issue needs #6 (Add get_balance() to the api package, lane `api`), queued for that lane. `Blocked by: #6` was added above; the picker resumes this issue once #6 closes. This attempt was not counted.
  ```

Expected check (*shape*):

```text
PASS  forced need on #4 filed #6 in lane api
      need marker on #4 points to #6: [A2] Add get_balance() to the api package [lane:api,wave:2,night-safe]
      #4 body: Blocked by: #6
      #4 labels: lane:web,wave:2,night-safe,waiting
```

(The exact label list varies; the check only needs `waiting`.)

### f. Pause

`gh nightshift pause` (*real*): `variable AGENTS_ENABLED=false`, then `PASS  night shift paused` /
`AGENTS_ENABLED=false`, the cleanup instructions (`gh repo delete OWNER/NAME --yes` after
`gh auth refresh -s delete_repo`, `rm -rf` the workdir, revoke the PAT) and `N check(s) passed, 0 failed.`
Pause stops new picks; runs already in progress finish. Lane api may already have picked `[A2]` before the
pause; that is expected.

## Timing and cost

| Phase | Typical wall time |
|---|---|
| a + b (repo, init, four secret prompts) | 3-5 min, mostly you pasting tokens |
| c (plan) | under 10 s |
| d: A picked to PR opened (agent run, sonnet) | 5-15 min |
| d: PR checks (CI about 1 min, review about 3-8 min) to merge and hand-off | 5-10 min |
| d: B picked after the merge | 1-2 min (push trigger) |
| e: B's PR checked and merged, then C picked and parked as waiting | 20-35 min |
| Total | about 45-75 min; budget 90 |

Claude usage: about four agent runs (A, B, C, possibly A2 before the pause) and two reviews. With
`claude setup-token` tokens this comes out of the owners' plan limits; the API-equivalent figure the night log
records is roughly $1-4 per sonnet run and $1-3 per opus review, so about $6-20 for the whole rehearsal
(`gh nightshift stats` shows the logged total afterwards).

Actions minutes (private repo, billed per job and rounded up to the minute): every night-shift run starts a
`pick` job per lane (about 1 min each even when it skips), the 20-minute cron adds about 6 runs an hour, and each
agent run is 5-20 min. Expect 60-150 minutes in total, inside the free plan's 2,000 a month.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `git push` refused: `refusing to allow ... to create or update workflow` | your gh token lacks `workflow` | `gh auth refresh -s workflow`, then `--from b --workdir ...` |
| `git push` refused: protected branch / required status checks | init's GitHub side ran before the push (e.g. you ran plain `init` by hand) | push through a PR, or temporarily `gh api -X DELETE repos/OWNER/NAME/branches/main/protection`, push, re-run `gh nightshift setup` |
| `init` prints `WARNING: branch protection failed` | private repo on a free plan | expected; see Prerequisites |
| Night-shift run fails at once: `workflow was not found` / `Unable to resolve action` | `--tool-ref` not pushed to `paawandesai/gh-nightshift`, or the tool repo is private | push the ref, make the tool repo public, re-run `init --regenerate` with the right `tool_ref` |
| `lane-api` job skipped, no pick | `AGENTS_ENABLED` is not `true` | `gh nightshift start` |
| pick: `The Claude token secret for lane api is empty or missing` | secret not set or misnamed | `gh nightshift join --lane api` |
| pick: `the PAT must authenticate and must not have the Workflows permission` | PAT expired, or has Workflows / is a classic PAT with `workflow` | make a fine-grained PAT as in Prerequisites; `gh secret set AGENT_PAT` |
| pick: `cannot read CI status on main` | PAT lacks Actions: Read, or `ci_workflow` is not `ci.yml` | fix the PAT or the config |
| pick: `main is red; waiting for lane api` | CI failed on `main` | look at the CI run; the api lane tries to fix it first |
| PR open but never merges: `PR #N in lane api is still in review` | `review / verdict` never reported (missing `CLAUDE_TOKEN_REVIEW`, review run failed) | check the night-shift-review run; set the secret |
| PR closed with `verdict_failed` / `checks_failed` | the review or CI rejected the agent's work | read the PR comments; the issue gets another attempt (up to `max_attempts: 3`) |
| `hand-off ... FAIL` although the PR merged | B's body lost its `Blocked by: #A` line, or the comment author is not the PAT login | check B's body and the merged PR's `Night shift: merged; hand-off sent to ...` comment. A hand-off goes only to issues that list A in `Blocked by` when the picker reconciles the merge; once the PR has its `handoff-done` marker it is not re-sent |
| `picked with the hand-off` FAIL: no task-web artifact | the web lane has not run since the merge, or the artifact expired (3 days) | `gh workflow run night-shift.yml -f lane=web`; re-run `--from d` |
| `task.md` has no Coordination section | B was picked more than 30 minutes after A closed with no hand-off posted (the picker waits up to 30 minutes for one) | check the api lane's run after the merge posted `handoff-done` on A's PR |
| `forced need` FAIL, C has a PR instead | the agent built a web-only workaround instead of reporting `waiting` | close the PR, strengthen C's Acceptance, re-label `night-safe`, re-run `--from e` |
| `forced need` FAIL, comment says `waiting with no valid needs` | the need named the wrong lane or no lane | read the agent's result in the `out-web` artifact (`result.json`) |
| new api ticket labelled `proposed`, not `night-safe` | the need had no `interface` (validator rule) | expected behaviour; the check still passes |
| waits time out | slow runners, usage limits, or a trigger was missed | look at the Actions tab; raise `--timeout`; resume with `--from` |

## Gaps found while writing the rehearsal

Fixed before the PR:

- `init` used to protect `main` before the generated files were pushed, so the push was refused. Protection now
  waits for `gh nightshift setup`.
- The web lane could pick B in the seconds between A's merge and the api picker posting the hand-off. The picker
  now waits up to 30 minutes for a hand-off from a dependency that just closed.
- `doctor` now checks that `paawandesai/gh-nightshift` is public and `tool_ref` exists.
- `init --tool-ref REF` pins the workflows without editing the config by hand.

Still open:

- `plan --dry-run` prints `created #0` for every ticket and renders `Blocked by: #0`, so a dry run cannot show the
  real dependency numbers.
- Plan-file tickets always get the ready label; there is no way to hold one back, so the script removes
  `night-safe` from C until step e.
- Lanes do not gate on each other's waves: any unblocked ready ticket is picked on the next run.
