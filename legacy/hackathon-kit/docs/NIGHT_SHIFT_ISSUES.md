# Writing night-shift issues

How to write a GitHub issue that the night-shift agents will pick up, build and merge on their own, and how to turn a review finding into one.

The short version: **one clear, testable change per issue, labelled `night-safe` + `lane:<lane>` + `wave:<wave>`, with no `human` label and no assignee.**

---

## 1. How the night shift picks work

Every 20 minutes (and after every merge to `main`), each lane's picker chooses **one** issue:

1. It skips everything while `AGENTS_ENABLED` is not `true`, or while CI on `main` is red (then only the fix lane works).
2. It only considers **open** issues that:
   - have its lane label (`lane:field`, `lane:cloud` or `lane:engine`),
   - have **no** `human`, `blocked` or `integrator` label,
   - have **no assignee**,
   - have every issue in their `Blocked by:` line **closed**.
3. Among those, it takes the **lowest wave** first (`wave:W04` before `wave:W05`), then the **lowest issue number**.

Each lane builds one issue at a time; the three lanes run in parallel. An agent builds the issue on an `agent/...` branch and opens a PR. A separate review agent then checks it, and it auto-merges when the required checks (`test` and `verdict`) pass. The PR closes the issue.

If an attempt fails, the issue is retried (the retry runs on a stronger model). After **3 failed attempts** the issue gets the `blocked` label and the lane moves on.

---

## 2. Labels

| Label | Required? | Meaning |
|---|---|---|
| `night-safe` | **Yes** | An agent may build this without a person. |
| `lane:field` / `lane:cloud` / `lane:engine` | **Yes, exactly one** | Which lane builds it. Evals (`evals/`) belong to `lane:engine`. |
| `wave:W05` (or `wave:WNN`) | **Yes** | Ordering. Use the **current wave** (ask in chat, or check the open queue) unless it must jump the queue. A lower number runs first. |
| `model:opus` | Optional | Start on the stronger model. Use it only for hard, cross-cutting tickets. |
| `human` | **Never** on agent tickets | A person owns this. The picker skips it. |
| `blocked` | Never set by hand | The picker sets it after 3 failures. Remove it after you fix the ticket to retry (see section 8). |
| `integrator` | Never on agent tickets | Integration work a person owns. |

Do **not** assign an agent ticket to anyone: assigned issues are skipped.

---

## 3. Title

```
[<ID>] <what changes, in a few words>
```

- **ID**: a lane prefix plus the next free number: `F` for field, `C` for cloud, `E` for engine, `EV` for evals and `H` for human tickets. Check the open and closed issues for the highest number in use.
- Describe the outcome, not the activity: `[F21] Show outbox count and online/offline badge in field header`, not `[F21] Work on header`.

---

## 4. Body template

Copy this template. Every section matters to the agent.

```markdown
## Goal
One or two sentences: what should be true when this is done, and why it matters
(which PRD requirement, demo step or review finding it serves).

## Do
- Concrete changes, naming the real files (see the "Ticket path mapping" in CLAUDE.md).
- Decisions already made (formats, names, thresholds, which library). The agent will not
  ask; if something is left open, it will decide and list it as an assumption.
- What NOT to change, if relevant (e.g. "do not change contracts/").

## Acceptance
- Checks a machine can run: test names or behaviours to test, commands the agent runs
  in its own job and whose output it pastes in the PR.
- Edge cases that must be covered.

## Files
field/rescue_field/..., field tests, ...

Blocked by: #123, #124
```

Rules for the `Blocked by:` line:

- Write it as a line of its own, exactly `Blocked by: #n` or `Blocked by: #a, #b`.
- Only list issues this one truly depends on. Every listed issue must be **closed** before the agent starts.
- Leave the line out entirely if there are no dependencies.
- Use it to **chain tickets that touch the same file**, so two lanes don't edit it at the same time and conflict.

---

## 5. What makes a good agent ticket

**Do:**

- **One change, one PR.** Aim for 15–45 minutes of agent work. Split anything bigger into chained tickets.
- **Make acceptance machine-checkable.** Tests, or commands run in the agent's job. "Looks good" is not checkable.
- **State decisions instead of leaving them open.** Reference `docs/DECISIONS.md`, `docs/PRD.md` or `contracts/` when the answer is already there.
- **Use the real paths** from CLAUDE.md's "Ticket path mapping".
- **Use mocks for anything external.** Agents have no API keys and no accounts. Write a `--mock` mode and test that.
- **Quote the evidence** when the ticket comes from a review: file, line and what is wrong (see section 7).

**The agent's environment** (what acceptance can rely on):

- Python 3.12 with the repo's dev dependencies; Postgres 16 (`DATABASE_URL` is set); Elixir/Phoenix for `cloud/`.
- Docker and Docker Compose on the runner.
- Mercury (`mmc`, `$MERCURY_JSON_SRC`) is installed **in the engine lane only**.
- Network access for package installs. Do not depend on third-party APIs.

---

## 6. What must NOT be an agent ticket

Make these `human` tickets instead (label `human` plus the lane, no `night-safe`):

| Kind of work | Why |
|---|---|
| Changes to `contracts/` (schemas, `engine_rules.md`, `sync_api.md`, vectors) | Every lane depends on them. A person decides, then agents update code to match. |
| Product decisions (what the product should do, thresholds, wording for users) | The agent would guess. Decide first and write the answer into the ticket or `DECISIONS.md`. |
| Anything needing API keys, accounts, billing, deploys or secrets | Agents have none. |
| Anything needing the trained model | Use the mock-model path until the model is pinned (H1b). |
| Eval sets: the sealed holdout (set E), golden sets, hand labels | Kept separate from the people and agents who build the product, on purpose. Agents must never read the holdout. |
| Very large or vague work ("improve the UI") | Split it into specific tickets first. |

---

## 7. Turning a review finding into a ticket

When you review an agent PR, the code or the demo and find a problem:

1. **One finding = one ticket.** Don't bundle unrelated fixes.
2. **Title** says the fix: `[C21] Reject sync batches with duplicate request ids`.
3. **Goal** quotes the evidence: the PR number or file and line, what happens now and what should happen.
4. **Do** says where the fix goes and anything it must not change.
5. **Acceptance** starts with a test that **fails today** and passes after the fix, plus the existing tests.
6. If the right fix is a contract or product change, file it as `human` with the decision needed, not as `night-safe`.
7. If it must happen before other queued work, give it a lower wave (e.g. `wave:W04` while the queue is on W05).

Example:

```markdown
## Goal
PR #143 (F14) tests a 401 response, but the sync worker drops the outbox row after a 401
(field/rescue_field/sync.py, `except Unauthorized:` branch). Rows must be kept and retried
after the token is fixed.

## Do
- In field/rescue_field/sync.py, on 401 stop the sync loop, keep every outbox row, and log
  one clear error with the device id.
- Do not change contracts/sync_api.md.

## Acceptance
- New test: with the fake cloud returning 401, all outbox rows remain after a sync attempt.
- After the fake cloud switches to 200, the next sync drains them with no duplicates.
- All field tests pass.

## Files
field/rescue_field/sync.py, field tests
```

---

## 8. Creating the issue

**From the terminal** (from your repo folder, with `R=Gauntlet-Hackathon/gauntletvshackathon`):

```bash
gh issue create -R $R \
  --title "[F21] Show outbox count and online/offline badge in field header" \
  --label night-safe --label lane:field --label wave:W05 \
  --body-file - <<'EOF'
## Goal
...

## Do
- ...

## Acceptance
- ...

## Files
...
EOF
```

The quoted `'EOF'` stops your shell from expanding `$`, backticks or `!` inside the body.

**From the web**: choose the **Night-shift ticket** issue template. It pre-fills the sections and the `night-safe` and `wave:W05` labels. Add the `lane:` label yourself, and change the wave if needed.

**From a cloud Claude session** (where `gh issue create` may be unavailable), use the REST API:

```bash
gh api repos/Gauntlet-Hackathon/gauntletvshackathon/issues \
  -f title='[F21] ...' -f body="$(cat body.md)" \
  -f 'labels[]=night-safe' -f 'labels[]=lane:field' -f 'labels[]=wave:W05'
```

---

## 9. Checklist before posting

- [ ] Title is `[<ID>] <outcome>`, with the next free ID.
- [ ] Labels: `night-safe`, exactly one `lane:`, one `wave:`. No `human`, no assignee.
- [ ] Goal says why. Do names real files and the decisions already made.
- [ ] Acceptance is checkable by tests or commands, including edge cases.
- [ ] Nothing in section 6 (protected paths, contracts, keys, model, eval sets).
- [ ] `Blocked by:` lists only real dependencies, and chains tickets that touch the same file.
- [ ] Not a duplicate of an open issue. Search first.
- [ ] Small enough for one PR.

---

## 10. After posting

- It is picked up within about 20 minutes, if it is the lowest wave and number in its lane and its dependencies are closed.
- Check what each lane is doing:

  ```bash
  R=Gauntlet-Hackathon/gauntletvshackathon
  RUN=$(gh run list -R $R --workflow night-shift.yml --limit 1 --json databaseId --jq '.[0].databaseId')
  gh run view $RUN -R $R --log 2>/dev/null | grep -o '{"mode".*}' | sort -u
  ```

  `picked #N` means it's being built. `waiting on dependencies` means a `Blocked by:` issue is still open. `no ready issues` means check the labels.
- **If it gets `blocked`**: read the agent's comments on the issue. Fix the ticket body (usually unclear acceptance, a missing decision, or something from section 6), then remove the `blocked` label. The attempt count starts again from zero.
- **Code freeze**: before the demo the agents are switched off (`AGENTS_ENABLED=false`). After the freeze, don't add `night-safe` tickets. Fixes go through people.
