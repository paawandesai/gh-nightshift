# Agent coordination rules

Paste this section into the project's CLAUDE.md (and AGENTS.md for non-Claude harnesses). It applies to every agent, day or night, on every teammate's machine.

## Lanes and ownership
- Every issue has exactly one `lane:<name>` label. Work only on issues in your lane.
- Each lane owns the folders listed in the table below. Edit files outside your lane only when the ticket names them.
- Shared files (package manifests, lockfiles, DB schema, route registry, `contracts/`) belong to the integrator. To change one, keep the edit minimal and say why in the PR, or ask in an issue comment.

| Lane | Owns |
|---|---|
| `field` | `field/` |
| `cloud` | `cloud/` (the Phoenix app; owns its own `mix.exs`, `mix.lock` and `priv/repo/migrations`) |
| `engine` | `engine/` and `evals/` |
| integrator (no agent lane) | `shared/`, `contracts/`, `pyproject.toml`, `constraints.txt`, `docs/`, `.github/` (CI) |

Python lanes list their own dependencies in `<lane>/requirements.txt`; the pins live in the integrator's `constraints.txt`.

## Branches and PRs
- One issue = one branch = one PR. Branch name: `agent/<lane>/issue-<n>` for agents, `<name>/issue-<n>` for humans.
- Never push to `main`. Every change lands through a PR whose checks pass (`test` and, for agent PRs, `verdict`).
- PR body starts with `Closes #<n>`.

## Contracts first
- Wave 0 merges the interfaces, stubs and contract tests before any lane starts. After that, interfaces change only through a ticket that says so.
- Build against the contract, not against another lane's implementation.

## Talking to other lanes
- Only through issue comments and labels. Stuck on another lane: comment on the blocking issue, mention its lane, add `blocked` to yours.
- A product or design question you cannot decide: comment the exact question on the issue and add `needs-human`.

## Definition of done
- Tests written or updated for the change, and the full suite passes locally.
- One line in a new file `progress/<lane>/<YYYY-MM-DD>-issue-<n>.md` (one file per change, so parallel PRs never conflict): date, lane, issue, what changed. `progress/<lane>.md` holds the older history; don't append to it.
- A failure seen twice becomes a new rule in this file. Agents propose the rule in their PR summary; a human adds it.

## Night shift
- Only issues labeled `night-safe` run unattended. Label only tickets with clear acceptance criteria and no open design questions.
- If `main` is red, every lane except `FIX_LANE` stops taking work until it is green.
- Kill switch: set the repo variable `AGENTS_ENABLED` to `false`.
