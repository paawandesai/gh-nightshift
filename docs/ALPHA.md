# Alpha testing gh-nightshift

Thanks for trying gh-nightshift. This guide gets you from nothing to a first night of agent PRs, and says what
to send back. It takes about 30 minutes for the person setting up the repo and 5 minutes for each lane owner.

gh-nightshift is alpha software. It runs Claude agents that open PRs and, once your tests and an independent
review pass, merge them into your default branch. **Start on a throwaway repo or a repo you can afford to
revert**, and keep `gh nightshift pause` handy.

## Who does what

| Role | Who | Does |
|---|---|---|
| Repo admin | One person per repo | Installs the extension, runs `init`, `setup`, `doctor` and `start`, writes the first tickets |
| Lane owner | Anyone with a Claude subscription | Stores their Claude token for one lane with `join` |

One person can be both. Each lane uses its owner's Claude token, so usage is spread across the owners' plans.

## What you need

- A GitHub repo where you are an **admin**. Setting repository secrets needs admin, so lane owners need it too,
  or the admin stores their secret for them (see "Joining a lane").
- A recent `gh` ([cli.github.com](https://cli.github.com)), logged in with the `workflow` scope: `gh auth login`, then `gh auth refresh -s workflow`.
- Python 3.10 or newer on your PATH. On Windows, install it from python.org (tick "Add python.exe to PATH") and
  run `gh` from Git Bash, PowerShell or cmd.
- Claude Code (`npm install -g @anthropic-ai/claude-code`) and a Claude Pro or Max plan, for `claude setup-token`.
- A CI workflow with a job called `test` that runs your tests on pull requests. If you have none, `init` writes a
  starter one.

## 1. Install

```sh
gh extension install paawandesai/gh-nightshift
gh nightshift --version        # gh-nightshift 0.1.2 or later
```

Upgrade later with `gh extension upgrade nightshift`. If `gh nightshift` says it cannot find Python, set
`NIGHTSHIFT_PYTHON` to your interpreter (for example `NIGHTSHIFT_PYTHON="py -3"` on Windows).

## 2. Set up the repo (repo admin)

Run these inside a clone of the repo, on an up-to-date `main`.

```sh
gh nightshift init                 # asks for lanes, folders, setup and test commands; --dry-run shows the GitHub calls
```

A **lane** is a part of the repo (for example `api/` or `web/`) that one agent works on at a time. Start with one
or two lanes. `init` suggests commands from your stack; check them, because the agent and CI will run exactly
what you enter.

`init` writes the config and workflows, creates labels, a night-log issue and repo variables, and then asks for
the secrets without echoing them:

| Secret | What to paste |
|---|---|
| `AGENT_PAT` | A [fine-grained PAT](https://github.com/settings/personal-access-tokens/new) for this repo only. Repository permissions: Contents, Issues, Pull requests **Read and write**; Actions, Commit statuses **Read**. Leave Workflows at No access. (There is no Checks permission for fine-grained PATs; that is expected.) |
| `CLAUDE_TOKEN_<OWNER>` | The output of `claude setup-token` from that lane's owner. Skip it here if the owner will run `join`. |
| `CLAUDE_TOKEN_REVIEW` | A `claude setup-token` token for the review gate, ideally from someone who does not own a lane. |

Prefer a GitHub App to a PAT for an organization? See "GitHub App" in the [README](../README.md#github-app-instead-of-a-pat).

Then commit, push and check:

```sh
git add -A && git commit -m "Set up gh-nightshift" && git push   # or open a PR and merge it
gh nightshift setup                       # branch protection: test + review / verdict must pass
gh nightshift doctor --pat-env MY_PAT     # export MY_PAT=<the PAT> first; prints a fix for every problem
```

Fix everything `doctor` marks with a cross before going on. Warnings are worth reading but do not block.

## 3. Joining a lane (lane owners)

```sh
claude setup-token                                   # copy the token it prints
gh nightshift join --lane <lane> --repo OWNER/REPO   # paste it at the hidden prompt
```

`join` works from any folder when you pass `--repo`. If it fails with a permissions error, you are not an admin:
ask the admin to grant you admin, or to run `gh secret set CLAUDE_TOKEN_<OWNER> -R OWNER/REPO` and paste your
token at their prompt. Never paste a token into an issue, a PR, a chat or this repo.

The admin can check which secrets exist (names only) with `gh secret list -R OWNER/REPO`.

**A second account as a fallback.** If a lane owner has a second Claude account, the lane can switch to it when
the first one hits its usage limit. The admin adds `fallback_token_secret: CLAUDE_TOKEN_<OWNER>_2` to that lane in
`.night-shift/config.yml`, runs `gh nightshift init --regenerate` and commits; the owner then signs in to the second
account and runs `gh nightshift join --lane <lane> --fallback`. Check that this fits your Claude plan's terms.

## 4. Write the first tickets (repo admin)

Agents pick issues labelled `night-safe` plus exactly one `lane:<name>`. Good first tickets are small and
testable: one endpoint, one component, one bug with a reproduction. Use the issue template `init` added (it files the ticket as `proposed`; swap that label for
`night-safe` when the ticket is ready), or follow `docs/NIGHT_SHIFT_ISSUES.md` in your repo, then check them:

```sh
gh nightshift lint
```

Two or three tickets per lane are enough for a first night.

## 5. Switch it on and watch

```sh
gh nightshift start        # sets AGENTS_ENABLED=true; lanes start within minutes
gh nightshift status       # what each lane is doing, open agent PRs, blocked issues
```

Lanes run on every push to main, after CI and reviews finish, every 20 minutes, and on manual dispatch from the
Actions tab. Each run logs a line (with an API-equivalent cost) on the **Night-shift log** issue.

- **Stop everything:** `gh nightshift pause` (cancels runs in flight).
- **Take a ticket yourself:** comment `/night-shift take` on it.
- **Retry a blocked ticket:** comment `/night-shift retry`, optionally with hints for the next attempt.

## Troubleshooting

| You see | What it means | Fix |
|---|---|---|
| `.night-shift/config.yml not found` | You ran a command outside the repo's clone | `cd` into the clone, or pass `--repo OWNER/REPO` |
| `has no .night-shift/config.yml on its default branch yet` | The setup files are not on main | Merge the setup commit or PR first |
| A lane run fails in `check-pat` or `pick` with HTTP 404 | The PAT is missing a permission or does not include this repo | Edit the PAT; `doctor --pat-env` names the missing permission |
| `gh secret set ... needs admin access` | You are not a repo admin | See "Joining a lane" |
| `paawandesai/gh-nightshift@vX does not exist` in `doctor` | `tool_ref` names a release that is not published | Set `tool_ref` in `.night-shift/config.yml` to a [published release](https://github.com/paawandesai/gh-nightshift/releases), run `gh nightshift init --regenerate`, commit |
| An issue gets `blocked` | Its attempts failed `max_attempts` times | Read the attempt comments on the issue, improve the ticket, comment `/night-shift retry` |
| An issue gets `needs-human` and a comment asking you to rotate a token | The agent wrote its Claude token into its output; nothing was pushed | Rotate that token (`claude setup-token`, then `join` again) and tell us |
| Runs never start an agent | `AGENTS_ENABLED` is false, main's CI is red, or no ticket is ready | `gh nightshift status` says which |
| `gh nightshift` cannot find Python (Windows) | Python is not on PATH | Reinstall Python with "Add to PATH", or set `NIGHTSHIFT_PYTHON` |

## What to report

Open an issue on [paawandesai/gh-nightshift](https://github.com/paawandesai/gh-nightshift/issues) for anything
that confused you, failed, or did something you did not expect. Include:

1. `gh nightshift --version` and your OS.
2. The command you ran and its output, or the link to the Actions run.
3. The output of `gh nightshift doctor`.
4. What you expected instead.

Check that nothing you paste contains a token. `doctor` and `status` never print one.

After a few nights, we would also like:

- `gh nightshift stats` (merge rate, close reasons, cost per merged PR).
- One or two agent PRs you thought were good, and one you thought was bad, with why.
- Anything in this guide or the README that was wrong or missing.

## Removing it

```sh
gh nightshift pause
```

Then delete `.night-shift/`, `.github/workflows/night-shift*.yml`, `.github/ISSUE_TEMPLATE/night-shift-ticket.md`,
`docs/NIGHT_SHIFT_ISSUES.md` and `AGENT_RULES.md` from the repo (and the starter `ci.yml` if `init` wrote it), remove the
`review / verdict` required check in branch protection, delete the secrets with `gh secret delete <NAME>`, revoke
the PAT, and run `gh extension remove nightshift`.

## Known limits in this alpha

- `merge.mode: queue` does not add PRs to the merge queue; use the default `strict` mode.
- The token guard catches a Claude token written into commits, results or logs, but not an agent that sets out to
  hide and send it elsewhere. See [SECURITY.md](../SECURITY.md#known-gaps).
- Costs on the night log are API-equivalent figures from Claude Code, not a bill.
- The CLI is tested on Linux and macOS in CI; Windows support is new.
