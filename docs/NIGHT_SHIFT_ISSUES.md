# Writing night-shift issues

How to write a GitHub issue that the night-shift agents will pick up, build and merge on their own, how to turn a review finding into one, and how to read what the night shift writes back.

The short version: **one clear, testable change per issue, labelled `night-safe` + one `lane:<lane>` + one `wave:<n>`, with no `human` label and no assignee. If other tickets depend on it, or it depends on others, it has an `## Interface` section.**

Lanes, label names and limits come from `.night-shift/config.yml`. The defaults are used below.

---

## 1. How the night shift picks work

After every merge to the main branch, when a PR's checks finish, and every 20 minutes as a backstop, each lane's picker chooses **one** issue:

1. It does nothing while the repo variable `AGENTS_ENABLED` is not `true`. While CI on the main branch is red, only the fix lane works, and it works on fixing main.
2. It only considers **open** issues that:
   - have exactly one lane label, and it is this lane's (`lane:<lane>`),
   - have the `night-safe` label,
   - have **none** of `human`, `blocked`, `needs-human`, `proposed` or `integrator`,
   - have **no assignee**,
   - have every issue in their `Blocked by:` line **closed**.
3. Among those, it takes the **lowest wave** first (`wave:4` before `wave:5`), then the **lowest issue number**.

Each lane builds one issue at a time; the lanes run in parallel. An agent builds the issue on an `agent/<lane>/issue-<n>` branch, and the night shift opens a PR. A separate review agent checks it. The picker keeps the PR up to date with main and merges it when the required checks (by default `test` and `review / verdict`) pass on the current main. The PR closes the issue.

If an attempt fails, the issue is retried on a stronger model. After **3 failed attempts** (`limits.max_attempts`) the issue gets the `blocked` label and the lane moves on. Runs that never really started (expired token, usage limit) are not counted.

---

## 2. Labels

| Label | Required? | Meaning |
|---|---|---|
| `night-safe` | **Yes** | An agent may build this without a person. |
| `lane:<lane>` | **Yes, exactly one** | Which lane builds it. Lanes and the folders they own are in `.night-shift/config.yml`. |
| `wave:<n>` | **Yes** | Ordering, 0 to 99 (`wave:W05` style also works). A lower number runs first. Use the current wave (check the open queue) unless it must jump the queue. |
| `model:opus` | Optional | Start on the stronger model. Use it only for hard, cross-cutting tickets. Any `model:<alias>` works. |
| `human` | **Never** on agent tickets | A person owns this. The picker skips it. |
| `proposed` | Never on a ready ticket | Suggested by an agent. A person reviews it first (see section 8). The picker skips it. |
| `blocked` | Never set by hand | The picker sets it after too many failed attempts. Fix the ticket and remove it to retry (see section 11). |
| `needs-human` | Set when a decision is needed | A product or design question is open. The picker skips it. |
| `waiting` | Never set by hand | The night shift parked this issue on another lane's ticket (see section 8). |
| `agent:in-progress` | Never set by hand | An agent is working on this now. |
| `integrator` | Never on agent tickets | Integration work a person owns. The picker skips it. |

Do **not** assign an agent ticket to anyone: assigned issues are skipped. A person claims a ticket by assigning themselves.

The night shift also uses `agent`, `fix-main`, `review-error` and `night-log` on PRs and on its log issue, and `frozen` during a code freeze. You do not set these.

---

## 3. Title

```
[<ID>] <what changes, in a few words>
```

- **ID**: the lane's prefix plus the next free number, e.g. `[A12]`. The prefix comes from `planner.id_prefixes` in the config. The default is the first letter of the lane name, uppercased (lane `api` -> `A`). Check open and closed issues for the highest number in use. Never reuse an ID.
- Keep the whole title under 100 characters.
- Describe the outcome, not the activity: `[A21] Return 404 for unknown user ids`, not `[A21] Work on users`.

---

## 4. Body template

Copy this template. Every section matters to the agent.

```markdown
## Goal
One or two sentences: what should be true when this is done, and why it matters
(which requirement, user flow or review finding it serves).

## Do
- Concrete changes, naming the real files.
- Decisions already made (formats, names, thresholds, which library). The agent will not
  ask; if something is left open, it will decide and list it as an assumption.
- What NOT to change, if relevant.

## Acceptance
- Checks a machine can run: test names or behaviours to test, commands the agent runs
  in its own job.
- Edge cases that must be covered.

## Files
src/users/routes.py, tests/test_users.py

## Interface
(Required when there is a "Blocked by" line or other tickets depend on this one. See section 5.)

Blocked by: #123, #124
```

Goal, Do, Acceptance and Files are always required.

Rules for the `Blocked by:` line:

- Write it as a line of its own, exactly `Blocked by: #n` or `Blocked by: #a, #b`.
- Only issue numbers. Anything else (a name, a ticket ID) counts as never met, and the issue waits forever.
- Only list issues this one truly depends on. Every listed issue must be **closed** before the agent starts.
- Leave the line out entirely if there are no dependencies.
- Use it to **chain tickets that touch the same file**, so two lanes don't edit it at the same time and conflict.
- Never make a cycle (A waits on B, B waits on A).

---

## 5. The `## Interface` section

Lanes build in parallel against what tickets promise, not against each other's code. So any ticket that has a `Blocked by:` line, or that another ticket lists in its `Blocked by:` line, must have an `## Interface` section. It says exactly what crosses the boundary:

- function signatures (name, arguments with types, return type, errors raised),
- endpoints with method, path, request shape and response shape, including error responses,
- environment variables (name, meaning, default),
- file paths that one side writes and the other reads,
- data formats (JSON fields, CSV columns, table columns).

The ticket that provides the interface and the ticket that consumes it state the **same** contract. The planner rejects tickets with dependencies and no Interface section.

Example:

```markdown
## Interface
- `GET /users/{id}` -> 200 `{"id": int, "name": str, "email": str}`; 404 `{"error": "not_found"}`
- `users.get_user(user_id: int) -> User | None` in `src/users/store.py`
- Env var `USERS_DB_URL` (required, no default)
```

When the provider's PR merges, the night shift posts a **hand-off comment** on every open issue that lists it in `Blocked by:`. The hand-off says what was actually built (files, interfaces, env vars, decisions) and any difference from the ticket's Interface section. The agent that builds the dependent issue reads it first.

---

## 6. What makes a good agent ticket

**Do:**

- **One change, one PR.** Aim for 15 to 45 minutes of agent work. Split anything bigger into chained tickets.
- **Make acceptance machine-checkable.** Tests, or commands run in the agent's job. "Looks good" is not checkable.
- **State decisions instead of leaving them open.** Point to the doc that already has the answer (a PRD, a decisions log, a contract file).
- **Use the real paths.** Name files that exist, or say where new ones go.
- **Use mocks for anything external.** Agents have no API keys and no accounts. Write a mock mode and test that.
- **Quote the evidence** when the ticket comes from a review: file, line and what is wrong (see section 9).

**Don't:**

- "Improve the error handling" (which errors? where? what should happen?).
- "Refactor the API and add tests" (two changes; split them).
- "Make it faster" (no number, no way to check).

**The agent's environment** (what acceptance can rely on): a fresh GitHub runner with the main branch checked out, plus the lane's `setup` commands or action and, if the lane sets `postgres_image`, a Postgres service with `DATABASE_URL` set. Check the lane in `.night-shift/config.yml`. There is network access for package installs. Do not depend on third-party APIs.

---

## 7. What must NOT be an agent ticket

Make these `human` tickets instead (label `human` plus the lane, no `night-safe`):

| Kind of work | Why |
|---|---|
| Changes to protected paths: `.github/`, `.claude/`, `CLAUDE.md`, `AGENTS.md`, `.mcp.json`, `.night-shift/` and anything in `protected_paths` | The night shift rejects agent changes there. Agents may never change their own rules or CI. |
| Changes to shared contracts (paths in `planner.human_paths`, `contracts/` by default) | Every lane depends on them. A person decides, then agents update code to match. |
| Product decisions (what the product should do, thresholds, wording for users) | The agent would guess. Decide first and write the answer into the ticket or a decisions doc. |
| Anything needing API keys, accounts, billing, deploys or secrets | Agents have none. |
| Test data or evaluation sets that must stay independent of the code | Agents should not write the checks that judge their own work. |
| Very large or vague work ("improve the UI") | Split it into specific tickets first. |

---

## 8. What the night shift writes on your issues

The night shift talks through ordinary issue comments. Each one carries a hidden marker. **Only markers posted by the night shift's own account (the PAT's login) count.** If anyone else, even a repo admin, pastes the same text, it is ignored. You cannot add or remove attempts, or block or unblock work, by writing a comment.

What you will see:

- **Attempt comments.** "Night shift lane `x` started an attempt: <run link>". Each counts toward the attempt limit, unless the night shift later edits it to say the attempt was not counted.
- **Outcome comments.** If an attempt ends without a PR, a comment says why (`stuck`, `done but no commits`, `rejected: touches protected paths`, ...).
- **PR close reasons.** When the picker closes or merges an agent PR, it records a reason: `verdict_passed`, `verdict_failed`, `checks_failed`, `conflict`, `stale` or `superseded`. Review findings are copied to the issue so the next attempt reads them. A conflict, or a review run that itself broke (`review-error`), does not count as an attempt.
- **Hand-off comments.** "Night shift hand-off: #12 merged in PR #40. This issue no longer waits on it." It lists what was built and any difference from the ticket's Interface (see section 5).
- **Waiting.** If an agent finds that something another lane owns does not exist yet, it stops and reports `waiting`. The night shift then:
  - files a ticket in that lane (up to 2 per issue, `limits.max_needs_per_issue`), or reuses an existing one with the same title. A new ticket is `proposed`: a person swaps that label for `night-safe` (unless `limits.auto_ready_needs` is on),
  - appends `#<new>` to this issue's `Blocked by:` line and adds the `waiting` label,
  - posts a comment saying which ticket it waits on.
  The attempt is not counted. The picker resumes the issue once the new ticket closes. If the request would create a dependency cycle, the issue gets `needs-human` instead.
- **Notes.** "Night shift note from #12 (lane `x`)". Information from another issue's agent, for example "I added a helper you can reuse". Up to 2 per sending issue. Notes never change what gets picked.
- **Proposed tickets.** Tickets an agent filed are labelled `proposed` when they need a person first: follow-up work an agent noticed but kept out of scope, and `waiting` requests that do not pass the ticket checks (the body then lists why). The picker skips them. To promote one, read it, fix the body if needed, then **add `night-safe` and remove `proposed`**. A `waiting` request that passes the checks is filed as `night-safe` straight away, or as `human` if it touches protected or contract paths.

Your own comments still matter. Comments by repo owners, members and collaborators are shown to the agent as discussion, but as data, not instructions. To change what the agent must do, **edit the ticket body**.

---

## 9. Turning a review finding into a ticket

When you review an agent PR, the code or the running product and find a problem:

1. **One finding = one ticket.** Don't bundle unrelated fixes.
2. **Title** says the fix: `[A21] Reject sign-ups with an empty email`.
3. **Goal** quotes the evidence: the PR number or file and line, what happens now and what should happen.
4. **Do** says where the fix goes and anything it must not change.
5. **Acceptance** starts with a test that **fails today** and passes after the fix, plus the existing tests.
6. If the right fix is a contract or product change, file it as `human` with the decision needed, not as `night-safe`.
7. If it must happen before other queued work, give it a lower wave (e.g. `wave:4` while the queue is on `wave:5`).

Example:

```markdown
## Goal
PR #143 ([A14]) tests a 401 response, but the sync job deletes the queued row after a 401
(src/sync/worker.py, the `except Unauthorized:` branch). Rows must be kept and retried
after the token is fixed.

## Do
- In src/sync/worker.py, on 401 stop the sync loop, keep every queued row, and log
  one clear error with the device id.
- Do not change the sync API contract.

## Acceptance
- New test: with the fake server returning 401, all queued rows remain after a sync attempt.
- After the fake server switches to 200, the next sync sends them with no duplicates.
- All existing tests pass.

## Files
src/sync/worker.py, tests/test_sync_worker.py
```

You can also ask the planner to do this for you (section 10).

---

## 10. The planner

The planner drafts tickets for you. A read-only agent writes the drafts; the night shift then checks every draft against this guide before it creates anything. It checks the title, the four required sections, exactly one known lane, a wave, an Interface section when there are dependencies, that every `Blocked by` target exists, no cycles and no duplicates of open issues. It creates at most `planner.max_tickets_per_run` tickets per run (8 by default), in dependency order. Valid tickets get `night-safe`. Tickets that touch protected or human-only paths become `human` tickets. Invalid drafts are not created; the run lists why.

Ways to run it:

- **From an issue.** Someone with write access comments:

  ```
  /night-shift plan <what you want, in plain words>
  ```

  For example, on a review issue: `/night-shift plan turn each finding above into a ticket`. Comments from people without write access are ignored.
- **From the terminal:**

  ```bash
  gh nightshift plan --from docs/PRD.md   # plan from a document
  gh nightshift plan --gap                # compare the docs with the code; ticket what is missing
  gh nightshift plan --gap --lane api     # the same, for one lane only
  ```

- **On a timer.** If `planner.refill_below` is set, the planner tops up any lane with fewer ready tickets than that.

Read what it created before you sleep. The planner never decides product questions; open questions go into its notes.

---

## 11. Creating the issue

**From the terminal** (from your repo folder):

```bash
gh api repos/OWNER/REPO/issues \
  -f title='[A21] Return 404 for unknown user ids' \
  -f body="$(cat body.md)" \
  -f 'labels[]=night-safe' -f 'labels[]=lane:api' -f 'labels[]=wave:5'
```

Write the body in `body.md` first. This uses the REST API, so it works everywhere `gh` is logged in, including cloud sessions.

**From the web**: choose the **Night-shift ticket** issue template. It pre-fills the sections and files the ticket as `proposed`. Add one `lane:` label and one `wave:` label, then swap `proposed` for `night-safe` when it is ready for an agent (the template never applies `night-safe` itself, because GitHub applies a template's labels for anyone who opens an issue).

---

## 12. Checklist before posting

- [ ] Title is `[<ID>] <outcome>`, with the lane's prefix and the next free number, under 100 characters.
- [ ] Labels: `night-safe`, exactly one `lane:`, one `wave:`. No `human`, no `proposed`, no assignee.
- [ ] Goal says why. Do names real files and the decisions already made.
- [ ] Acceptance is checkable by tests or commands, including edge cases.
- [ ] Files lists the files it touches.
- [ ] `## Interface` is there if it has a `Blocked by:` line or other tickets depend on it.
- [ ] Nothing from section 7 (protected paths, contracts, product decisions, keys, deploys).
- [ ] `Blocked by:` lists only real dependencies as `#n`, and chains tickets that touch the same file.
- [ ] Not a duplicate of an open issue. Search first.
- [ ] Small enough for one PR.

---

## 13. After posting

- It is picked up within about 20 minutes, if it is the lowest wave and number in its lane and its dependencies are closed.
- Check what each lane is doing:

  ```bash
  gh nightshift status
  ```

  `picked #N` means it is being built. `waiting on dependencies` means a `Blocked by:` issue is still open. `no ready issues` means check the labels. `still in review` means the lane's PR is waiting on checks.
- The pinned night-log issue (label `night-log`) gets one line per run: time, lane, issue, model, outcome and cost.
- **If it gets `blocked`**: read the night shift's comments and the review findings on the issue. Fix the ticket body (usually unclear acceptance, a missing decision, a missing Interface, or something from section 7), then remove the `blocked` label. The attempt count starts again from zero.
- **If it gets `needs-human`**: answer the question in the ticket body, then remove the label.
- **Stopping the night shift**: `gh nightshift pause` stops every lane (sets `AGENTS_ENABLED` to `false`); `gh nightshift start` turns it back on. For a code freeze, `gh nightshift freeze` stops the lanes and hands every remaining `night-safe` ticket to people (`human` + `frozen`); `gh nightshift resume` undoes it.
