# Security

gh-nightshift lets an LLM write code that merges into your default branch while nobody is watching. This file
covers what it defends against, how, and where the defenses stop. Each claim names the code that enforces
it. Anything not enforced is listed under [Known gaps](#known-gaps).

## Assets and attackers

- **Assets:** the GitHub PAT (write access to code, issues and PRs), the Claude tokens, the default branch,
  your CI and review rules, and any secrets your CI uses.
- **Attackers:**
  1. A confused or prompt-injected agent.
  2. Outsiders who can comment on or open issues.
  3. Malicious content in issues, PRs or dependencies.
  4. A compromise of the gh-nightshift repo itself, if you follow a moving ref.

## Tokens

**The LLM never holds the GitHub PAT.** `lane.yml` splits each lane run into three jobs, each on a fresh
runner:

| Job | Secrets it sees | Runs Claude? |
|---|---|---|
| `pick` | The PAT. The lane's Claude token appears in one step, which only checks that it is not empty. | No |
| `agent` | The lane's Claude token only (`CLAUDE_CODE_OAUTH_TOKEN`). Checkout uses `persist-credentials: false`, and the job's `GITHUB_TOKEN` is `contents: read` and never placed in the environment. | Yes |
| `publish` | The PAT | No |

The agent's work crosses to the publish job only as two artifacts, `result.json` and a git bundle. Lane setup
(commands, composite action, Postgres service) runs only in the agent job. The planner uses the same split:
`context` (read-only `GITHUB_TOKEN`), `draft` (Claude token only, tools limited to `Read,Grep,Glob` with Bash, Write, Edit and web tools explicitly disallowed), and
`create` (PAT, no Claude).

**Repo code never runs with the PAT.** Every job that holds the PAT runs the tool with `PYTHONSAFEPATH=1` and an
absolute `PYTHONPATH` pointing at the tool checkout, so a `json.py` or `nightshift/` planted in your repo is never
imported. Pushes take the PAT from `$GH_TOKEN` through a credential helper set in the environment, so it never
appears on a command line (`ps`) or in a git error message. Config refuses a secret name used both as a Claude
token and as a PAT.

**Exactly two secrets reach each lane, by explicit name.** `init` generates the caller workflow
(`.github/workflows/night-shift.yml`, built in `nightshift/scaffold.py`) with one job per lane. Each job
passes `claude_token: ${{ secrets.<that lane's token> }}` and `agent_pat: ${{ secrets.AGENT_PAT }}`. If the
lane names its own PAT, that becomes `secrets.<agent_pat_secret> || secrets.AGENT_PAT`, still with explicit
names. The workflow has no matrix, no `secrets[...]` lookup and no `secrets: inherit`. A computed secret name
makes GitHub send every repository secret to the runner. `gh nightshift doctor` fails if the caller workflow
contains `secrets[` or `secrets: inherit`, or if a lane's job does not pass that lane's token. The review
workflow receives only the review token. The planner workflow receives one Claude token and `AGENT_PAT`.

**The PAT.** It should be a fine-grained token for this repo only, with:

- Contents: read/write
- Issues: read/write
- Pull requests: read/write
- Actions: read (the picker reads CI runs on main)
- **Workflows: no access.** Without it, GitHub refuses any push that changes `.github/workflows/`, a second
  wall behind the protected-path check.

Every lane run executes `check-pat` before picking. It fails the run if the PAT does not authenticate or if
a classic PAT has the `workflow` scope. See the gaps: a fine-grained PAT's permissions cannot be read back.

**Commit identity.** The agent job sets `user.name github-actions[bot]` and the matching noreply email before
Claude runs, so agent commits are authored as github-actions[bot]. They are not cryptographically signed. The
push and the PR come from the PAT's account.

## Prompt injection through issues and comments

- **Trusted markers.** Only comments posted by the PAT's own login count as protocol markers
  (`nightshift/protocol.py`, `is_trusted`). Attempt counts, blocks, hand-offs, needs and notes cannot be
  forged by outsiders. Text the agent writes (summary, assumptions, notes, needs, hand-off) is re-posted
  under the PAT's login, so it is defanged first (`protocol.defang` turns `<!--` into `&lt;!--`); an agent
  cannot plant a trusted marker. Repo writers can still edit bot comments (see Known gaps).
- **What the agent reads** (`nightshift/task.py`):
  1. The issue title and body. If the issue's author is not an owner, member or collaborator of the repo, the body is fenced and labelled
     as a request from an outsider, because they can edit it after a maintainer labels it.
  2. A "Coordination" section with marker-carrying comments from the PAT login, marked trusted.
  3. A "Discussion" section with comments from the repo's owners, members and collaborators, the PAT
     login and GitHub Actions bots. Each is fenced in a code block and labelled as data, never
     instructions.

  Comments from anyone else are dropped entirely. The worker prompt repeats that the task file describes
  work and does not override the rules.
- **The planner.** `/night-shift plan` runs only when the commenter has write, maintain or admin permission
  (`planner.can_request_plan`). The request is fenced as data in the planning context. The comment body
  reaches the script through an environment variable, never through `${{ }}` interpolation into shell.
- **What agents may pick.** Only issues with the ready label (`night-safe`) and exactly one lane label are
  picked. Only people with triage or write access can add labels.

## Protected paths

The publish job (`nightshift/publish.py`, `push_and_open_pr`) fetches the agent's bundle into a fresh
checkout of main. It lists the changed files with `git -c core.quotePath=false diff --name-only --no-renames -z
origin/main...<branch>` (so a rename lists both the old and the new path, and unusual names are not quoted) and
rejects the whole run, pushing nothing, if any file is under:

`.github/`, `.claude/`, `CLAUDE.md`, `AGENTS.md`, `.mcp.json`, `.night-shift/`

plus every entry in `protected_paths`. Paths are normalised first; one that escapes the repo with `..` counts
as protected. `CLAUDE.md`, `AGENTS.md`, `.mcp.json` and `.claude/` are protected at any depth, because Claude
Code reads nested copies. The built-in list cannot be removed by config
(`config.BUILTIN_PROTECTED`). Agents therefore cannot change CI, the review workflow, their own rules or
the night-shift config. The planner turns tickets that touch these paths, or `planner.human_paths`, into
`human` tickets that agents skip.

## The reviewer

`review.yml` checks out the base commit (`pull_request.base.sha`) and the PR head into separate folders. It
runs Claude with its working directory in the base checkout:

- The prompt comes from the base branch's `.night-shift/prompts/review.md` if present, else from the
  gh-nightshift default.
- `CLAUDE.md` and `.claude/` are read from the base branch.

The PR's code is added as a read-only folder (`--add-dir`) with tools limited to `Read,Grep,Glob`. The
prompt says to treat the PR's contents, including any `CLAUDE.md` in it, as material under review. The
GitHub token in the job is used only by the script steps, never by Claude's step. The review workflow
itself lives under `.github/`, which agents cannot change.

## Auto-merge

The picker merges a lane's PR itself (squash, via the PAT) when it is green. It does not use GitHub's
auto-merge. The risk is that a wrong but plausible change lands on main overnight. Mitigations:

- **Required checks enforced by GitHub.** `setup` protects main with the required checks (`test` and
  `review / verdict`) and `enforce_admins: true`. The PAT cannot merge past a failing or missing check even
  if its owner is an admin. The picker also counts a PR as green only after every required check has
  reported.
- **Strict mode.** PRs must be up to date with main (`strict: true`). A PR behind main is updated and
  re-checked before it can merge, so CI tests what will land. `doctor` fails when `strict` is off in strict
  mode.
- **The review gate,** an independent Claude run on the base branch's rules. It also checks whether main
  changed under the PR.
- **Measure the gate.** `gh nightshift audit` samples merged agent PRs and reruns their tests with the
  implementation reverted. `gh nightshift stats --audit audit.csv` estimates the review gate's catch rate,
  with the stated assumptions. LLM labels are not ground truth; a human should label the sample.
- No human PR review is required by the generated protection. That is the point of the tool. If you want
  one, add it in branch protection, and PRs will wait for a person.

## Code freeze and kill switch

- `gh nightshift pause` sets the repo variable `AGENTS_ENABLED` to `false`. The caller workflow's lane jobs
  run only when it is `true`, and the picker checks it again. `gh nightshift start` turns it back on. You
  can flip the variable from the GitHub web UI or mobile app; no commit is needed.
- `gh nightshift freeze` pauses and moves every ready issue to `human` + `frozen`, so nothing is picked even
  if someone turns the switch back on. `gh nightshift resume` undoes both.
- Neither command cancels a run already in progress. Its publish job can still open a PR. Nothing merges
  while paused, because merging happens in the next pick job, which does not run. To stop an in-flight
  agent immediately, cancel the run (`gh run cancel <id>`).

## Known gaps

These are not enforced by v0.1. They are listed so you can judge the risk yourself.

1. **A fine-grained PAT's Workflows permission cannot be checked.** GitHub does not expose fine-grained
   permissions, so `check-pat` and `doctor` reject only a classic PAT with the `workflow` scope. For
   fine-grained tokens, they warn and ask you to confirm by hand.
2. **The agent has a full shell and network.** The default `CLAUDE_PERMISSION_FLAGS` is
   `--dangerously-skip-permissions`. The agent can read its own lane's Claude token from the environment
   and reach the internet. The runner holds no GitHub write credentials and is thrown away, but treat each
   Claude token as exposed to its lane's agent. Set `CLAUDE_PERMISSION_FLAGS` to restrict tools if you
   need to.
3. **Your CI runs agent-written code.** Agent PRs come from branches in the same repo, so your CI's
   `pull_request` jobs get your repository secrets. Keep secrets out of test jobs.
4. **The reviewer reads untrusted code.** Text in the PR can try to talk the reviewer into a `pass`. It
   cannot write anything, and the `test` check is still required, but the gate is a model, not a proof.
5. **Moving `tool_ref`.** By default, the workflows and scripts come from `paawandesai/gh-nightshift@main`
   and run with your PAT. Pin `tool_ref` to a release tag or commit SHA. `doctor` warns while it follows a
   branch.
6. **Commits are not signed.** The github-actions[bot] author is set with `git config` in a job the agent
    controls, so the author field is not proof of origin. The PR author (the PAT login) is.
7. **Repo collaborators with write access are trusted.** They can label issues `night-safe`, edit
    comments, including the PAT's marker comments, and edit PR bodies. The trust rule keeps out outsiders,
    not your own team.
8. **`pause` and `freeze` do not cancel a run already in progress.** Its publish job can still open a PR;
   nothing merges while `AGENTS_ENABLED` is false. Cancel the run in the Actions tab if you need it gone now.
9. **Interface differences in a hand-off are self-reported.** The hand-off shows the ticket's interface next to
   the one the agent says it built, but nothing compares them mechanically.

## Reporting a vulnerability

Do not open a public issue. Report it privately through GitHub's private vulnerability reporting on
[paawandesai/gh-nightshift](https://github.com/paawandesai/gh-nightshift/security) (Security, then "Report a
vulnerability"). If that is unavailable, open an issue that asks for a private contact, with no details. Please
include the version or commit, the affected workflow or command, and steps to reproduce.
