# Agent coordination rules

Paste this section into the project's CLAUDE.md (and AGENTS.md for other harnesses). It applies to every agent, day or night.

## Lanes and ownership
- Every issue has exactly one `lane:<name>` label. Work only on issues in your lane.
- Each lane owns the folders listed under `paths` in `.night-shift/config.yml`. Edit files outside your lane only when the ticket names them.
- Shared files (package manifests, lockfiles, DB schema, route registry, contracts) belong to a person. To change one, keep the edit minimal and say why in the PR.
- Never edit protected paths: `.github/`, `.claude/`, `CLAUDE.md`, `AGENTS.md`, `.mcp.json`, `.night-shift/` and anything in `protected_paths`. The night shift rejects such changes.

## Branches and PRs
- One issue = one branch = one PR. Agents use `agent/<lane>/issue-<n>`; people use `<name>/issue-<n>`.
- Never push to the default branch. Every change lands through a PR whose required checks pass (`test` and, for agent PRs, `review / verdict`), tested against current main.
- PR body starts with `Closes #<n>`.

## Contracts first
- Interfaces change only through a ticket that says so. Build against the contract, not against another lane's implementation.
- A ticket that depends on another, or that others depend on, has an `## Interface` section. Build exactly that, and report differences in your hand-off.

## Talking to other lanes
- Only through issues: the night shift's comment protocol (hand-offs, needs, notes) and `Blocked by: #n` lines.
- Stuck on another lane: report `waiting` with `needs`; the night shift files the ticket and parks your issue.
- A product or design question you cannot decide: list it under assumptions, or ask a person with the `needs-human` label.

## Definition of done
- Tests written or updated for the change, and the lane's test command passes.
- One line in a new file `progress/<lane>/<YYYY-MM-DD>-issue-<n>.md` (one file per change, so parallel PRs never conflict).
- A failure seen twice becomes a new rule in this file. Agents propose the rule in their PR summary; a person adds it.

## Night shift
- Only issues labeled `night-safe` run unattended. Label only tickets with clear acceptance criteria and no open design questions.
- If the default branch is red, every lane except the fix lane stops taking work until it is green.
- Kill switch: `gh nightshift pause` (sets the repo variable `AGENTS_ENABLED` to `false`).
