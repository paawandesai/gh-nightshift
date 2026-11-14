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

**Self-hosted runners.** `runners.github` and `runners.agent` choose where these jobs run. On GitHub-hosted
runners every job gets a fresh machine. A self-hosted runner is not fresh: whatever one job leaves behind, the
next job on that machine can find. So the two must be separate machines. The config refuses label sets where
either is a subset of the other (a runner carrying both would take both kinds of job) and self-hosted sets with
only generic labels (`self-hosted`, `linux`, `x64`, ...), but labels cannot prove two machines are different:
register each class on its own machine, ideally in its own runner group. Keep the `github` machine for the
token jobs alone: no CI, no Claude, no Docker group, no other org's work, since anything that runs there could
read the token. The reviewer runs Claude on PR content, so it is in the `agent` class even though it holds no PAT.

On an `agent` machine, a prompt-injected run could leave something behind for the next job, which may belong to
another lane with its own Claude token. Claude Code is installed fresh into the job's temp directory each time,
but Node's tool cache and the runner user's home persist. Register agent runners with `--ephemeral` (one job
per registration) if lanes must stay isolated from each other; otherwise treat every lane's Claude token as
reachable from every other lane on that machine.

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
tokens. The fallback lives only in its own step, `Run agent (fallback token)`, which runs only when the first step
says so; a run that does not need it never holds it. That decision reads `result.json` and the commit count, which
the agent writes, so an agent can force the fallback run, but it cannot reach the fallback token from the first
step. The pick job only checks that one of the two is set. Config refuses a fallback that is also a PAT secret, the
lane's own token, another lane's token, the planner's or a reviewer's, and `doctor` fails if the caller stops
passing it. A fallback adds a second credential an agent
could find (see known gap 2). Check that using a second account this way fits your Claude plan's terms.

**GitHub App mode.** With `auth.mode: app`, every caller passes the App's private key (`NIGHT_SHIFT_APP_KEY`)
instead of `AGENT_PAT`, so a lane still receives exactly two secrets. Only the jobs that would hold the PAT
receive the key. Each mints an installation token (`nightshift/app.py`): a JWT signed with `openssl`, then
`POST /app/installations/{id}/access_tokens` limited to this repository and to Contents, Issues, Pull requests
write, Actions and Metadata read. Workflows is never requested, and a token that comes back with Workflows,
Administration, Secrets or Environments is refused. The token is masked in the log and passed as a step output
into only the steps that name it (never `GITHUB_ENV`, which would reach every later step and action), so the
agent job never holds the key or the token. It expires after an hour.

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
- Repository security advisories: read/write, only when `redteam.enabled`: the red-team scan files its findings
  as draft advisories. In App mode only the filing job's token asks for it (`app-token --advisories`); every other
  minted token lacks it.

Every lane run executes `check-pat` before picking. It fails the run if the PAT does not authenticate or if
it is a classic PAT (which reaches every repository its owner can). See the gaps: a fine-grained PAT's
permissions cannot be read back.

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
- **Tickets an agent asks for.** A `needs` ticket in another lane is ready at once when it passes the ticket
  validator, so by default one agent can queue work for another with nobody in between (see Known gaps 12).
  Set `limits.auto_ready_needs: false` to file these as `proposed`; a person then swaps the label for
  `night-safe`.
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
  `bot_login`, `trusted_logins`, `auth`, `extends` or any secret name, and its
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

`.github/`, `.claude/`, `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `AGENT_RULES.md`, `.mcp.json`, `.night-shift/`

plus every entry in `protected_paths`, and `planner.docs` while `planner.refill_below` is on (the planner then
files tickets from them unattended). Paths are normalised first; one that escapes the repo with `..` counts
as protected. `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`, `.mcp.json` and `.claude/` are protected at any depth,
because Claude Code reads nested copies. An entry may be a glob (`**/package.json` matches at any depth). The built-in list cannot be removed by config
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

**Review rules.** `.night-shift/review-rules.yml` is read from the base checkout, never from the PR, and sits
under `.night-shift/`, a built-in protected path, so an agent cannot add, weaken or delete the rules that judge
its own PR. The runner is deterministic and stdlib only and never executes PR code. It lists changed files with
`git diff --name-only -z` and reads added lines from each file's own `-U0` hunk headers, so no line of file
content is parsed as diff syntax. Every diff runs with `--text --no-ext-diff --no-textconv
--ignore-submodules=none --no-renames`, so a PR's `.gitattributes` or `.gitmodules` cannot mark code as binary,
run a converter or hide a change, and a rename counts under both names. A file a rule selects but cannot read
(a symlink, outside the checkout, over 1 MB, not UTF-8, a lone carriage return) or parse becomes a
check-by-hand hit instead of being skipped. Rules are selectors, not a boundary: aliased imports or unusual
syntax can avoid a pattern, which is why the reviewers still read the whole diff. Security rules may not use
`unless`, which searches PR-controlled text. Snippets it
quotes are code under review; backticks are replaced so a snippet cannot close its code span. A hit the reviewer
leaves unanswered or answers `no` fails the review whatever verdict the reviewer gave, and more hits than the
cap (60) fail it too.

**Verdict reuse.** After `update-branch`, the gates may keep a passed verdict without calling Claude. They do
so only when the new head is a merge whose first parent is the PR's head before this push (`before` from the
pull_request event) and whose second parent is on the base branch, the PR's own diff against its
merge base is byte for byte the reviewed one, main, from the reviewed merge base to its current tip, changed none of the PR's files and no protected path (so
the rules, prompts, workflows and agent instruction files the reviewer saw are unchanged), and every
`review / verdict` (or `review / security`) run on the previous head passed, read from GitHub's check runs
(which comments cannot forge). The reusable `review.yml` declares no permissions of its own, because a called
workflow can only narrow its caller's grant; the generated caller grants exactly `contents: read`,
`pull-requests: write`, `issues: write`, `checks: read` and `actions: read`, and `doctor` fails a caller with no
permissions block, `write-all`/`read-all`, or any write scope besides pull-requests and issues. An agent cannot
produce such a merge with new code in it: any change to the PR's own diff forces a full review. What reuse
gives up is the reviewer re-reading the PR against main changes in other files it depends on; CI still runs
on the merged result. `review.reuse_on_update: false` turns it off.

**The security gate** (`security.enabled`) is a second job in `review.yml` with the same isolation: base
checkout as working directory, PR mounted read-only, `Read,Grep,Glob` only, no PAT. It receives one Claude
token, `security.claude_token_secret` (default: the review token), never a PAT; the config refuses a secret name
used both ways. It calls Claude only when a `security: true` rule hits, the PR touches `security.paths` or
`risk.approve_paths`, or `security.always` is on, and otherwise passes. A high or critical finding fails the
check even if the reviewer said pass. Findings on a PR are posted as PR comments (and on the issue when it
fails), so on a public repo they are public; they describe unmerged code that the gate has just kept out of main.

**The red-team scan** is different: it looks at code already on main, so its findings are live vulnerabilities.
They are never written where a public repo would show them. The scan job has the Claude token only; Claude's
output is never printed, and it leaves the job gzip'd and AES-256 encrypted with that token (a job output is
printed in the log of the job that uses it, and every log and artifact of a public repo is readable by anyone).
The filing job receives the same Claude token only as the decryption key (it never runs Claude), decrypts inside
a pipe, and creates draft private security advisories on a public repo, posting only "the scan ran" on the night
log, or `security` + `human` issues on a private repo, where the repo's visibility already limits them to
members. Finding text is the model's and is fenced and defanged. Only advisories or issues authored by a trusted
login count for deduplication (an outsider's private vulnerability report cannot suppress a finding), and the
scan's progress marker is trusted only from the PAT's login. A scan that fails, is cut short or cannot file
records nothing, so the next scan covers the same range again. On a public repo the run looks the same whether
or not anything was found: the sealed output is padded to a fixed size, a filing error prints one generic line
with no severity or count, and the token's access is checked on every run, findings or not.

The ciphertext stays in public logs for the log retention period, and it is only as safe as the Claude token
that sealed it. That token defaults to the security (or review) token, which agent jobs also hold; give the scan
its own token with `redteam.claude_token_secret`, and rotate it if it may have leaked. On a private repo, removing the
`security` label from a filed issue makes the next full scan file it again (deduplication looks at that label).
In App mode the filing job always asks for Repository security advisories, so approve that permission on the
installation even for private repos.

**Validation** (`redteam.validate`, private repos only) adds the one red-team job where a model has a shell: it
must run the repro test it writes. That job is built like a lane's agent job. It holds the red-team Claude token
only (on a pipe, not in Claude's environment), a read-only `GITHUB_TOKEN` that no step uses, and no PAT or App
key; it runs on the `agent` runner class; and nothing it produces is uploaded or printed. The step that runs the
repro test has no secret in its environment and a scrubbed one for the test itself (no credential-like variable
and none of the runner's file-command variables; Claude's step drops every `GITHUB_*` variable for the same
reason: what is written to those files runs in the later seal step), and every process the model or its test
started is stopped before the step that holds the token again to seal the result. The model does not choose the
command that runs its test: it names a runner from a fixed list, and the harness builds the argv and runs it with
no shell, then reads the runner's report. Before running, the harness checks with git, against the commit the job
checked out, that no file from main was changed or deleted and nothing was added besides the test and at most one
helper beside it. A result that
contains the token (or its base64 form) is refused when it is sealed and again when it is opened, so a test
file cannot carry the token into an issue. The verdict comes from the harness's two runs, never from the
model's account of them, and the filing job reads it only from results sealed with the scan's token. The
prompt limits attacks to the checkout and localhost; the runner still has internet access, as lane agents do.
A finding without a result is never filed and never dropped: the scan does not record progress until every
finding has a verdict.

The shared context in `.night-shift/SECURITY.md` is read from the base branch, capped at 32 KiB, fenced and
defanged, and presented to every reader as material under review. It is a protected path, so an agent PR that
touches it is rejected. Its "Not a finding" list can hide a real bug from later scans, which is why only a
person's merge can change it and why only that section's fingerprints count.

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
  changed under the PR, and must answer every hit of the repo's own review rules.
- **The security gate** (optional, `review / security`): a second read-only reviewer that looks only for
  exploitable changes, started by security rules or security paths.
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

These are not enforced. They are listed so you can judge the risk yourself.

1. **A fine-grained PAT's Workflows permission cannot be checked.** GitHub does not expose fine-grained
   permissions, so `check-pat` and `doctor` reject classic PATs and can only ask you to confirm a fine-grained
   token's permissions by hand.
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
5. **The tool repository is the root of trust.** The workflows and scripts come from
   `paawandesai/gh-nightshift` and run with your PAT or App key. `init` pins `tool_ref` to the 40-character
   commit of the release that ran it, and `doctor` warns about a tag or a branch, but a personal account still
   publishes the code. Organizations that care should fork the tool and point `uses:` and `tool_ref` at their
   fork's SHA.
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
10. **The PAT and the App key are repository secrets unless you set `environment`.** Any workflow on any
    branch can read a repository secret, so everyone with write access can. An App private key can mint tokens
    for every repository the App is installed on, with all of the App's permissions; the one-repo limit is
    chosen by the code that mints. Use one App per repository, and keep the credential in an environment
    limited to the default branch (README, Credentials in an environment). `doctor` warns until you do.
11. **Who labelled a ticket, and what it said then, is not checked.** The picker trusts the ready label
    whoever applied it (a bot, an issue template of your own), and the author can edit the title and body
    afterwards. Tickets from organization members without write access are read unfenced.
12. **Agents talk to each other.** Hand-offs and notes written by one agent appear in another's task file in
    the section marked trusted, and a note can go to any open issue. Their text is single-line and cannot
    forge markers, but it is still an agent's text.
13. **The agent job runs on the default branch's ref, with sudo unless the lane sets `sudo: false`.** A
    process with root on the runner can read the job's own credentials for the Actions cache and artifacts.
    `sudo: false` removes sudo (and Docker, except on lanes with `postgres_image`) after setup, on GitHub-hosted
    runners only. Leftover processes are stopped (GitHub-hosted runners only: a self-hosted machine may run
    another job as the same user) and the agent's artifact is downloaded by id, but do not
    restore Actions caches in workflows that hold secrets (`doctor` warns), and treat the cache of the default
    branch as writable by agents.
14. **The reviewer, planner and red-team scan can read files on their runner.** Their token reaches Claude on
    a pipe, `Read` is denied under `/proc` and `/sys` (where the step's environment holds it), and their output
    is searched for the token before it is posted, but the deny rules are Claude Code's, not the kernel's.
15. **Pinned versions need updating by hand.** Actions are pinned to commit SHAs and Claude Code to a fixed
    version (`CLAUDE_CODE_VERSION`, default 2.1.319), so fixes in them arrive only with a gh-nightshift release
    or when you raise the variable. npm resolves Claude Code's own dependencies at install time.
16. **Protected paths cover the night shift's own files.** `progress/`, your docs and other agent-read files
    are not protected. `init` writes a default `risk.approve_paths` (dependency manifests, lockfiles,
    package-manager, editor, hook and container config), which applies in `strict` mode; configs from before
    v0.2.0 do not have it. `doctor` lists scripts that workflows with secrets run, for `protected_paths`.
17. **Your repo's Actions settings are yours.** `doctor` warns when the default `GITHUB_TOKEN` is read/write,
    when Actions may approve PRs and when CI runs on `pull_request_target`, and `setup` binds the required
    checks to the GitHub Actions app, but nothing changes those settings for you. With
    `merge.allow_unenforced: true` the picker merges on its own reading of the checks. Agent branches
    (`agent/**`) have no ruleset. In `queue` merge mode GitHub writes the squash message, so closing keywords in
    an agent's commit messages work there.
18. **The planner's tickets are ready at once when a writer asked for them.** In review mode an issue written
    by someone without write access is fenced as an outside suggestion and its tickets are `proposed`; an issue a
    writer wrote is planned from directly.
19. **Cross-repo `Blocked by`** copies the title and `## Interface` section of an issue from another
    repository, but only from repositories listed in `cross_repos`. That text is quoted as data.

20. **A model that sets out to fake a validation can.** In the validate job the model has a shell on the same
    machine the harness runs on, so it could tamper with the test runner, an ignored dependency, git's own data or
    the tool, or write a test that fakes its runner's report. The harness closes the cheap and the accidental paths:
    the model never writes the command (it names a runner; the harness builds the argv and runs it without a
    shell), a missing runner or a run with no report is never `validated`, only assertion failures count (not
    fixture errors, uncaught exceptions, panics or build errors), and a checkout that differs from the scanned
    commit by more than the new test and one helper beside it is refused. A person should still read each repro
    test before trusting it, and a fix that turns the same test green is the independent check.
21. **Unreproduced findings are skipped by later scans.** Their fingerprints are listed on the night log with
    the titles, and a full scan tries them again, but a real bug whose repro test was badly written stays
    unfiled until someone reads that list. Critical ones are filed anyway, labelled `unconfirmed`.

## Reporting a vulnerability

Do not open a public issue. Report it privately through GitHub's private vulnerability reporting on
[paawandesai/gh-nightshift](https://github.com/paawandesai/gh-nightshift/security) (Security, then "Report a
vulnerability"). If that is unavailable, open an issue that asks for a private contact, with no details. Please
include the version or commit, the affected workflow or command, and steps to reproduce.
