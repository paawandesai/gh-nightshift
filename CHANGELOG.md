# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.4] - unreleased

### Added

- Codex lanes: `provider: codex` with `codex_token_secret` (an OpenAI API key) runs a lane on OpenAI's Codex CLI
  (`codex exec --json --output-schema`) instead of Claude Code. The caller passes the key as the new `codex_token`
  (and `codex_token_fallback`) input of `lane.yml`, so a lane still receives exactly its own model token and its
  PAT. The key reaches Codex only inside a subshell and is stripped from the commands the agent runs; the token
  guard covers it. Publish reshapes Codex's event stream into the usual result (`nightshift/providers.py`), so the
  never-started rule, the fallback token, stats and the night log work unchanged. Codex reports no cost, so a Codex
  lane cannot set `budget_usd_per_night`; it uses its own `model`/`escalate_model` only. The reviewer and planner
  still run Claude, and the planner never borrows a Codex key. New variables: `CODEX_VERSION`,
  `CODEX_PERMISSION_FLAGS`.

- `runners.github` and `runners.agent` in the config choose the runners the reusable workflows use (default
  `ubuntu-latest`), so lanes can run on self-hosted machines. The jobs that hold the PAT or the App key and the
  jobs that run Claude (agent, planner draft, review) get separate labels, and the config refuses label sets
  where either is a subset of the other or has only generic labels (`self-hosted`, `linux`, ...): Claude must
  never share a machine with the token. The review workflow's input is now `agent_runs_on`.
- The jobs that run Claude set up Node 22 (before the lane's own setup, which still wins) and install Claude
  Code into the job's temp directory, so it works on a runner without root and a run cannot leave a planted
  `claude` for the next job on a self-hosted machine.

## [0.1.3]

### Fixed

- The token guard withheld every run that ended without commits (a run that reports `blocked` or `waiting`,
  for example) and labelled it "token found in its commits". With no commits to search, `xargs -r` ran nothing
  and exited 0, which the guard read as a match. It now searches commits only when there are some. No token
  was leaked in these runs and none needs rotating because of this message alone.

## [0.1.2] - 2026-11-09

### Security

- Fixes from the security review of 2026-11-09:
  - The issue template files tickets as `proposed`, not `night-safe`. GitHub applies a template's labels for
    anyone who opens an issue, so an outsider's ticket arrived already approved. Re-run `gh nightshift init
    --force` (or edit `.github/ISSUE_TEMPLATE/night-shift-ticket.md`) in repos set up before this release.
  - New `limits.auto_ready_needs` (default `true`, the behaviour so far). Set it to `false` and a ticket an agent
    requests in another lane (`needs`) is filed as `proposed` until a person promotes it.
  - Hand-off items are single lines, so agent text cannot start a section of its own in a trusted comment.
  - The run's cost and turn count are read as numbers. A crafted cost could put a trusted marker on the night
    log, and a non-numeric turn count crashed the publish job.
  - Agent text can no longer close other issues: closing keywords are broken up in the PR body and title, and
    the picker writes the squash commit message itself (which also keeps `[skip ci]` off main).
  - Without an open night-log issue, a lane with a budget and the fix-main lane stop instead of running uncounted.
  - Only `github-actions[bot]` itself counts as the Actions bot; a login that starts with `github-actions` did too.
  - `setup` keeps the branch protection a repo already has (required reviews, restrictions, other checks) and
    prints what it kept and changed. It used to replace it.
  - SECURITY.md: four claims corrected and ten known gaps added.
- The agent's Claude token is handed to Claude on a pipe instead of an environment variable, so tests and tools
  the agent runs do not inherit it (`NIGHT_SHIFT_TOKEN_IN_ENV=true` restores the old behaviour).
- A token guard in the agent job searches the commits, the result and the log for the lane's Claude token before
  anything is uploaded or pushed. On a hit the run is withheld and the issue is labelled `needs-human`.

### Changed

- With `--repo`, commands run from a folder without a night-shift config read the config, workflows and CODEOWNERS
  from the repo's default branch, so `setup`, `doctor`, `status` and the switches work from any folder. `start`
  refuses a repo that has no config on main yet.
- `init` pins the workflows to the installed extension's release (`tool_ref: v0.1.2`) instead of `main`.

### Added

- `lanes[].fallback_token_secret`: an optional second Claude token per lane (for example from a second account).
  The agent job switches to it only when the lane's token cannot start a run (usage limit, expired or revoked
  token). It reaches Claude on the same pipe and the token guard checks it too; the lane then receives three
  secrets. `join --lane X --fallback` stores it.
- `docs/ALPHA.md`: a guide for alpha testers (install, set up a repo, join a lane, what to report).

### Fixed

- The `gh nightshift` launcher now finds Python on Windows: it also tries `python` and `py -3`, skips a `python3`
  that is only the Microsoft Store stub, and passes a Windows-style `PYTHONPATH`.
- `init` suggests setup and test commands only from the lane's own folder or the repo root, never from another
  lane's folder.
- A failed `gh secret set` in `init` or `join` explains that repository secrets need admin access.
- `gh nightshift --help` lists only the commands people run.

## [0.1.1] - 2026-11-09

### Added

- `init --lane-pats` (with `--owners` and `--bot-login`) pairs every lane with its owner's own PAT and Claude
  token (`AGENT_PAT_<OWNER>`, `CLAUDE_TOKEN_<OWNER>`) and trusts the owners' logins, so each lane's PRs are
  opened as its owner.

### Fixed

- Agent runs ignore build and test caches (`__pycache__/`, `*.pyc`, `.pytest_cache/`, `node_modules/`, ...) even
  when the repo has no `.gitignore` for them, so they never end up in an agent PR.

## [0.1.0] - 2026-11-09

First release, extracted from the hackathon night-shift kit (`legacy/hackathon-kit/`, see `docs/EXTRACTION.md`).

### Added

- The planner rejects tickets whose Files or Requirements items, joined on one line when rendered, would form a
  `Blocked by` line (a dependency the plan never declared).

- `gh nightshift` CLI extension (Python 3.10+, standard library only) with the commands `init`, `setup`,
  `join`, `doctor`, `status`, `stats`, `plan`, `start`, `pause`, `freeze`, `resume`, `lint` and `audit`.
- The review gate fetches the base branch with the job token for that one command, so it works on private
  repos (the checkouts keep no credentials).
- `check-pat` (run at the start of every lane) probes the repo, contents, issues, pull requests, Actions, checks and commit
  statuses with the PAT and names the missing fine-grained permission, instead of `pick` failing later on a bare 404. The PAT now also needs
  Commit statuses: Read. Fine-grained PATs cannot have Checks, so the picker reads a PR's CI from its Actions jobs
  when the check-runs endpoint answers 403 (App mode keeps Checks: read).
  Every command that writes to GitHub supports `--dry-run`, which prints the exact `gh api` calls.
- `.night-shift/config.yml`, validated against `nightshift/schema/config.schema.json`, with an annotated
  example in `examples/config.yml`.
- Reusable workflow `lane.yml` that runs each lane as three jobs. `pick` holds the PAT, `agent` holds only
  the lane's Claude token, and `publish` holds the PAT and validates the agent's git bundle on a clean
  runner.
- A generated caller workflow with one job per lane and explicit secret names. It has no `secrets[...]`
  lookups and no `secrets: inherit`.
- Per-lane setup commands, an optional local composite action, and an optional Postgres service, all run
  only in the agent job.
- Protected paths (`.github/`, `.claude/`, `CLAUDE.md`, `AGENTS.md`, `.mcp.json`, `.night-shift/` plus
  configured ones). Agent changes to them are rejected in the publish job.
- `check-pat` step in every lane run. It fails a PAT that does not authenticate or has the classic
  `workflow` scope.
- Reusable review gate `review.yml` (required check `review / verdict`). It reads its prompt and rules from
  the base branch, runs with read-only tools, and checks main drift since the PR's base
  (`git diff <merge-base>..origin/main --stat` plus overlapping diffs).
- Merge modes `strict` (up-to-date branch protection and automatic `update-branch`) and `queue` (merge
  queue ruleset).
- Comment protocol with `<!-- ns:{...} -->` markers. Only markers posted by the PAT's own login are
  trusted.
- Hand-offs to dependent issues on merge. They restate the interface as built and flag differences from
  the ticket.
- `waiting` status with cross-lane `needs`: capped at 2 per issue, cycles refused, attempt not counted.
  Also notes to other issues (capped at 2 per issue), and follow-ups filed as `proposed`.
- PR reason codes: `verdict_passed`, `verdict_failed`, `checks_failed`, `conflict`, `stale`, `superseded`,
  `manual`.
- Reusable planner workflow `planner.yml` with modes `prd`, `gap` and `review` (`/night-shift plan` from a
  user with write access), plus scheduled refill below `planner.refill_below`.
- A ticket validator for the planner: required sections, a required `## Interface` section for tickets
  with dependencies, `human` tickets for protected and contract paths, dedupe, a per-run cap, and a
  coverage stopping rule.
- The ticket guide `docs/NIGHT_SHIFT_ISSUES.md`, an issue template and `AGENT_RULES.md`, written into
  projects by `init`.
- `gh nightshift audit`: a seeded sample of merged agent PRs, an evidence folder, a mechanical test check
  (tests at the merge commit and with the implementation reverted), optional LLM labels, and a report with
  Wilson intervals, the detection floor and Cohen's kappa.
- `gh nightshift stats`: merge rate with a 95% interval, reason codes, escalation share, cost per merged PR
  and the review gate's estimated catch rate.
- Kill switch and code freeze: the `AGENTS_ENABLED` repo variable, `pause`, `freeze` and `resume`.
- `README.md`, `SECURITY.md` (threat model and known gaps) and this changelog.
- Per-contributor PATs: `trusted_logins`, `join` stores and checks the lane owner's PAT, `doctor` checks each.
- `init` runs fully from flags, with optional `--preset python|node|go` and a starter CI when none exists.
- `pause` and `freeze` cancel lane runs in flight (`--no-cancel` to keep them).
- Optional nightly budget per lane (`budget_usd_per_night`), off by default.
- Morning digest: `gh nightshift digest`, posted daily to the night log by `night-shift-digest.yml`, with
  an optional Slack or Discord webhook (`digest.webhook_secret`).
- Comment commands `/night-shift take` and `/night-shift retry` (and `take N` / `retry N` in the CLI).
- Risk tiers: `risk.approve_labels`, `approve_paths`, `default: approve` with `auto_labels`; such PRs wait
  for one human approval before the picker merges them.
- GitHub App auth (`auth.mode: app`, `app manifest`, `app check`, `init --app-id --app-key-file`): a
  down-scoped installation token minted in the PAT jobs only.
- `init --template hackathon`.
- CODEOWNERS-aware lanes: `init` suggests lane owners from CODEOWNERS and sets `risk.codeowners`, which
  makes PRs touching owned files wait for an owner's approval.
- Org-wide config inheritance: `extends:` plus `gh nightshift config pull` (`.night-shift/base.yml`).
- Cross-repo `Blocked by: org/repo#n` with pull-style hand-offs.
- `stats --org`, a weekly report (`digest --weekly`, `digest.weekly_schedule`) and `audit-log` export
  (model, prompt version, cost per agent PR); PR bodies now carry the prompt version.
- Roadmap issue: waves, status per ticket and lane, Blocked-by mermaid graph; created by `init`/`setup`,
  refreshed by the planner, every merge and the digest; `gh nightshift roadmap` and `plan --show`.

[0.1.0]: https://github.com/paawandesai/gh-nightshift/tree/build/v0.1
