# progress.md

One line per step with its verify result. Builders and verifiers are separate subagents; at most 3 fix rounds
per step, then the step is marked blocked.

- Step 1 (extract kit, EXTRACTION.md, port picker tests): built; verify 1 PASS (57 ported + 37 legacy tests pass; EXTRACTION.md stale lines fixed after verify).
- Step 2 (config + schema, reusable workflows, one job per lane, setup only in agent job): built; verify 1 FAIL (PAT jobs could import repo code; rename bypass of protected paths; PAT accepted as Claude token); fix round 1 in 0a8ab12; verify 2 FAIL (PYTHONSAFEPATH leaked into the agent job; AGENT_PAT still accepted as a Claude token when lanes name their own PATs); fix round 2 in 62b4f1d; verify 3 PASS (C, D, E, F).
  - A (strict protection, update-branch, conflict close, doctor strict=false): verify 1 PASS.
  - B (reviewer main-drift question): verify 1 PASS (weak: not enforced); fix round 1 enforces it via --overlap; verify 2 PASS.
- Step 3 (comment protocol, hand-offs, needs, notes, trust): built; verify 1 FAIL (agent text could forge trusted markers); fix round 1 in 0a8ab12; verify 2 FAIL (markers via dict keys, legacy block phrase, marker in a file name); fix round 2 in 62b4f1d; verify 3 FAIL (agent lane name echoed into a "Night shift:" comment matched the legacy block text; model text in the planner summary could forge a plan marker); fix round 3 (last allowed) in f771948; verify 4 PASS (no forgery path found across publish, planner summary, picker, task prep).
  - C hand-off (interface as built vs ticket): verify 1 PASS (differences self-reported; now labelled as such).
  - D (reason codes incl. manual, verdict_passed): verify 1 PASS with caveat (manual inferred only); fix round 1 records manual; verify 2 PASS.
- Step 4 (planner workflow, validator, coverage rule, follow-ups): built; verify 1 PARTIAL (path and text injection bypasses); fix round 1 in 0a8ab12; verify 2 FAIL (inline Blocked-by, request fence, code comments rejected, quoted paths, non-list crash); fix round 2 in 13fb285; verify 3 FAIL (requirements field unchecked; markup-wrapped paths); fix round 3 (last allowed) in 62b4f1d; verify 4 FAIL: **BLOCKED** after 3 fix rounds. Remaining defect: Files and Covers items are joined into one line when rendered, and only single items are checked, so `["R1 blocked by", "#9"]` renders `Covers: R1 blocked by, #9` and adds an undeclared dependency (or `-1`, which stalls the ticket). Needed fix: run parse_deps and the control-line check on the rendered body and compare with blocked_by.
  - C (required ## Interface): verify 1 PASS.
- Step 5 (CLI, every command against a mocked gh): built; 138 CLI tests found 5 bugs, fixed in f2eca14; verify 1 FAIL (PAT in git argv; bot login assumption); fix round 1 in 0a8ab12; verify 2 PASS (C1-C7) with one low doctor defect (403 hid the report), fixed in 13fb285.
  - A (doctor flags strict=false with fix): verify 1 PASS.
  - E (stats reasons + catch rate with Wilson CI; audit sample/evidence/mechanical check/CSV/kappa): verify 1 PASS (label-llm folder bug fixed in round 1).
- Step 6 (kit CI, rehearsal script and doc): verify 1 PASS (CI green on 0a8ab12: test 3.10, test 3.12, lint; shellcheck clean; dry run makes no network calls). Two doc mismatches fixed in 62b4f1d.
- Step 7 (README with F results, SECURITY, CHANGELOG, NIGHT_SHIFT_ISSUES.md): verify 1 PASS (10 SECURITY claims checked against code; templates identical). Five low doc defects fixed in 62b4f1d.
  - F (results from the first real run, A-C as audit-driven changes): verify 1 PASS (Wilson intervals recomputed: 72-85%, 52-86%, 75-98%; floor 36 points).

## Additions after the first verify (approved by Paawan)

- Batch 1 (a58b54a): built, tests in tests/test_team.py; verify 1 FAIL (with batch 2: bot_login not trusted by lanes on their own PAT; examples missing keys); fix round 1 (FixRound1 tests).
  - Per-contributor PATs (trusted_logins, join stores/checks the PAT, doctor checks each PAT).
  - (1) Init fully from flags, presets python/node/go, starter CI.
  - (4) pause/freeze cancel runs in flight.
  - (2) Optional nightly budget per lane, off by default.
- Batch 2 (1006b94): built, tests in tests/test_team.py (Roadmap, Digest); verify 1 FAIL (workflow PAT could not find the roadmap that setup created; titles and agent file names could fake a cost figure; setup not guarded); fix round 1 (FixRound1 tests, CostLineForgery).
  - (3) Morning digest: `digest` command, night-log post with a digest marker, Slack/Discord webhook, reusable digest.yml and generated caller.
  - Roadmap issue: `roadmap` command, created by init/setup, refreshed by planner publish, every merge hand-off and the digest; `plan --show`. Pinning is GraphQL-only: manual.
- Batch 3 (v0.1 picks, 109537c): built, tests in tests/test_team.py (Commands, RiskTiers, GitHubApp); verify 1 FAIL (renames and >2000-file PRs bypassed approve_paths; App permission denylist; stale approvals not re-asked; bot comments could run commands; doctor checked only the lane caller); fix round 1 (Batch3Fixes tests); verify 2 PASS. Batches 1-2 fix round 1: verify 2 PASS.
  - `/night-shift take` and `/night-shift retry` comment commands (commands.yml, generated caller, CLI take/retry).
  - Risk tiers by label and path, default auto or approve; approval-waiting PRs do not hold the lane.
  - GitHub App auth: manifest, openssl-signed JWT, down-scoped installation token in PAT jobs only, init/doctor/app check with --dry-run. Creating the App itself is manual (Paawan).
- Batch 4 (f6ed229): built, tests in tests/test_team.py (CrossRepo, CodeOwners, Templates, OrgConfig, OrgStatsWeeklyAuditLog); verify 1 FAIL (an approve_paths hit skipped the code-owner check; planner accepted cross-repo Blocked-by; CODEOWNERS failed open; base.yml could loosen labels, merge mode, tool_ref, risk; two pattern mismatches; `..` repo names; forged prompt version in audit-log; hackathon left contracts/ editable); fix round 1 (Batch4Fixes tests); verify 2 PASS with 4 low findings (wildcard without a slash, audit-log model fallback, empty CSV cells, base could replace required_checks/human_paths), fixed in fix round 2 (test_round2_low_findings).
  - Hackathon template (`init --template hackathon`).
  - CODEOWNERS-aware lanes and required owner review (`risk.codeowners`); team owners accept any writer (team membership is not readable with a repo-scoped token).
  - Org config inheritance (`extends`, `config pull`, committed base.yml).
  - Cross-repo Blocked-by and pull-style hand-offs (the other repo must be readable by the PAT).
  - `stats --org`, weekly report, `audit-log` export with model, prompt version and cost.
