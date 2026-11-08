# Extraction inventory

gh-nightshift is the night-shift kit from the VERSUS hackathon repo, made to work for any project.
This file lists every hackathon-specific assumption found in the kit and what became of it.

## Source

- Source repo: `gauntletvshackathon`, commit `5c16cc0ec5281000917e8988a37a812852deaf15`.
- The kit as copied lives in `legacy/hackathon-kit/`. The kit's `.github/` folder is renamed `github/`
  there, so its workflows never run in this repo. Read `github/...` below as `.github/...` in the source.
- Files copied: `AGENT_RULES.md`, `RUNBOOK.md`, `docs/NIGHT_SHIFT_ISSUES.md`, `github/night-shift/*`
  (picker, prompts, setup scripts, picker tests), `github/workflows/*` (`night-shift.yml`,
  `night-shift-lane.yml`, `agent-review.yml`), `github/actions/install-mercury/action.yml` and
  `github/ISSUE_TEMPLATE/night-shift-ticket.md`.
- The source repo's `CLAUDE.md` (same commit) was read for context. It is not part of the copy.

Paths in the "Where" column are relative to `legacy/hackathon-kit/`, except `CLAUDE.md`, which means the
source repo's `CLAUDE.md`. All config keys are in `.night-shift/config.yml` (schema:
`nightshift/schema/config.schema.json`).

## Inventory

### Lanes, tokens and accounts

| Assumption | Where | Hackathon value | In gh-nightshift |
|---|---|---|---|
| Lane names | `github/workflows/night-shift.yml:50,53,56`; `AGENT_RULES.md:12-14`; `docs/NIGHT_SHIFT_ISSUES.md:15,32` | `field`, `cloud`, `engine` | `lanes[].name` (pattern `^[a-z0-9][a-z0-9_-]{0,30}$`). `gh nightshift init` writes one caller job per lane and one `lane:<name>` label per lane. |
| Lane ownership table | `AGENT_RULES.md:10-15` | `field/`; `cloud/` (with `mix.exs`, `mix.lock`, migrations); `engine/` + `evals/`; integrator owns `shared/`, `contracts/`, `pyproject.toml`, `constraints.txt`, `docs/`, `.github/` | `lanes[].paths` and `lanes[].description`. The worker prompt shows them as `__LANE_PATHS__`; the planner context lists them. The integrator row is gone: shared files "belong to a person" (`nightshift/templates/AGENT_RULES.md`). |
| Per-person Claude token secrets | `github/workflows/night-shift.yml:51,54,57,72`; `github/night-shift/setup_repo.sh:68-72`; `RUNBOOK.md:25` | `CLAUDE_TOKEN_PAAWAN` (field), `CLAUDE_TOKEN_DEBU` (cloud), `CLAUDE_TOKEN_TYLER` (engine), picked with `secrets[matrix.token_secret]` | `lanes[].claude_token_secret` (any `^[A-Z_][A-Z0-9_]*$` name). The generated caller job names it literally: `claude_token: ${{ secrets.<NAME> }}`. `gh nightshift init` prompts for missing secrets (`repo_setup.prompt_missing_secrets`). |
| Review gate token | `github/workflows/agent-review.yml:8,71,77`; `RUNBOOK.md:26` | `CLAUDE_TOKEN_REVIEW` | `review_token_secret` (default `CLAUDE_TOKEN_REVIEW`). Passed explicitly by the generated `night-shift-review.yml`. The error text in `.github/workflows/review.yml:92` still says "Secret CLAUDE_TOKEN_REVIEW is empty" even when the key names another secret (cosmetic). |
| Publishing PAT | `github/workflows/night-shift.yml:5-6,73`; `github/night-shift/setup_repo.sh:65-66` | `AGENT_PAT` (Contents, Issues, PRs read/write; Actions read; no Workflows) | Still `AGENT_PAT` by default (fixed name, not a key). `lanes[].agent_pat_secret` overrides it per lane. The planner always uses `AGENT_PAT` (`scaffold.planner_workflow`). The lane's `check-pat` step now fails a PAT that has the Workflows scope. |
| Per-lane PAT override | `github/workflows/night-shift.yml:8-10,73`; `RUNBOOK.md:27` | `matrix.lane == 'cloud' && secrets.AGENT_PAT_DEBU \|\| secrets.AGENT_PAT` | `lanes[].agent_pat_secret`. Changed behaviour: there is **no fallback** to `AGENT_PAT` when the named secret is empty. The kit fell back; gh-nightshift passes `secrets.<agent_pat_secret>` as is, and `agent_pat` is a required secret of `lane.yml`, so set the secret before naming it. |
| Planner token | (no planner in the kit) | n/a | `planner.claude_token_secret` (default: the first lane's token). |

### Stack and runner setup

| Assumption | Where | Hackathon value | In gh-nightshift |
|---|---|---|---|
| Python version and install | `github/workflows/night-shift-lane.yml:185-198`; `CLAUDE.md:13-15` | `setup-python` 3.12 with pip cache; `pip install -c constraints.txt --group dev -r shared/... -r field/... -r engine/... -r evals/requirements.txt` | Removed from the workflow. A lane lists its own `lanes[].setup.commands` (string or list), run in the agent job only. `gh nightshift init` suggests commands from the files it finds (`scaffold.detect_stack`). |
| Elixir toolchain | `github/workflows/night-shift-lane.yml:200-209`; `CLAUDE.md:17-18` | `erlef/setup-beam@v1` with `cloud/.tool-versions`; `mix deps.get && mix deps.compile` in `cloud/` with `MIX_ENV=test` | Removed. Put it in `lanes[].setup.commands`, or in a local composite action named by `lanes[].setup.action` (a `uses:` with `setup-beam` works there). Ran for every lane in the kit; now per lane. |
| Postgres service | `github/workflows/night-shift-lane.yml:159-172`; `CLAUDE.md:18-19` | `postgres:16` service for every lane, `DATABASE_URL=postgres://postgres:postgres@localhost:5432` | `lanes[].postgres_image`. Only lanes that set it get the service and `DATABASE_URL`. User, password, port and the URL are still fixed (`postgres`/`postgres`/5432) in `.github/workflows/lane.yml:128-142`. |
| Mercury install, engine lane only | `github/workflows/night-shift-lane.yml:211-213`; `github/actions/install-mercury/action.yml` | `if: inputs.lane == 'engine'`, `uses: ./.github/actions/install-mercury` (Mercury 22.01.8 debs from a release mirror, `mercury_json` at a pinned SHA) | `lanes[].setup.action: ./path/to/action` on the lane that needs it. The workflow copies it to `.ns-lane-setup` (kept out of git) and runs it in that lane's agent job only. The Mercury action itself is not shipped. |
| Lane test command | `CLAUDE.md:22-29`; worker prompt rule 3 (`github/night-shift/worker-prompt.md:8`) | Per-lane commands in CLAUDE.md (`ruff check field && pytest field`, `mix test`, ...) | `lanes[].test_command`, shown to the agent as `__TEST_COMMAND__` (falls back to "see CLAUDE.md"). |
| Agent job environment claims | `docs/NIGHT_SHIFT_ISSUES.md:100-105` | Python 3.12, Postgres 16, Elixir, Docker, Mercury in engine only | Removed from the ticket guide. The guide now says to check the lane's `setup` and `postgres_image` in the config. |
| Claude Code version and permission flags | `github/workflows/night-shift-lane.yml:216,224`; `RUNBOOK.md:79` | `vars.CLAUDE_CODE_VERSION \|\| latest`; `vars.CLAUDE_PERMISSION_FLAGS \|\| --dangerously-skip-permissions` | Unchanged: the same repo variables, same defaults. Not in the config file. |

### Paths and project rules

| Assumption | Where | Hackathon value | In gh-nightshift |
|---|---|---|---|
| Field screen rule | `github/night-shift/worker-prompt.md:13` (rule 8); `CLAUDE.md:34` | New screens go in `field/rescue_field/screens/` with a module-level `router`; no `include_router` in `field/rescue_field/api.py` | Removed. Rule 8 in `nightshift/prompts/worker.md` is now "waiting on another lane". Project rules like this belong in the project's `CLAUDE.md`, or in an override prompt at `.night-shift/prompts/worker.md` on the default branch (`nightshift/prompts.py`). |
| Ticket path mapping | `docs/NIGHT_SHIFT_ISSUES.md:22,64,96`; `github/ISSUE_TEMPLATE/night-shift-ticket.md:22`; `CLAUDE.md:36-50` | "see Ticket path mapping in CLAUDE.md" (`field_app` -> `rescue_field`, ...) | Removed. The guide says to use real paths. |
| Progress files | `github/night-shift/worker-prompt.md:3,12`; `AGENT_RULES.md:34`; `CLAUDE.md:32-33` | `progress/<lane>/<YYYY-MM-DD>-issue-<n>.md`, never `progress/<lane>.md` | **Not generalized.** Still hard-coded in `nightshift/prompts/worker.md` rule 7 and `nightshift/templates/AGENT_RULES.md` (`progress/<lane>/...`). Change it with a worker prompt override. The mention of `progress/<lane>.md` history is gone. |
| Docs the agent reads for open decisions | `github/night-shift/worker-prompt.md:11` (rule 6) | `docs/PRD.md`, `docs/DECISIONS.md`, `HANDOFF.md` | **Not generalized** for the worker (same names in `nightshift/prompts/worker.md` rule 6; missing files are skipped). The planner's docs are configurable: `planner.docs` (default `["docs/PRD.md"]`). |
| Protected paths | `github/night-shift/worker-prompt.md:7` (rule 2); `RUNBOOK.md:16` | Prompt says ".github/ changes are rejected automatically", but the RUNBOOK says the guard was removed: agent PRs may change any file; only the PAT's missing Workflows scope stops `.github/workflows/` pushes | Guard restored and always on. `publish.push_and_open_pr` rejects a bundle whose diff touches `.github/`, `.claude/`, `CLAUDE.md`, `AGENTS.md`, `.mcp.json` or `.night-shift/` (`config.BUILTIN_PROTECTED`) plus `protected_paths`. The config can add paths, never remove the built-in ones. The worker prompt lists them (`__PROTECTED__`); the planner turns tickets that touch them into `human` tickets. |
| Contracts are person-owned | `AGENT_RULES.md:8,15,25`; `docs/NIGHT_SHIFT_ISSUES.md:115` | `contracts/` (`engine_rules.md`, `sync_api.md`, vectors) | `planner.human_paths` (default `["contracts/"]`): tickets touching them become `human` tickets. Not a protected path unless listed in `protected_paths`. |
| Wave 0 contracts first | `AGENT_RULES.md:25` | "Wave 0 merges the interfaces, stubs and contract tests before any lane starts" | Generalized to the `## Interface` section rule (required when a ticket has or is a dependency) and hand-off comments. |
| Eval holdout, trained model, H1b | `docs/NIGHT_SHIFT_ISSUES.md:118-119` | Sealed holdout set E, golden sets, mock model until H1b | Removed (product-specific). |
| Picker tests location | `CLAUDE.md:28`; `github/night-shift/tests/test_agent_pick.py` | `python -m unittest discover -s .github/night-shift/tests` | gh-nightshift tests live in `tests/` of this repo. Nothing is installed into a project's `.github/night-shift/`. |
| Picker test fixture names | `github/night-shift/tests/test_agent_pick.py:10-11,63-77` | Already generic: lane `api`, repo `o/r`, PAT login `night-bot`, night-log issue `500`, branch `agent/api/issue-1` | Nothing to generalize. At the time of writing, `tests/` holds only `test_audit.py` (lane `core`); there is no ported picker test yet. |
| Fly deploy, local run, demo device tokens | `RUNBOOK.md:81-165` | `rescue-cloud-demo`, `rescue-field-demo`, `DEVICE_TOKENS`, HF model pins | Removed: product docs, not night-shift tooling. |

### Labels

| Label | Where | Hackathon value | In gh-nightshift |
|---|---|---|---|
| Ready label | `github/night-shift/agent_pick.py:64`; `github/workflows/night-shift-lane.yml:76` | `night-safe` (repo variable `READY_LABEL`) | `labels.ready` (default `night-safe`). The `READY_LABEL` variable is no longer read. |
| Lane labels | `github/night-shift/setup_repo.sh:19`; `github/night-shift/agent_pick.py:89-90` | `lane:field`, `lane:cloud`, `lane:engine` | `lane:<lanes[].name>`. The `lane:` prefix is fixed. The picker now requires exactly one lane label on an issue. |
| Wave labels | `github/night-shift/setup_repo.sh:20`; `github/ISSUE_TEMPLATE/night-shift-ticket.md:5`; `docs/NIGHT_SHIFT_ISSUES.md:33` | `wave:0`..`wave:9` created; template pre-fills `wave:W05` | `wave:0`..`wave:9` created by `scaffold.labels`; `wave:N` and `wave:WNN` both still sort (`rules.wave_of`). The template no longer pre-fills a wave. The `wave:` prefix is fixed. The validator wants a wave 0-99. |
| Skip labels | `github/night-shift/agent_pick.py:36` | `blocked`, `needs-human`, `human`, `integrator` | `labels.blocked`, `labels.needs_human`, `labels.human`, plus the new `labels.proposed`. **`integrator` is not generalized**: it is hard-coded in `Config.skip_labels` and no longer created as a label. (The kit skipped `human` and `integrator` but never created them; gh-nightshift creates `human`.) |
| `agent` PR label | `github/workflows/night-shift-lane.yml:333` | `agent` | `labels.agent`. The picker now finds lane PRs by head branch `agent/<lane>/`, same-repo head and author = PAT login, not by label. |
| `agent:in-progress` | `github/workflows/night-shift-lane.yml:109,359`; `github/night-shift/setup_repo.sh:25` | `agent:in-progress` | `labels.in_progress`. |
| `night-log` | `github/workflows/night-shift-lane.yml:129,362`; `github/night-shift/agent_pick.py:282-285`; `github/night-shift/setup_repo.sh:38-41` | `night-log` | `labels.night_log`. |
| `fix-main` | `github/workflows/night-shift-lane.yml:334`; `github/night-shift/setup_repo.sh:26` | `fix-main` | **Fixed name**, not configurable (`publish.push_and_open_pr`). Still created. |
| `model:*` | `github/night-shift/agent_pick.py:101-105`; `github/night-shift/setup_repo.sh:27-28` | `model:opus`, `model:haiku`; any `model:<alias>` overrides | Same: `model:<alias>` on an issue wins (`rules.model_for`). `model:opus` and `model:haiku` are created. Prefix fixed. |
| `review-error` | `github/workflows/agent-review.yml:105`; `github/night-shift/agent_pick.py:235` | `review-error` | **Fixed name** (`.github/workflows/review.yml:115`, `picker.close_pr`). Still created. |
| `needs-human` | `AGENT_RULES.md:30`; `github/night-shift/setup_repo.sh:23` | `needs-human` | `labels.needs_human`. Also added automatically when a `needs` request would create a dependency cycle. |
| New labels | (not in the kit) | n/a | `proposed` (`labels.proposed`), and fixed names `waiting` (an issue parked on another lane) and `frozen` (`gh nightshift freeze`). |

### The night-log issue

| Assumption | Where | Hackathon value | In gh-nightshift |
|---|---|---|---|
| How the log issue is found | `github/workflows/night-shift-lane.yml:129,362`; `github/night-shift/agent_pick.py:282-285` | First open issue with label `night-log`. **No issue number is hard-coded** in the kit. (`500` in the tests is a fake number.) | Same rule over REST: the first open issue with `labels.night_log` (`GH.night_log`). `gh nightshift init` creates one if none exists. |
| Log line timezone | `github/workflows/night-shift-lane.yml:365` | `TZ=America/Chicago date +%H:%M` | `timezone` (IANA name, default `UTC`). An unknown name falls back to UTC (`publish.publish`). |

### Branches, CI and triggers

| Assumption | Where | Hackathon value | In gh-nightshift |
|---|---|---|---|
| Main branch | `github/workflows/night-shift.yml:21`; `github/workflows/night-shift-lane.yml:39`; `github/night-shift/agent_pick.py:67` | `main` (push trigger hard-coded; elsewhere `default_branch \|\| vars.MAIN_BRANCH \|\| main`) | `main_branch` (default `main`): push trigger in the generated caller, picker, task, publish and branch protection. Partly still a variable: `.github/workflows/lane.yml:41` checks out `github.event.repository.default_branch \|\| vars.MAIN_BRANCH \|\| 'main'`, so on a scheduled run with a non-`main` default branch set `MAIN_BRANCH` too. |
| CI workflow file | `github/night-shift/agent_pick.py:66`; `github/workflows/night-shift-lane.yml:78,92` | `ci.yml` (repo variable `CI_WORKFLOW`) | `ci_workflow` (default `ci.yml`). The `CI_WORKFLOW` variable is no longer read. |
| CI workflow name for `workflow_run` | `github/workflows/night-shift.yml:25` | `workflows: [CI, agent-review]` | `ci_workflow_name`, else the `name:` read from the CI file at init, else `CI`. The review workflow is now called `night-shift-review`. |
| Required checks | `github/night-shift/setup_repo.sh:50`; `AGENT_RULES.md:21` | `test`, `verdict` | `required_checks` (default `["test", "review / verdict"]`). Also used by the picker: a PR is green only when every required check has reported. |
| Agent branch names | `github/workflows/night-shift-lane.yml:99,113`; `github/workflows/agent-review.yml:23,55` | `agent/<lane>/issue-<n>`, `agent/<lane>/fix-main-<run id>` | **Fixed**, not configurable. Fix-main branches are now `agent/<lane>/fix-main-<short sha>`. |
| Schedule | `github/workflows/night-shift.yml:28` | `*/20 * * * *` | **Not generalized.** Hard-coded in `scaffold.lane_workflow`. Edit the generated `.github/workflows/night-shift.yml` by hand (a later `init --regenerate` overwrites it). The planner refill timer is configurable: `planner.schedule` (default `17 * * * *`, used only when `planner.refill_below > 0`). |
| Kill switch and fix lane | `github/workflows/night-shift.yml:11,41`; `github/night-shift/agent_pick.py:63,68`; `github/night-shift/setup_repo.sh:34-35` | Repo variables `AGENTS_ENABLED`, `FIX_LANE` (setup sets it to the first lane) | `AGENTS_ENABLED` stays a repo variable (`gh nightshift start` / `pause`). `fix_lane` in the config (default: first lane); init writes it to the `FIX_LANE` variable, which still wins at run time. |
| Branch protection mode | `github/night-shift/setup_repo.sh:44-56` | `strict: false` (PRs need not be up to date) | `merge.mode`: `strict` (default; `strict: true`, the picker calls update-branch) or `queue` (merge queue ruleset, CI must run on `merge_group`). |
| Org/repo slug in docs | `docs/NIGHT_SHIFT_ISSUES.md:162,190,216` | `Gauntlet-Hackathon/gauntletvshackathon` | Removed; the guide uses `OWNER/REPO`. |

### Models and limits

| Assumption | Where | Hackathon value | In gh-nightshift |
|---|---|---|---|
| First-try model per lane | `github/workflows/night-shift.yml:52,55,58,67`; `github/workflows/night-shift-lane.yml:25,221`; `github/night-shift/agent_pick.py:74` | `sonnet` | `lanes[].model`, else `models.default` (default `sonnet`). The `LANE_MODEL` env path still exists in `picker.Runtime` but `lane.yml` does not set it. |
| Retry and fix-main model | `github/night-shift/agent_pick.py:75`; `RUNBOOK.md:73` | `vars.ESCALATE_MODEL \|\| opus` | `lanes[].escalate_model`, else `models.escalate` (default `opus`). The `ESCALATE_MODEL` repo variable still wins at run time. |
| Review model | `github/workflows/agent-review.yml:73` | `vars.REVIEW_MODEL \|\| opus` | `models.review` (default `opus`), passed as the `model` input of `review.yml`; `vars.REVIEW_MODEL` is only a fallback when the input is empty. |
| Planner model | (no planner in the kit) | n/a | `models.planner` is parsed, but **not wired**: `.github/workflows/planner.yml:104` uses `vars.PLANNER_MODEL \|\| 'opus'`. Set the `PLANNER_MODEL` variable for now. |
| Max turns | `github/workflows/night-shift-lane.yml:222`; `RUNBOOK.md:79` | `vars.MAX_TURNS \|\| 80` | `limits.max_turns` (default 80), emitted by the `lane-config` step as `max_turns`. The `MAX_TURNS` variable is no longer read. The review run's 30 turns and the planner's 60 turns stay hard-coded. |
| Attempt cap | `github/night-shift/agent_pick.py:65`; `docs/NIGHT_SHIFT_ISSUES.md:23` | 3 (`MAX_ATTEMPTS` env, never set by the workflow) | `limits.max_attempts` (default 3, 1-10). Also caps fix-main tries per failing commit. |
| Infra (never-started) cap | `github/night-shift/agent_pick.py:84-86`; `RUNBOOK.md:13` | `2 * MAX_ATTEMPTS` = 6 | `limits.infra_cap` (default `2 * max_attempts`). |
| Stale PR timeout | `github/night-shift/agent_pick.py:70`; `RUNBOOK.md:62` | 45 minutes | `limits.stale_minutes` (default 45, minimum 5). |
| Job timeouts | `github/workflows/night-shift-lane.yml:44,153,266`; `github/workflows/agent-review.yml:25` | pick 10, agent 50, publish 10, review 20 minutes | **Not generalized.** Same values hard-coded in `lane.yml` and `review.yml`. |
| New caps | (not in the kit) | n/a | `limits.max_needs_per_issue` (2), `limits.max_notes_per_issue` (2), `planner.max_tickets_per_run` (8). Follow-ups are capped at 3 per run and the hand-off window at 72 hours, both hard-coded (`publish.MAX_FOLLOW_UPS`, `picker.HANDOFF_WINDOW_HOURS`). |

### Ticket IDs

| Assumption | Where | Hackathon value | In gh-nightshift |
|---|---|---|---|
| Ticket ID prefixes | `docs/NIGHT_SHIFT_ISSUES.md:49`; `github/ISSUE_TEMPLATE/night-shift-ticket.md:4,11` | `F` field, `C` cloud, `E` engine, `EV` evals, `H` human; template title `[F00]` | `planner.id_prefixes` (`lane: PREFIX`, 1-4 capital letters); default is the lane's first letter, uppercased (`Config.id_prefix`). One prefix per lane: a second prefix inside a lane (like `EV` inside `engine`) and a separate prefix for human tickets (`H`) are **not supported**. Human tickets use their lane's prefix. Template title is now `[X0]`. |

### GitHub API usage

| Assumption | Where | Hackathon value | In gh-nightshift |
|---|---|---|---|
| `gh issue` / `gh pr` / `gh run` commands | `github/night-shift/agent_pick.py:216-220,232,248,264-268,283,303-307,324`; `github/workflows/night-shift-lane.yml:101,108-111,115,126,129,335-337,359,362`; `github/workflows/agent-review.yml:57,101,105`; `github/night-shift/setup_repo.sh:14,16,33-41` | `gh issue list/view/edit/comment/create`, `gh pr list` (GraphQL `statusCheckRollup`, `mergeable`), `gh pr create/merge/close/edit/comment`, `gh run list/view` | Replaced by REST only, through `gh api` in `nightshift/gh.py` (no GraphQL). Checks come from `commits/{sha}/check-runs` and `commits/{sha}/status`; CI health from `actions/workflows/{file}/runs`; fix-main logs from `actions/jobs/{id}/logs`. |
| One-time setup scripts | `github/night-shift/setup_repo.sh`; `github/night-shift/assign_lanes.sh` | Bash scripts taking `<owner/repo> <lane>...` and `EPIC=lane` pairs | `setup_repo.sh` became `nightshift/repo_setup.py` (labels, repo settings, variables, protection, night log, secrets), run by `gh nightshift init`. **`assign_lanes.sh` (Idea Factory `epic:` labels to `lane:` labels) has no equivalent yet.** |

## Behaviour changes vs the kit

- **REST only.** Every GitHub call goes through `gh api` REST endpoints. No GraphQL, no `gh issue list` or
  `gh pr list`. This also works where GraphQL is blocked.
- **One job per lane, explicit secrets.** The kit used a matrix and `secrets[matrix.token_secret]`, plus a
  hard-coded `matrix.lane == 'cloud'` PAT override. `gh nightshift init` now writes one caller job per lane
  that names its Claude token and PAT literally. No matrix, no `secrets[...]`, no `secrets: inherit`.
  Re-run `gh nightshift init --regenerate` after changing lanes.
- **Strict, up-to-date merging.** The kit set `strict: false` and asked for auto-merge at publish time.
  Now branch protection is `strict: true` (in `merge.mode: strict`). The picker checks
  `compare/main...head`; a PR behind main is updated with `PUT pulls/{n}/update-branch` and must pass its
  checks again. Only then does the picker merge it with `PUT pulls/{n}/merge` (squash, pinned to the
  head SHA). Publish no longer requests auto-merge. `merge.mode: queue` uses a merge queue instead.
  The review gate also gets a "what changed on main" file and a `main_drift` question.
- **PR close reason codes.** Every agent PR the picker closes or merges gets a trusted marker with a
  reason: `verdict_passed`, `verdict_failed`, `checks_failed`, `conflict`, `stale`, `superseded` or
  `manual`. A PR whose issue was closed meanwhile is closed as `superseded`. Conflicts and `review-error`
  PRs still refund the attempt.
- **Comment protocol trusted only from the PAT login.** Machine-readable facts are hidden
  `<!-- ns:{...} -->` markers. They count only when the comment author is the PAT's own login, not even
  repo admins. The kit's old markers (`<!-- agent-attempt -->` and friends) are still counted from that
  login, so a repo can switch mid-flight. New marker types carry hand-offs (posted on dependent issues
  when a PR merges, with the interface as built), `needs` (an agent that is `waiting` on another lane
  files a ticket there, adds `Blocked by` and the `waiting` label, and the attempt is not counted), notes
  between issues, and `proposed` follow-ups. The agent's task file separates trusted "Coordination" from
  fenced "Discussion" (comments by owners, members and collaborators, as data).
- **Protected paths enforced again.** Publish rejects any bundle that touches the built-in protected
  paths or `protected_paths`. The kit had removed this guard.
- **Planner.** New: a read-only Claude run drafts tickets; a separate job with the PAT validates them
  against the ticket guide (title, sections, one lane, wave, `## Interface` when dependencies exist,
  real `Blocked by` targets, no cycles, no duplicates, cap per run) and creates them in dependency order.
  Runs from `gh nightshift plan`, from the planner workflow, from a `/night-shift plan <request>`
  comment by someone with write access, or on a timer when `planner.refill_below > 0`.
- **Lane setup is per lane.** The kit installed Python, Elixir and Postgres on every lane's runner. Now
  each lane runs only its own `setup.commands`, `setup.action` and `postgres_image`.
- **Prompts can be overridden** per project with `.night-shift/prompts/<worker|review|planner>.md` on the
  default branch.

## Not mapped to configuration

These kit assumptions are still fixed in gh-nightshift:

- The `*/20 * * * *` lane schedule (hard-coded in the generated caller workflow).
- Agent branch names `agent/<lane>/issue-<n>` and `agent/<lane>/fix-main-<sha>`.
- The `progress/<lane>/<date>-issue-<n>.md` rule and the worker's decision docs (`docs/PRD.md`,
  `docs/DECISIONS.md`, `HANDOFF.md`). Change them with a worker prompt override.
- Label names `fix-main`, `review-error`, `waiting`, `frozen`, `integrator` and the prefixes `lane:`,
  `wave:`, `model:`.
- Job timeouts (10/50/10/20 minutes), review max turns (30) and planner max turns (60).
- Postgres service credentials and `DATABASE_URL` (only the image is configurable).
- `AGENT_PAT` as the default PAT name and the planner's PAT.
- `models.planner` is in the config but the planner workflow reads `vars.PLANNER_MODEL` instead.
- A second ticket prefix inside one lane (`EV`) or a human-ticket prefix (`H`).
- The epic-to-lane mapping script (`assign_lanes.sh`).
