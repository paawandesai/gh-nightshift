# progress.md

One line per step with its verify result. Builders and verifiers are separate subagents; at most 3 fix rounds
per step, then the step is marked blocked.

- Step 1 (extract kit, EXTRACTION.md, port picker tests): built; verify 1 PASS (57 ported + 37 legacy tests pass; EXTRACTION.md stale lines fixed after verify).
- Step 2 (config + schema, reusable workflows, one job per lane, setup only in agent job): built; verify 1 FAIL (PAT jobs could import repo code; rename bypass of protected paths; PAT accepted as Claude token); fix round 1 pushed in 0a8ab12; re-verify pending.
  - A (strict protection, update-branch, conflict close, doctor strict=false): verify 1 PASS.
  - B (reviewer main-drift question): verify 1 PASS (weak: not enforced); fix round 1 enforces it via --overlap; re-verify pending.
- Step 3 (comment protocol, hand-offs, needs, notes, trust): built; verify 1 FAIL (agent text could forge trusted markers); fix round 1 pushed in 0a8ab12; re-verify pending.
  - C hand-off (interface as built vs ticket): verify 1 PASS (differences self-reported; now labelled as such).
  - D (reason codes incl. manual, verdict_passed): verify 1 PASS with caveat (manual inferred only); fix round 1 records manual; re-verify pending.
- Step 4 (planner workflow, validator, coverage rule, follow-ups): built; verify 1 PARTIAL (path and text injection bypasses); fix round 1 pushed in 0a8ab12; re-verify pending.
  - C (required ## Interface): verify 1 PASS.
- Step 5 (CLI, every command against a mocked gh): built; 138 CLI tests found 5 bugs, fixed in f2eca14; verify 1 FAIL (PAT in git argv; bot login assumption); fix round 1 pushed in 0a8ab12; re-verify pending.
  - A (doctor flags strict=false with fix): verify 1 PASS.
  - E (stats reasons + catch rate with Wilson CI; audit sample/evidence/mechanical check/CSV/kappa): verify 1 PASS (label-llm folder bug fixed in round 1).
- Step 6 (kit CI, rehearsal script and doc): CI green on e8943eb (after an actionlint/shellcheck fix); rehearse.sh + REHEARSAL.md built; verify pending.
- Step 7 (README with F results, SECURITY, CHANGELOG, NIGHT_SHIFT_ISSUES.md): built; verify pending.
  - F (results from the first real run, A-C as audit-driven changes): numbers checked with stats_math; verify pending.
