#!/usr/bin/env bash
# End-to-end rehearsal of gh-nightshift against a NEW private sandbox repo.
#
#   scripts/rehearse.sh OWNER/NAME              # dry run (the default): print every command, run nothing
#   scripts/rehearse.sh --live OWNER/NAME       # really do it (creates the repo, spends Actions minutes and
#                                               # Claude usage)
#
# Dry run is the default on purpose: a live run creates a repository, stores secrets and starts paid agent
# runs. A dry run touches neither the network nor the disk; it prints the commands, the files it would
# write and the checks it would make. See docs/REHEARSAL.md for what each step proves and the expected output.
#
# Options
#   --live              run for real (without it, or with --dry-run, nothing is executed)
#   --dry-run           print the commands only (default)
#   --tool-ref REF      gh-nightshift ref the sandbox's caller workflows use (default: main). Must be pushed to
#                       the tool repo, which must be public (or reachable by the sandbox's Actions).
#   --workdir DIR       where to clone the sandbox (default: a new temp dir)
#   --timeout MIN       how long to wait for each live milestone (default: 60)
#   --from STEP         start at step a|b|c|d|e|f (resume a live run; needs --workdir with the clone)
# Environment
#   NS                  how to call the CLI (default: "gh nightshift"; e.g. NS="$PWD/gh-nightshift")
#   NS_AGENT_PAT        optional: the AGENT_PAT value, so `doctor --pat-env NS_AGENT_PAT` can check it
#
# Only REST is used for checks (`gh api`, `gh run download`, `gh workflow run`). Nothing is ever deleted;
# cleanup instructions are printed at the end.
set -euo pipefail

LIVE=0
TOOL_REF="main"
WORKDIR=""
TIMEOUT_MIN=60
FROM="a"
SANDBOX=""
NS="${NS:-gh nightshift}"
POLL=30
FAILS=0
PASSES=0

usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --live) LIVE=1 ;;
    --dry-run) LIVE=0 ;;
    --tool-ref) TOOL_REF="${2:?--tool-ref needs a value}"; shift ;;
    --workdir) WORKDIR="${2:?--workdir needs a value}"; shift ;;
    --timeout) TIMEOUT_MIN="${2:?--timeout needs minutes}"; shift ;;
    --from) FROM="${2:?--from needs a step}"; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *) SANDBOX="$1" ;;
  esac
  shift
done

if [ -z "$SANDBOX" ] || ! [[ "$SANDBOX" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "usage: scripts/rehearse.sh [--live] [--tool-ref REF] OWNER/NAME" >&2
  exit 2
fi
case "$FROM" in a|b|c|d|e|f) ;; *) echo "--from must be one of a b c d e f" >&2; exit 2 ;; esac
if ! [[ "$TIMEOUT_MIN" =~ ^[0-9]+$ ]]; then echo "--timeout must be whole minutes" >&2; exit 2; fi

NAME="${SANDBOX#*/}"
if [ -z "$WORKDIR" ]; then
  if [ "$LIVE" = 1 ]; then WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/ns-rehearsal.XXXXXX")"; else WORKDIR="/tmp/ns-rehearsal.XXXXXX"; fi
fi
CLONE="$WORKDIR/$NAME"
read -r -a NS_CMD <<< "$NS"

# --- helpers ----------------------------------------------------------------------------

say() { printf '%s\n' "$*"; }
step() { printf '\n==== %s ====\n' "$*"; }

quote() {  # the arguments, shell-quoted, on one line
  local out="" arg
  for arg in "$@"; do out+="$(printf '%q' "$arg") "; done
  printf '%s' "${out% }"
}

show() { printf '+ %s\n' "$(quote "$@")"; }  # print a command the way a shell would read it

run() {  # run (live) or print (dry run) one command
  show "$@"
  if [ "$LIVE" = 1 ]; then "$@"; fi
}

run_in_clone() {  # like run, but inside the sandbox clone
  printf '+ (cd %s && %s)\n' "$(quote "$CLONE")" "$(quote "$@")"
  if [ "$LIVE" = 1 ]; then (cd "$CLONE" && "$@"); fi
}

ns() { run_in_clone "${NS_CMD[@]}" "$@"; }

write_file() {  # write_file PATH (content on stdin), relative to the clone
  local path="$1" content
  content="$(cat)"
  if [ "$LIVE" = 1 ]; then
    mkdir -p "$(dirname "$CLONE/$path")"
    printf '%s\n' "$content" > "$CLONE/$path"
    say "wrote $path"
  else
    say "[dry-run] would write $path ($(printf '%s\n' "$content" | wc -l | tr -d ' ') lines)"
  fi
}

pass() { PASSES=$((PASSES + 1)); printf 'PASS  %s\n' "$1"; shift; local l; for l in "$@"; do printf '      %s\n' "$l"; done; }
fail() { FAILS=$((FAILS + 1)); printf 'FAIL  %s\n' "$1"; shift; local l; for l in "$@"; do printf '      %s\n' "$l"; done; }

api() { gh api -H "Accept: application/vnd.github+json" "$@"; }

wait_until() {  # wait_until DESCRIPTION KICK_EVERY_POLLS KICK_CMD|- CHECK_FN [ARGS...]: poll quietly
  local desc="$1" kick_every="$2" kick="$3"; shift 3
  if [ "$LIVE" = 0 ]; then
    say "[dry-run] would poll every ${POLL}s for up to ${TIMEOUT_MIN} min until: $desc"
    return 0
  fi
  local deadline=$(( $(date +%s) + TIMEOUT_MIN * 60 )) polls=0
  say "waiting (up to ${TIMEOUT_MIN} min) until: $desc"
  until ( "$@" ) >/dev/null 2>&1; do  # a subshell, so polling never counts as PASS/FAIL
    if [ "$(date +%s)" -ge "$deadline" ]; then say "timed out waiting until: $desc"; return 1; fi
    polls=$((polls + 1))
    if [ "$kick" != "-" ] && [ $((polls % kick_every)) -eq 0 ]; then
      say "  still waiting ($((polls * POLL / 60)) min); dispatching again: $kick"
      $kick >/dev/null 2>&1 || true
    fi
    sleep "$POLL"
  done
  say "  done after about $((polls * POLL / 60)) min"
}

dispatch_lane() {  # gh workflow run night-shift.yml for one lane (REST workflow_dispatch)
  gh workflow run night-shift.yml -R "$SANDBOX" -f lane="$1"
}
kick_api() { dispatch_lane api; }
kick_web() { dispatch_lane web; }

issue_by_title() {  # issue_by_title LANE TEXT PLACEHOLDER: open issue in LANE whose title contains TEXT
  local path="repos/$SANDBOX/issues?state=open&labels=lane:$1&per_page=100"
  local jq="[.[] | select(.pull_request == null) | select(.title | contains(\"$2\")) | .number][0] // empty"
  if [ "$LIVE" = 0 ]; then show gh api "$path" --jq "$jq" >&2; printf '%s' "$3"; return 0; fi
  api "$path" --jq "$jq"
}

# --- the checks: each prints PASS/FAIL with evidence --------------------------------------

check_pr_merged() {  # A's PR (branch agent/api/issue-A) merged and A closed
  local a="$1" pr state
  if [ "$LIVE" = 0 ]; then
    show gh api "repos/$SANDBOX/pulls?state=closed&per_page=50" --jq ".[] | select(.head.ref == \"agent/api/issue-$a\" and .merged_at != null)"
    show gh api "repos/$SANDBOX/issues/$a" --jq .state
    return 0
  fi
  pr="$(api "repos/$SANDBOX/pulls?state=closed&per_page=50" \
    --jq "[.[] | select(.head.ref == \"agent/api/issue-$a\" and .merged_at != null) | \"#\(.number) merged_at=\(.merged_at) \(.html_url)\"][0] // empty")"
  state="$(api "repos/$SANDBOX/issues/$a" --jq .state)"
  if [ -n "$pr" ] && [ "$state" = "closed" ]; then
    pass "A's PR merged (issue #$a)" "PR $pr" "issue #$a state=$state"
  else
    fail "A's PR merged (issue #$a)" "merged PR on agent/api/issue-$a: ${pr:-none}" "issue #$a state=$state"
    return 1
  fi
}

check_handoff() {  # trusted hand-off marker from A on B's issue
  local a="$1" b="$2" found
  local jq=".[] | select(.body | contains(\"<!-- ns:{\") and contains(\"\\\"type\\\":\\\"handoff\\\"\") and contains(\"\\\"from\\\":$a,\")) | \"comment \(.id) by \(.user.login): \(.body | split(\"\\n\")[0])\""
  if [ "$LIVE" = 0 ]; then show gh api "repos/$SANDBOX/issues/$b/comments?per_page=100" --jq "$jq"; return 0; fi
  found="$(api "repos/$SANDBOX/issues/$b/comments?per_page=100" --jq "$jq" | head -n 1)"
  if [ -n "$found" ]; then
    pass "hand-off from #$a posted on #$b" "$found"
  else
    fail "hand-off from #$a posted on #$b" "no comment on #$b carries <!-- ns:{...\"type\":\"handoff\"} --> with \"from\":$a"
    return 1
  fi
}

find_task_run() {  # newest night-shift run whose task-<lane> artifact is the task for ISSUE; prints "RUN DIR"
  local lane="$1" issue="$2" run dir
  for run in $(api "repos/$SANDBOX/actions/workflows/night-shift.yml/runs?per_page=30" --jq '.workflow_runs[].id'); do
    if [ -z "$(api "repos/$SANDBOX/actions/runs/$run/artifacts" --jq ".artifacts[] | select(.name == \"task-$lane\") | .id")" ]; then
      continue
    fi
    dir="$WORKDIR/task-$lane-$run"
    [ -f "$dir/task.md" ] || gh run download "$run" -R "$SANDBOX" -n "task-$lane" -D "$dir" >/dev/null 2>&1 || continue
    if grep -q "^# Issue #$issue:" "$dir/task.md" 2>/dev/null; then printf '%s %s\n' "$run" "$dir"; return 0; fi
  done
  return 1
}

check_task_has_handoff() {  # B was picked and its task.md carries the hand-off in Coordination
  local a="$1" b="$2" found run dir
  if [ "$LIVE" = 0 ]; then
    show gh api "repos/$SANDBOX/actions/workflows/night-shift.yml/runs?per_page=30" --jq '.workflow_runs[].id'
    show gh api "repos/$SANDBOX/actions/runs/RUN_ID/artifacts" --jq '.artifacts[] | select(.name == "task-web") | .id'
    show gh run download RUN_ID -R "$SANDBOX" -n task-web -D "$WORKDIR/task-web-RUN_ID"
    show grep -n -e "^# Issue #$b:" -e '^## Coordination' -e '"type":"handoff"' "$WORKDIR/task-web-RUN_ID/task.md"
    return 0
  fi
  if ! found="$(find_task_run web "$b")"; then
    fail "#$b picked with the hand-off in its task file" "no task-web artifact for issue #$b in the last 30 night-shift runs"
    return 1
  fi
  run="${found%% *}"; dir="${found#* }"
  if grep -q '^## Coordination (posted by the night shift; trusted)' "$dir/task.md" \
     && grep -q "\"from\":$a,.*\"type\":\"handoff\"" "$dir/task.md"; then
    local ev=("run https://github.com/$SANDBOX/actions/runs/$run" "$dir/task.md:") line
    while IFS= read -r line; do ev+=("  $line"); done < <(grep -n -e '^## Coordination' -e '"type":"handoff"' "$dir/task.md" | head -n 3)
    pass "#$b picked with the hand-off in its task file" "${ev[@]}"
  else
    fail "#$b picked with the hand-off in its task file" "$dir/task.md has no Coordination section with the hand-off from #$a" \
      "first line: $(head -n 1 "$dir/task.md")"
    return 1
  fi
}

need_target() {  # issue number named by the newest trusted need marker on ISSUE
  api "repos/$SANDBOX/issues/$1/comments?per_page=100" \
    --jq '[.[] | .body | select(contains("\"type\":\"need\"")) | capture("\"to\":(?<to>[0-9]+)").to][-1] // empty'
}

check_need() {  # a forced need: new api ticket, "Blocked by: #new" appended, waiting label
  local c="$1" new lane_labels body labels ok=1 ev=()
  if [ "$LIVE" = 0 ]; then
    show gh api "repos/$SANDBOX/issues/$c/comments?per_page=100" --jq '[.[] | .body | select(contains("\"type\":\"need\"")) | capture("\"to\":(?<to>[0-9]+)").to][-1]'
    show gh api "repos/$SANDBOX/issues/NEW" --jq '[.labels[].name] | join(",")'
    show gh api "repos/$SANDBOX/issues/$c" --jq .body
    show gh api "repos/$SANDBOX/issues/$c" --jq '[.labels[].name] | join(",")'
    return 0
  fi
  new="$(need_target "$c")"
  if [ -z "$new" ]; then fail "forced need on #$c" "no comment on #$c carries a need marker"; return 1; fi
  lane_labels="$(api "repos/$SANDBOX/issues/$new" --jq '"\(.title) [\([.labels[].name] | join(","))]"')"
  body="$(api "repos/$SANDBOX/issues/$c" --jq .body)"
  labels="$(api "repos/$SANDBOX/issues/$c" --jq '[.labels[].name] | join(",")')"
  ev+=("need marker on #$c points to #$new: $lane_labels")
  [[ "$lane_labels" == *"lane:api"* ]] || { ok=0; ev+=("#$new is not in lane api"); }
  if printf '%s\n' "$body" | grep -Eiq "blocked by:.*#$new([^0-9]|\$)"; then
    ev+=("#$c body: $(printf '%s\n' "$body" | grep -Ei 'blocked by:' | head -n 1)")
  else
    ok=0; ev+=("#$c body has no 'Blocked by: ... #$new' line")
  fi
  if [[ ",$labels," == *",waiting,"* ]]; then ev+=("#$c labels: $labels"); else ok=0; ev+=("#$c labels lack waiting: $labels"); fi
  if [ "$ok" = 1 ]; then pass "forced need on #$c filed #$new in lane api" "${ev[@]}"; else fail "forced need on #$c" "${ev[@]}"; return 1; fi
}

check_paused() {
  local v
  if [ "$LIVE" = 0 ]; then show gh api "repos/$SANDBOX/actions/variables/AGENTS_ENABLED" --jq .value; return 0; fi
  v="$(api "repos/$SANDBOX/actions/variables/AGENTS_ENABLED" --jq .value)"
  if [ "$v" = "false" ]; then pass "night shift paused" "AGENTS_ENABLED=$v"; else fail "night shift paused" "AGENTS_ENABLED=$v"; return 1; fi
}

# --- step a: the sandbox repo -------------------------------------------------------------

step_a() {
  step "a. create the private sandbox $SANDBOX and push a tiny Python project"
  run gh repo create "$SANDBOX" --private --description "gh-nightshift rehearsal sandbox (safe to delete)"
  run mkdir -p "$WORKDIR"
  run gh repo clone "$SANDBOX" "$CLONE"

  write_file README.md <<'EOF'
# ns-sandbox

Rehearsal sandbox for gh-nightshift: a tiny Python project with two lanes, `api/` and `web/`.
EOF
  write_file api/__init__.py <<'EOF'
"""The api lane's package."""
EOF
  write_file api/version.py <<'EOF'
VERSION = "0.1.0"


def version() -> str:
    return VERSION
EOF
  write_file api/requirements.txt <<'EOF'
pytest
EOF
  write_file api/tests/test_version.py <<'EOF'
from api.version import version


def test_version():
    assert version() == "0.1.0"
EOF
  write_file api/conftest.py <<'EOF'
import sys
from pathlib import Path

# `cd api && pytest` imports the package as `api`, from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
EOF
  write_file web/__init__.py <<'EOF'
"""The web lane's package."""
EOF
  write_file web/app.py <<'EOF'
def title() -> str:
    return "ns-sandbox"
EOF
  write_file web/requirements.txt <<'EOF'
pytest
EOF
  write_file web/tests/test_app.py <<'EOF'
from web.app import title


def test_title():
    assert title() == "ns-sandbox"
EOF
  write_file web/conftest.py <<'EOF'
import sys
from pathlib import Path

# `cd web && pytest` imports `web` and `api` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
EOF
  write_file .github/workflows/ci.yml <<'EOF'
name: CI

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install pytest
      - run: cd api && pytest
      - run: cd web && pytest
EOF
  run_in_clone git add -A
  run_in_clone git commit -m "Sandbox: tiny Python project with api/ and web/ and CI"
  run_in_clone git push -u origin HEAD:main
}

# --- step b: gh nightshift init -----------------------------------------------------------

step_b() {
  step "b. gh nightshift init --lanes api,web --yes"
  say "init writes the files and sets labels, settings, variables, the night-log issue and the secrets; after the"
  say "push, \`setup\` adds branch protection. You will be prompted (hidden input) for each missing secret:"
  say "  AGENT_PAT            fine-grained PAT: Contents, Issues, Pull requests read/write; Actions read; NO Workflows"
  say "  CLAUDE_TOKEN_API     \`claude setup-token\` of the api lane owner"
  say "  CLAUDE_TOKEN_WEB     \`claude setup-token\` of the web lane owner"
  say "  CLAUDE_TOKEN_REVIEW  \`claude setup-token\` for the review gate"
  say "(planner runs reuse CLAUDE_TOKEN_API; names come from .github/workflows/night-shift*.yml)"
  ns init --lanes api,web --yes --tool-ref "$TOOL_REF"
  run_in_clone git add -A
  run_in_clone git commit -m "Set up gh-nightshift (lanes api, web)"
  run_in_clone git push origin HEAD:main
  ns setup   # branch protection, now that the files are on main
}

# --- step c: plan 3 tickets from a plan file ----------------------------------------------

plan_json() {
  cat <<'EOF'
{
  "tickets": [
    {
      "key": "api-greet",
      "title": "Add greet() to the api package",
      "lane": "api",
      "wave": 0,
      "goal": "The api package can greet a user by name, so the web page can show a greeting.",
      "do": ["Add api/greeting.py with greet(name: str) -> str returning \"Hello, <name>!\"",
             "Add tests in api/tests/test_greeting.py"],
      "acceptance": ["greet(\"Ada\") == \"Hello, Ada!\"", "`cd api && pytest` passes"],
      "files": ["api/greeting.py", "api/tests/test_greeting.py"],
      "interface": "api/greeting.py: greet(name: str) -> str, returns \"Hello, <name>!\"",
      "requirements": ["R1 greeting"]
    },
    {
      "key": "web-page",
      "title": "Render a greeting page from api greet()",
      "lane": "web",
      "wave": 1,
      "goal": "The web package renders an HTML greeting built from the api lane's greet().",
      "do": ["Add web/page.py with render_page(name: str) -> str returning \"<h1>\" + greet(name) + \"</h1>\"",
             "Import greet from api.greeting; do not change files under api/",
             "Add tests in web/tests/test_page.py"],
      "acceptance": ["render_page(\"Ada\") == \"<h1>Hello, Ada!</h1>\"", "`cd web && pytest` passes"],
      "files": ["web/page.py", "web/tests/test_page.py"],
      "interface": "web/page.py: render_page(name: str) -> str, uses api.greeting.greet",
      "blocked_by": ["api-greet"],
      "requirements": ["R2 greeting page"]
    },
    {
      "key": "web-balance",
      "title": "Show the account balance on the web page",
      "lane": "web",
      "wave": 2,
      "goal": "The web package renders a user's account balance from the api lane's accounts module.",
      "do": ["Add web/balance.py with render_balance(user_id: int) -> str returning \"<p>Balance: <n></p>\"",
             "Get the number from api.accounts.get_balance(user_id: int) -> int. api/ is owned by lane api: do not create or edit anything under api/. If api/accounts.py does not exist, report status waiting with one need for lane api (rule 8) giving that exact interface.",
             "Add tests in web/tests/test_balance.py"],
      "acceptance": ["render_balance(1) uses api.accounts.get_balance(1)", "`cd web && pytest` passes", "no file under api/ is changed"],
      "files": ["web/balance.py", "web/tests/test_balance.py"],
      "requirements": ["R3 balance"]
    }
  ],
  "coverage": [
    {"requirement": "R1 greeting", "status": "ticketed", "tickets": ["api-greet"]},
    {"requirement": "R2 greeting page", "status": "ticketed", "tickets": ["web-page"]},
    {"requirement": "R3 balance", "status": "ticketed", "tickets": ["web-balance"]}
  ],
  "notes": "Rehearsal plan: web-page waits on api-greet; web-balance forces a need in lane api."
}
EOF
}

A=""; B=""; C=""

lookup_issues() {
  A="$(issue_by_title api 'Add greet() to the api package' '<A>')"
  B="$(issue_by_title web 'Render a greeting page from api greet()' '<B>')"
  C="$(issue_by_title web 'Show the account balance on the web page' '<C>')"
  if [ "$LIVE" = 1 ] && { [ -z "$A" ] || [ -z "$B" ] || [ -z "$C" ]; }; then
    say "could not find the three planned issues (A=$A B=$B C=$C); did step c run?"; exit 1
  fi
  say "issues: A=#$A (api)  B=#$B (web, Blocked by A)  C=#$C (web, forces a need)"
}

step_c() {
  step "c. plan 3 tickets (B in lane web depends on A in lane api) from a plan file"
  if [ "$LIVE" = 1 ]; then
    plan_json > "$WORKDIR/plan.json"
    say "wrote $WORKDIR/plan.json"
  else
    say "[dry-run] would write $WORKDIR/plan.json:"
    plan_json | sed 's/^/    /'
  fi
  ns plan --plan-file "$WORKDIR/plan.json"
  lookup_issues
  say "Hold C back until step e: take the ready label off it so the web lane cannot pick it yet."
  run gh api -X DELETE "repos/$SANDBOX/issues/$C/labels/night-safe"
}

# --- step d: doctor, start, one lane run, hand-off ----------------------------------------

step_d() {
  step "d. doctor, start, run lane api; A merges, B gets the hand-off and is picked"
  if [ -n "${NS_AGENT_PAT:-}" ]; then ns doctor --pat-env NS_AGENT_PAT || true; else ns doctor || true; fi
  ns start
  [ -n "$A" ] || lookup_issues
  run gh workflow run night-shift.yml -R "$SANDBOX" -f lane=api
  say "The api lane picks #$A, the agent builds it, publish opens the PR, CI and the review run, and the next"
  say "night-shift run (started by workflow_run) merges it and posts the hand-off on #$B."
  wait_until "A's PR is merged" 20 kick_api check_pr_merged "$A" || true
  check_pr_merged "$A" || true
  wait_until "the hand-off from #$A is on #$B" 6 kick_api check_handoff "$A" "$B" || true
  check_handoff "$A" "$B" || true
  say "The merge's push to main starts every lane; the web lane picks #$B (its blocker is closed now)."
  wait_until "#$B is picked with the hand-off in its task file" 10 kick_web check_task_has_handoff "$A" "$B" || true
  check_task_has_handoff "$A" "$B" || true
}

# --- step e: a forced need ----------------------------------------------------------------

step_e() {
  step "e. force a need: C (web) requires api.accounts.get_balance, which does not exist"
  [ -n "$C" ] || lookup_issues
  say "C's Do section says api/ belongs to lane api and asks for status waiting with a need if"
  say "api/accounts.py is missing. An owner comment repeats it (the agent sees it as Discussion, i.e. data)."
  run gh api "repos/$SANDBOX/issues/$C/comments" -f body="Rehearsal note: api/accounts.py does not exist and belongs to lane api. Do not create it from the web lane; report status waiting with a need for lane api with the interface api/accounts.py: get_balance(user_id: int) -> int."
  run gh api "repos/$SANDBOX/issues/$C/labels" -f "labels[]=night-safe"
  say "The web lane picks C once B's PR has merged (one open agent PR per lane)."
  run gh workflow run night-shift.yml -R "$SANDBOX" -f lane=web
  wait_until "#$C has a need marker" 10 kick_web need_marker_present "$C" || true
  check_need "$C" || true
}

need_marker_present() { [ -n "$(need_target "$1")" ]; }

# --- step f: pause and clean-up instructions ----------------------------------------------

step_f() {
  step "f. pause the night shift"
  ns pause
  check_paused || true
  cat <<EOF

Cleanup (nothing was deleted automatically):
  gh nightshift pause --repo $SANDBOX               # already done; stops new lane runs
  gh run list -R $SANDBOX --status in_progress      # cancel any still running: gh run cancel <id> -R $SANDBOX
  gh repo delete $SANDBOX --yes                     # needs the delete_repo scope: gh auth refresh -s delete_repo
  rm -rf $WORKDIR
  Revoke the AGENT_PAT if it was made only for this sandbox (Settings > Developer settings > Fine-grained tokens).
EOF
}

# --- main ---------------------------------------------------------------------------------

if [ "$LIVE" = 1 ]; then
  say "LIVE rehearsal against $SANDBOX (workdir $WORKDIR, tool ref $TOOL_REF)."
  "${NS_CMD[@]}" --version >/dev/null 2>&1 || {
    say "cannot run '$NS'. Install the extension from this checkout (gh extension install .) or set NS=./gh-nightshift"
    exit 1
  }
  if [ "$FROM" = "a" ] && gh api "repos/$SANDBOX" >/dev/null 2>&1; then
    say "$SANDBOX already exists; the rehearsal needs a NEW repo (or resume with --from and --workdir)."; exit 1
  fi
else
  say "DRY RUN: printing the commands for a rehearsal against $SANDBOX; nothing is executed."
  say "Run again with --live to do it for real."
fi

started=0
for s in a b c d e f; do
  [ "$s" = "$FROM" ] && started=1
  [ "$started" = 1 ] || continue
  "step_$s"
done

step "result"
if [ "$LIVE" = 1 ]; then
  say "$PASSES check(s) passed, $FAILS failed."
  [ "$FAILS" -eq 0 ]
else
  say "dry run complete: no command was executed."
fi
