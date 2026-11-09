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
| `agent` | The lane's Claude token only, handed to Claude on a pipe (not in its environment). Checkout uses `persist-credentials: false`, and the job's `GITHUB_TOKEN` is `contents: read` and never placed in the environment. | Yes |
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

**Exactly two secrets reach each lane, by explicit name (three with a fallback Claude token).** `init` generates the caller workflow
(`.github/workflows/night-shift.yml`, built in `nightshift/scaffold.py`) with one job per lane. Each job
passes `claude_token: ${{ secrets.<that lane's token> }}` and `agent_pat: ${{ secrets.AGENT_PAT }}`. If the
lane names its own PAT, that becomes `secrets.<agent_pat_secret> || secrets.AGENT_PAT`, still with explicit
names. The workflow has no matrix, no `secrets[...]` lookup and no `secrets: inherit`. A computed secret name
makes GitHub send every repository secret to the runner. `gh nightshift doctor` fails if the caller workflow
contains `secrets[` or `secrets: inherit`, or if a lane's job does not pass that lane's token. The review
workflow receives only the review token. The planner workflow receives one Claude token and `AGENT_PAT`.
The digest workflow receives `AGENT_PAT` and, if configured, the webhook secret; no Claude token and no model.
The commands workflow receives only `AGENT_PAT`.

**Fallback Claude token.** A lane may name `fallback_token_secret`, a second Claude token (for example from its
owner's second account). The caller passes it by explicit name as `claude_token_fallback`, so that lane receives
three secrets. It is handled exactly like the lane's own token: the agent job uses it only when the first token did
not start a run (no structured output, fewer than 2 turns, no commits: a usage limit, an expired or revoked token),
hands it to Claude on the same pipe, keeps it out of the agent's environment, and the token guard searches for both
tokens. The pick job only checks that one of the two is set. Config refuses a fallback that is also a PAT secret or
the lane's own token, and `doctor` fails if the caller stops passing it. A fallback adds a second credential an agent
could find (see known gap 2). Check that using a second account this way fits your Claude plan's terms.

**GitHub App mode.** With `auth.mode: app`, every caller passes the App's private key (`NIGHT_SHIFT_APP_KEY`)
instead of `AGENT_PAT`, so a lane still receives exactly two secrets. Only the jobs that would hold the PAT
receive the key. Each mints an installation token (`nightshift/app.py`): a JWT signed with `openssl`, then
`POST /app/installations/{id}/access_tokens` limited to this repository and to Contents, Issues, Pull requests
write, Actions and Metadata read. Workflows is never requested, and a token that comes back with Workflows,
Administration, Secrets or Environments is refused. The token is masked in the log and exported only to later
steps of the same job, so the agent job never holds the key or the token. It expires after an hour.

**The PAT.** It should be a fine-grained token for this repo only, with:

- Contents: read/write
- Issues: read/write
- Pull requests: read/write
- Actions: read (the picker reads CI runs on main)
- Commit statuses: read (the picker reads a PR's statuses before merging). Fine-grained PATs cannot be given Checks,
  so with a PAT the picker reads a PR's CI from its Actions jobs instead; in App mode the App also gets Checks: read
  and sees checks posted by other apps too.
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
  under the PAT's login, so it is defanged first (`protocol.defang` turns `<!--` into `&lt;!--`), and the run's
  cost and turn count are read as numbers (`publish._cost`), because the agent's runner writes the result file;
  an agent cannot plant a trusted marker. Repo writers can still edit bot comments (see Known gaps).
- **Agent text stays in its place.** Hand-off items and assumptions are single lines (`protocol.one_line`), so
  an item cannot start a heading of its own inside a comment the next agent trusts. Closing keywords in agent
  text ("closes #12") are broken with a zero-width space (`protocol.no_close`), the PR body's own `Closes #n`
  is the only one that works, and the picker writes the squash commit's title and message itself
  (`picker.merge_body`) instead of taking the agent's commit messages. So an agent cannot close other tickets
  or the night log, or put `[skip ci]` on main. Without an open night-log issue, a lane with a budget and the
  fix-main lane stop instead of running uncounted.
- **Tickets an agent asks for.** A `needs` ticket in another lane is filed as `proposed`; a person swaps the
  label for `night-safe`. `limits.auto_ready_needs: true` restores the old behaviour (ready at once when the
  ticket passes the validator) and cannot be set from an org-wide base config.
- **Per-contributor PATs.** When lanes use their owners' own PATs, list those logins in `trusted_logins`;
  markers, PRs and night-log cost lines from them count too. The default is just the PAT's own login.
  `join` refuses a classic PAT with the `workflow` scope, and `doctor --pat-env` checks every PAT and warns
  about a PAT login missing from `trusted_logins`.
- **Roadmap and digest.** The roadmap issue is the one labelled `roadmap` *and* opened by a trusted login;
  a `roadmap` label on anyone else's issue is ignored, so it is never overwritten. Ticket titles are
  defanged in the roadmap and the digest, and escaped inside mermaid labels.
- **What the agent reads** (`nightshift/task.py`):
  1. The issue title and body. If the issue's author is not an owner, member or collaborator of the repo, the body is fenced and labelled
     as a request from an outsider, because they can edit it after a maintainer labels it.
  2. A "Coordination" section with marker-carrying comments from the PAT login, marked trusted.
  3. A "Discussion" section with comments from the repo's owners, members and collaborators, the PAT
     login and `github-actions[bot]` (that exact login, which no user can register). Each is fenced in a
     code block and labelled as data, never instructions.

  Comments from anyone else are dropped entirely. "Member" is GitHub's `author_association`: in an
  organization it includes members who have no write access to this repo (see Known gaps). The worker prompt
  repeats that the task file describes work and does not override the rules.
- **The planner.** `/night-shift plan` runs only when the commenter has write, maintain or admin permission
  (`planner.can_request_plan`). The request is fenced as data in the planning context. The comment body
  reaches the script through an environment variable, never through `${{ }}` interpolation into shell.
- **Comment commands.** `/night-shift take` and `/night-shift retry` act only for commenters with write,
  maintain or admin permission (checked over REST) and only on issues with a lane label. The comment reaches
  the script through an environment variable. `retry` unassigns only people named in trusted `take` markers.
- **CODEOWNERS.** Read from the default branch, never the PR. With `risk.codeowners` on, root and `docs/`
  CODEOWNERS are protected paths (`.github/` already is), and a CODEOWNERS that cannot be read (anything but
  404) blocks the merge instead of skipping the owner review.
- **Risk tiers.** An approval counts only from a person with write access who did not open the PR, on the
  PR's current head commit, and only if that person's latest review is an approval. Bots never count.
- **Cross-repo hand-offs.** Text from another repository (title, `## Interface`) is defanged, fenced and
  labelled as data; its trust markers are never read, because that repo's PAT login is not this repo's.
- **Org-wide base config.** `.night-shift/base.yml` cannot set lanes, labels, the merge mode, `tool_ref`,
  `bot_login`, `trusted_logins`, `auth`, `extends`, `limits.auto_ready_needs` or any secret name, and its
  `risk` settings can only make approval stricter (`planner.claude_token_secret` and `digest.webhook_secret`
  are dropped from it), so whoever controls the org repo cannot widen trust or route a secret. Its other
  settings are not clamped: a base can raise `limits` (attempts, turns) or switch on planner refill. It lives
  under `.night-shift/`, which agents cannot change.
- **Audit log export.** CSV cells starting with `=`, `+`, `-` or `@` are prefixed with `'` so a crafted model
  name cannot run as a spreadsheet formula. Only PRs and night-log lines from trusted logins are exported.
- **What agents may pick.** Only issues with the ready label (`night-safe`) and exactly one lane label are
  picked. Only people with triage or write access can add labels by hand. An issue template's `labels:` are
  applied for whoever opens the issue, so the ticket template `init` installs files tickets as `proposed`,
  never `night-safe`. The picker does not check who applied the label (see Known gaps).

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
  `review / verdict`) and `enforce_admins: true`. It adds to a protection that is already there and never
  removes from it: required reviews, push restrictions and other required checks are kept, and it prints what
  it kept and changed (`repo_setup.merged_protection`). If the current protection cannot be read, it changes
  nothing. The PAT cannot merge past a failing or missing check even
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
- Both also cancel lane runs that are queued, waiting or in progress (`POST actions/runs/{id}/cancel`), so
  an in-flight publish job cannot still open a PR. Cancelling needs Actions write, which your own `gh`
  login has and the PAT must not; run them as yourself. `--no-cancel` lets runs in flight finish. A run
  that reaches its publish step in the second between the variable flip and the cancel can still open a
  PR; nothing merges while `AGENTS_ENABLED` is false.

## Known gaps

These are not enforced by v0.1. They are listed so you can judge the risk yourself.

1. **A fine-grained PAT's Workflows permission cannot be checked.** GitHub does not expose fine-grained
   permissions, so `check-pat` and `doctor` reject only a classic PAT with the `workflow` scope. For
   fine-grained tokens, they warn and ask you to confirm by hand.
2. **The agent has a full shell and network.** The default `CLAUDE_PERMISSION_FLAGS` is
   `--dangerously-skip-permissions`. Two things limit what that means for the lane's Claude token:
   - Claude reads the token from a pipe, not from its environment, so the commands it runs (tests, package
     scripts, `env`) do not inherit it. Set the repo variable `NIGHT_SHIFT_TOKEN_IN_ENV=true` only for a Claude
     Code version that cannot read `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`.
   - A token guard searches the commits, the result and the log for the token (and its base64 form) before
     anything leaves the runner. On a hit nothing is uploaded or pushed, and the issue gets `needs-human`.

   Neither stops an agent that sets out to find and send its token: it shares a user account with the step
   that holds it and can reach the internet, and the guard only knows the token's plain and base64 forms.
   Treat each Claude token as exposed to its lane's agent, keep untrusted people from filing night-safe
   issues, and rotate a token you have doubts about. The runner holds no GitHub write credentials and is
   thrown away. Set `CLAUDE_PERMISSION_FLAGS` to restrict tools if you need to.
3. **Your CI runs agent-written code.** Agent PRs come from branches in the same repo, so your CI's
   `pull_request` jobs get your repository secrets. Keep secrets out of test jobs.
4. **The reviewer reads untrusted code.** Text in the PR can try to talk the reviewer into a `pass`. It
   cannot write anything, and the `test` check is still required, but the gate is a model, not a proof.
5. **Moving `tool_ref`.** By default, the workflows and scripts come from `paawandesai/gh-nightshift@main`
   and run with your PAT. `init` pins `tool_ref` to the release tag of the extension that ran it. A tag can be
   moved by whoever can push to the tool repo; a 40-character commit SHA cannot. Pin a SHA (ideally of a fork
   your organization owns) when the App key or PAT matters. `doctor` warns only while it follows a branch.
6. **Commits are not signed.** The github-actions[bot] author is set with `git config` in a job the agent
    controls, so the author field is not proof of origin. The PR author (the PAT login) is.
7. **Repo collaborators with write access are trusted.** They can label issues `night-safe`, edit
    comments, including the PAT's marker comments, and edit PR bodies. The trust rule keeps out outsiders,
    not your own team.
8. **Interface differences in a hand-off are self-reported.** The hand-off shows the ticket's interface next to
   the one the agent says it built, but nothing compares them mechanically.
9. **The digest webhook URL is a bearer secret.** Anyone holding a Slack or Discord webhook URL can post to
   that channel. It is passed by explicit name (`digest.webhook_secret`) only to the digest workflow, never
   to a lane, and the tool never prints it, but it lives in the same job as the PAT.
10. **The PAT and the App key are repository secrets.** Any workflow on any branch can read a repository
    secret, so everyone with write access can. An App private key can mint tokens for every repository the App
    is installed on, with all of the App's permissions; the one-repo limit is chosen by the code that mints.
    Use one App per repository, or store the key as an environment secret limited to the default branch.
11. **Who labelled a ticket, and what it said then, is not checked.** The picker trusts the ready label
    whoever applied it (a bot, an issue template of your own), and the author can edit the title and body
    afterwards. Tickets from organization members without write access are read unfenced.
12. **Agents talk to each other.** Hand-offs and notes written by one agent appear in another's task file in
    the section marked trusted, and a note can go to any open issue. Their text is single-line and cannot
    forge markers, but it is still an agent's text.
13. **The agent job runs on the default branch's ref with sudo.** A process with root on the runner can read
    the job's own credentials for the Actions cache and artifacts. Do not restore Actions caches in workflows
    that hold secrets, and treat the cache of the default branch as writable by agents.
14. **The reviewer and the planner hold their Claude token in the environment** and can read any file on
    their runner. A reviewer talked into it could quote the token in its review comment.
15. **Actions and Claude Code are not pinned to commits.** The workflows use `actions/checkout@v4`,
    `upload-artifact@v4`, `download-artifact@v4` and `@anthropic-ai/claude-code@latest` (set the repo variable
    `CLAUDE_CODE_VERSION` to pin the last one).
16. **Protected paths cover the night shift's own files only.** `AGENT_RULES.md`, `progress/`, your docs,
    dependency manifests and lockfiles, package-manager config, scripts your workflows call, and editor or
    container config are not protected. Add what matters to `protected_paths` or `risk.approve_paths`.
17. **Your repo's Actions settings are not checked.** If the default `GITHUB_TOKEN` is read/write, agent code
    running in your CI can post commit statuses and comments. The required checks are registered by name and
    are not tied to the GitHub Actions app. In `queue` merge mode GitHub writes the squash message, so closing
    keywords in an agent's commit messages work there.
18. **The planner's tickets are ready at once.** In review mode it reads the issue's title and body, which an
    outsider may have written; with refill on it plans from docs that agents can edit.
19. **Cross-repo `Blocked by`** makes the night shift copy the title and `## Interface` section of an issue
    from any repository its token can read. Use a fine-grained PAT or the App, which reach one repository.

## Reporting a vulnerability

Do not open a public issue. Report it privately through GitHub's private vulnerability reporting on
[paawandesai/gh-nightshift](https://github.com/paawandesai/gh-nightshift/security) (Security, then "Report a
vulnerability"). If that is unavailable, open an issue that asks for a private contact, with no details. Please
include the version or commit, the affected workflow or command, and steps to reproduce.
