# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

First release, extracted from the hackathon night-shift kit (`legacy/hackathon-kit/`, see `docs/EXTRACTION.md`).

### Added

- `gh nightshift` CLI extension (Python 3.10+, standard library only) with the commands `init`, `setup`,
  `join`, `doctor`, `status`, `stats`, `plan`, `start`, `pause`, `freeze`, `resume`, `lint` and `audit`.
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

[0.1.0]: https://github.com/paawandesai/gh-nightshift/tree/build/v0.1
