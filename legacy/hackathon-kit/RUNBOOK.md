# Night-shift runbook

Agents run on GitHub's runners after every merge to main (and every 20 minutes as a backstop), one per lane, so laptops can sleep. Each run: pick the next ready issue in the lane, run Claude Code headless on it, open a PR, and auto-merge it once `test` (CI) and `verdict` (independent Claude review) pass.

```
cron */20 ─► night-shift.yml ─► night-shift-lane.yml, once per lane (one run at a time per lane)
   job pick     (AGENT_PAT)       .github/night-shift/agent_pick.py: kill switch → lane busy? → main red? → next unblocked issue
                                  writes task.md from the issue + member comments only, records the attempt
   job agent    (Claude token)    claude -p on its own runner, no GitHub write access → result.json + git bundle
   job publish  (AGENT_PAT)       clean runner: pushes, opens PR, auto-merge
PR ─► ci.yml (test) + agent-review.yml (verdict, prompt and CLAUDE.md taken from main) ─► auto-merge into main
      failed / conflicted / stale PR ─► picker closes it, issue returns to the queue (max 3 attempts → blocked)
      run that never started (expired token, usage limit, setup failure) ─► refunded, capped at 6 per issue
```

**Who holds which secret.** Each lane run gets exactly two secrets (its Claude token and `AGENT_PAT`), and the agent's machine gets only the Claude token. **There is no protected-path guard** (removed on request): agent PRs may change any file. One limit remains: `AGENT_PAT` has no Workflows permission, so GitHub still rejects an agent push that edits `.github/workflows/`.

## Setup (about 45 minutes; do it on a throwaway repo first)

1. **Copy the kit** into the repo root: `.github/` (workflows plus `night-shift/` with the picker, prompts, setup script and tests), `AGENT_RULES.md`, `RUNBOOK.md`. Paste `AGENT_RULES.md` into `CLAUDE.md`.
2. **Edit the lanes** in `.github/workflows/night-shift.yml` (`matrix.include`). Lane names must match the `lane:<name>` labels.
3. **Run setup:** `.github/night-shift/setup_repo.sh <owner/repo> <lane> <lane> ...` creates labels, enables auto-merge, sets `AGENTS_ENABLED=false` and `FIX_LANE`, creates the night-log issue (pin it), and protects `main` for everyone, admins included (so humans merge through PRs too).
4. **Add secrets** (Settings → Secrets and variables → Actions):
   - `AGENT_PAT`: fine-grained token limited to this repo. Contents, Issues and Pull requests: read and write. Actions: read (without it the picker can't see CI and every lane waits). **Do not grant Workflows**: that is what stops an agent's PR from editing CI or the review gate. A token is needed instead of the built-in one because pushes and PRs made with the built-in token don't start CI.
   - `CLAUDE_TOKEN_<PERSON>`: each person runs `claude setup-token` and sets their own secret (`gh secret set CLAUDE_TOKEN_<PERSON> -R <owner/repo>`, paste). Each lane's `token_secret` points at its owner's secret, so a person's lanes all spend their plan.
   - `CLAUDE_TOKEN_REVIEW`: from someone else, for the review gate.
   - Optional `AGENT_PAT_<PERSON>`: a lane owner's own token with exactly the `AGENT_PAT` permissions (still no Workflows). Lanes mapped to it in `night-shift.yml` open their PRs as that person, so GitHub credits the squash commits to them instead of the `AGENT_PAT` owner. Today the `cloud` lane uses `AGENT_PAT_DEBU` and falls back to `AGENT_PAT` while it is unset. The pick job logs which account a lane publishes as. Switching a lane's token resets its issues' attempt counts, because the picker only counts markers posted by the lane's own token account.
5. **Replace the CI test step** in `ci.yml` with the real stack commands once Wave 0 picks the stack.
6. **Turn it on:** set the variable `AGENTS_ENABLED=true`.

## From Idea Factory to running lanes
1. The Idea Factory's github stage publishes issues labeled `epic:E01`, `wave:W01`, ..., with a "Blocked by: #n" links block. The picker reads those formats directly.
2. Map epics to lanes: `.github/night-shift/assign_lanes.sh <owner/repo> E01=api E02=web E03=data`. It lists any issue left without a lane.
3. Lane owners label what agents may do alone: `gh issue edit 12 13 14 -R <owner/repo> --add-label night-safe`.
4. Humans claim a ticket by assigning themselves; the picker skips assigned issues.

## Rehearsal (do before the event)
1. Create two small issues in one lane, labeled `lane:<x>`, `night-safe`, `wave:1`. Make the second say `Blocked by #<first>`.
2. Actions → night-shift → Run workflow with that lane.
3. Check: the run shows three jobs (pick, agent, publish), the attempt comment appears on issue 1, a PR `agent/<x>/issue-<n>` opens, `test` and `verdict` run, the PR merges, issue 1 closes.
4. Run again: it should now pick issue 2.
5. Break it on purpose: set `AGENTS_ENABLED=false` and confirm the next run does nothing.

## Before you sleep
- [ ] Main is green and the demo path works end to end.
- [ ] Each lane has 6–8 hours of `night-safe` issues with clear acceptance criteria and nothing waiting on a human decision.
- [ ] Each lane also has filler issues (tests, error handling, docs) so it doesn't sit idle when its main work is blocked.
- [ ] Lane tokens are fresh, and everyone has usage left on their plan.
- [ ] Someone has the kill switch on their phone: GitHub mobile → repo → Settings → Variables → `AGENTS_ENABLED`.

## Morning
1. Read the pinned night-log issue.
2. Triage `needs-human` and `blocked` issues; answer the questions in comments, then remove the label.
3. Skim merged agent PRs from the night and run the demo path.

## Troubleshooting
| Symptom | Likely cause |
|---|---|
| Workflow never starts | `AGENTS_ENABLED` not `true`; scheduled runs only run from the default branch; GitHub's cron can lag by several minutes |
| "Secret CLAUDE_TOKEN_X is empty" | Secret name doesn't match `token_secret` in the matrix |
| PR opens but no checks run | PR was created with the built-in token instead of `AGENT_PAT` |
| Lane stuck on "still in review" | A PR is waiting on checks. Failed, conflicted, or checkless after 45 min PRs are closed automatically |
| Every lane skips with "main is red" | Expected: `FIX_LANE` is fixing it. After 3 tries on the same failing commit it stops ("fix-main gave up"); fix main by hand |
| Every lane skips with "cannot read CI status" | `AGENT_PAT` is missing Actions: read |
| Night log shows "infra-error" | Lane token expired or plan usage ran out. The attempt wasn't counted; refresh the token or wait for the usage window |
| Publish fails when pushing a branch | The agent edited `.github/workflows/`, which `AGENT_PAT` may not push (no Workflows permission); make that change yourself |
| PR closed with `review-error` label | The review token expired or ran out of usage; the attempt was not counted. Refresh `CLAUDE_TOKEN_REVIEW` |
| Issue `blocked` after "runs that never started" | That lane's Claude token is expired or out of usage |
| Branch protection step fails | Private repos on free plans can't use it. Make the repo public for the hackathon |

## Model routing
- **First try:** the lane's `model` in `night-shift.yml` (default `sonnet`: fast, and spares everyone's usage).
- **Retry or fix-main:** `ESCALATE_MODEL` (default `opus`). A ticket that failed once gets the stronger model.
- **Per ticket:** add a label like `model:opus` or `model:haiku` to override (create the label first).
- **Review gate:** `REVIEW_MODEL` (default `opus`): the last check before main is worth the stronger model.
- The night log shows `[model]` on every line, so in the morning you can see which tickets needed escalation.

## Tunables (repo variables)
`READY_LABEL` (night-safe) · `FIX_LANE` · `CI_WORKFLOW` (ci.yml) · `MAX_TURNS` (80) · `CLAUDE_CODE_VERSION` (latest; pin after the rehearsal) · `CLAUDE_PERMISSION_FLAGS` (`--dangerously-skip-permissions`; the runner is disposable and holds no push credentials) · `ESCALATE_MODEL` (opus) · `REVIEW_MODEL` (opus)

## Deploy cloud to Fly
Config: `cloud/fly.toml`. Its app name `rescue-cloud-demo` is a **placeholder**; pick your own below (`<app>`). Nothing here has been deployed yet and no credentials are in the repo. Run every command from the **repo root** (the image builds from the root so it can ship the frozen engine build), logged in with `fly auth login`.

What `fly.toml` already does: builds `cloud/Dockerfile`; runs `/app/bin/migrate` as the `release_command` before each release (creates the DB if missing, then migrates); listens on `PORT=8080` = `internal_port`; HTTP health check `GET /healthz` (200 when the DB answers, 503 otherwise; excluded from the https redirect in `config/prod.exs`); one `shared-cpu-1x` 512 MB machine with auto-stop off for the demo; `ECTO_IPV6=true` and `ERL_AFLAGS=-proto_dist inet6_tcp` because Fly Postgres is IPv6-only on the private network.

```sh
# 1. Create the app from the checked-in config, without deploying. Answer "yes" to copying the config,
#    "no" to any Postgres/Redis offer (step 2 does it) and "no" to deploying now.
fly launch --copy-config --no-deploy --config cloud/fly.toml --name <app>
#    If it did not rewrite it, set app = "<app>" in cloud/fly.toml (do not commit your real app name if you prefer).

# 2. Create a Postgres cluster and attach it (attach sets the DATABASE_URL secret on <app>).
fly postgres create --name <app>-db --region iad --initial-cluster-size 1 --vm-size shared-cpu-1x --volume-size 1
fly postgres attach <app>-db --app <app>

# 3. Secrets. PHX_HOST is a secret, not in fly.toml, so no placeholder host can leak into a deploy.
#    DEVICE_TOKENS is "device_id:token" pairs, comma-separated; give each laptop its own long random token
#    and put the same value in that laptop's DEVICE_TOKEN.
fly secrets set --app <app> \
  SECRET_KEY_BASE="$(openssl rand -base64 48)" \
  PHX_HOST="<app>.fly.dev" \
  DEVICE_TOKENS="laptop-dev-01:<long-random-token>,demo-field-01:<long-random-token>,laptop-02:<long-random-token>"

# 4. Deploy (builds the image, runs bin/migrate, starts the machine once /healthz passes).
fly deploy . --config cloud/fly.toml --app <app>

# 5. Check it.
fly status --app <app>                 # machine "started", check "passing"
curl -i https://<app>.fly.dev/healthz  # HTTP 200 {"status":"ok"...}; 503 means the DB is unreachable
fly logs --app <app>                   # if the release_command or the check fails
```

Each `fly.toml` names its Dockerfile relative to its own folder (`dockerfile = "Dockerfile"`), which is how flyctl resolves it; the `.` argument keeps the build context at the repo root. Do not add `--dockerfile cloud/Dockerfile`: flyctl then looks for `cloud/cloud/Dockerfile`.
If the build hangs at "Waiting for depot builder" or fails TLS behind a corporate or sandbox proxy, add `--depot=false` to use Fly's own remote builder.

Prod loads none of the committed dev tokens (`config/devices.exs`), so `DEVICE_TOKENS` must list every device that syncs and must include `demo-field-01:<token>` for the demo (the prod cloud accepts only `DEVICE_TOKENS`); a device left out gets 401.
Then point each field laptop at it: `CLOUD_URL=https://<app>.fly.dev`, `DEVICE_ID`/`DEVICE_TOKEN` matching a `DEVICE_TOKENS` pair.
If the deploy fails at `release_command` with a connection error, check `fly secrets list --app <app>` shows `DATABASE_URL` (re-run `fly postgres attach`) and that `ECTO_IPV6` is still in `[env]`.
If the dashboard misses requests that devices synced (their outbox is empty), or `fly logs` shows `projection: project <request_id> failed`, fix the cause and rebuild the read model from the stored events (safe while devices sync; it never deletes events): `fly ssh console --app <app> -C "/app/bin/rescue_cloud eval 'RescueCloud.Release.rebuild_projection()'"`. See `cloud/README.md`, "Rebuild the dashboard".

## Deploy hosted field demo to Fly
Config: `field/fly.toml`. Its app name `rescue-field-demo` is a **placeholder** (`<app>` below). Nothing here has been deployed and no credentials are in the repo. Run every command from the **repo root** (the image builds from the root), logged in with `fly auth login`. Deploy the cloud first: you need its URL.

The cloud's device map must contain `demo-field-01` with the same token you give the field app: add `demo-field-01:<long-random-token>` to the cloud's `DEVICE_TOKENS` secret (the DEVICE_TOKENS handling is being fixed in the step-3 PR, finding A6; check it is merged first).

```sh
# 1. Create the app from the checked-in config, without deploying.
fly launch --copy-config --no-deploy --config field/fly.toml --name <app>

# 2. Volume for the SQLite file (mounted at /data; must be in the app's region).
fly volumes create field_data --size 1 --region iad --app <app>

# 3. Secrets (never in fly.toml).
fly secrets set --app <app> DEVICE_TOKEN=<long-random-token> CLOUD_URL=https://<cloud-app>.fly.dev

# 4. Deploy. fly.toml pins the trained model (gated repo), so the build needs a read-only HF token.
fly deploy . --config field/fly.toml --app <app> --build-secret HF_TOKEN=<hf-token>

# 5. Check it.
curl https://<app>.fly.dev/sync/status
```

### Bake the trained model into the field image
`field/fly.toml` `[build.args]` pins the model (currently the soup pin `8f932d7`, the average of v8, v9 and v10, #104), so every Fly deploy from the checked-in config installs CPU torch, downloads the gated weights (slower build) and needs `--build-secret HF_TOKEN=...`. `--build-arg HF_REPO=... --build-arg HF_REVISION=...` on the command line overrides that pin. The keyword-only image is now a local `docker build` / `docker compose build` without build args.
Without build args the image has no torch and uses the keyword extractor. With both `HF_REPO` and `HF_REVISION` (a 40-hex commit SHA) the build installs CPU torch, downloads the weights into `/models/current`, and the app runs fully offline (`HF_HUB_OFFLINE=1`).
The HF repo is **gated** (CC BY-NC-SA, research-only, #213). Pass the token as a build secret only, never as an arg or env:
```bash
# override the fly.toml pin with another revision:
fly deploy . --config field/fly.toml --app <app> \
  --build-arg HF_REPO=<org>/<model> --build-arg HF_REVISION=<40-hex-sha> --build-secret HF_TOKEN=<hf-token>
# local / compose-style build:
HF_TOKEN=<hf-token> docker build -f field/Dockerfile --secret id=HF_TOKEN,env=HF_TOKEN \
  --build-arg HF_REPO=<org>/<model> --build-arg HF_REVISION=<40-hex-sha> .
```

The model build needs the 4 GB VM that `field/fly.toml` sets (`[[vm]]`, with a 60 s health-check grace period for model load); Fly's 256 MB default is too small. To bake the model for a local compose rehearsal (token still only as a build secret, see above): `HF_REPO=<org>/<model> HF_REVISION=<40-hex-sha> docker compose build field`.

## Run locally
Whole stack (Postgres, cloud, field) with Docker:
1. `cp .env.example .env` and set `SECRET_KEY_BASE` (`openssl rand -base64 48` or `mix phx.gen.secret`).
2. `docker compose up --build`. The cloud waits for a healthy db, runs `bin/migrate`, then serves on http://localhost:4000; the field app is on http://localhost:8000.
3. Try it: `curl -XPOST localhost:8000/requests -H 'content-type: application/json' -d '{"raw_text":"3 people on a roof at Elm St"}'`, then check `curl localhost:8000/sync/status` (`outbox_backlog` 0) and the cloud dashboard.
4. `docker compose down -v` stops it and wipes the db and field data.

The field app uses the dev device `laptop-dev-01` / `dev-token-laptop-dev-01`; the cloud is given the same pair via `DEVICE_TOKENS`.
