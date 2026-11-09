#!/usr/bin/env bash
# One-time repo setup for the night shift. Run from anywhere with an authenticated gh (repo admin).
#   .github/night-shift/setup_repo.sh <owner/repo> <lane> [<lane> ...]
# Example: .github/night-shift/setup_repo.sh team/hackapp api web data
# Safe to re-run: existing labels are updated, not duplicated.
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <owner/repo> <lane> [<lane> ...]" >&2
  exit 1
fi
REPO="$1"; shift
LANES=("$@")
MAIN="$(gh repo view "$REPO" --json defaultBranchRef --jq .defaultBranchRef.name)"

label() { gh label create "$1" -R "$REPO" --color "$2" --description "$3" --force >/dev/null && echo "label: $1"; }

echo "== Labels"
for lane in "${LANES[@]}"; do label "lane:$lane" 1d76db "Work owned by the $lane lane"; done
for w in 0 1 2 3 4 5 6 7 8 9; do label "wave:$w" c5def5 "Build wave $w"; done
label night-safe 0e8a16 "Clear enough for an agent to do unattended"
label blocked b60205 "Stuck: needs a human"
label needs-human d93f0b "Needs a product or design decision"
label agent 5319e7 "Opened by a night-shift agent"
label agent:in-progress fbca04 "An agent is working on this now"
label fix-main b60205 "Fixes failing CI on main"
label model:opus 5319e7 "Use the strong model for this ticket"
label model:haiku c2e0c6 "Use the fast, cheap model for this ticket"
label review-error e99695 "The review gate itself failed; attempt not counted"
label night-log 000000 "Running log of night-shift activity"

echo "== Repo settings"
gh repo edit "$REPO" --enable-auto-merge --delete-branch-on-merge --enable-squash-merge >/dev/null && echo "auto-merge on"
gh variable set AGENTS_ENABLED -R "$REPO" --body false && echo "AGENTS_ENABLED=false (flip to true when ready)"
gh variable set FIX_LANE -R "$REPO" --body "${LANES[0]}" && echo "FIX_LANE=${LANES[0]}"

echo "== Night log issue"
if [ -z "$(gh issue list -R "$REPO" --label night-log --state open --json number --jq '.[0].number // empty')" ]; then
  gh issue create -R "$REPO" --title "Night-shift log" --label night-log \
    --body "Each agent run adds one line here: time, lane, issue, outcome, cost. Pin this issue."
fi

echo "== Branch protection on $MAIN (requires checks: test, verdict)"
# strict=false: PRs need not be rebased on the latest main (a lane PR would otherwise stall when main moves).
# Semantic breakage still shows up as red CI on main, which pauses every lane except FIX_LANE.
# enforce_admins=true: even an admin's token (AGENT_PAT is one) cannot push to main or merge red PRs.
# Humans go through PRs too; temporarily switch it off in Settings > Branches if you must hotfix.
if gh api -X PUT "repos/$REPO/branches/$MAIN/protection" --input - >/dev/null <<JSON
{
  "required_status_checks": { "strict": false, "contexts": ["test", "verdict"] },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
then echo "protected $MAIN"
else echo "WARNING: branch protection failed (private repos on free plans can't use it). Make the repo public or skip; auto-merge needs it."
fi

cat <<EOF

== Still to do by hand (Settings > Secrets and variables > Actions):
  secret AGENT_PAT            fine-grained PAT, only $REPO: Contents, Issues, Pull requests = Read and write;
                              Actions = Read. Do NOT grant Workflows.
EOF
echo "  secret CLAUDE_TOKEN_<PERSON>  one per person: each runs 'claude setup-token' and sets their own secret,"
echo "                              e.g. claude setup-token, then: gh secret set CLAUDE_TOKEN_PAAWAN -R $REPO"
echo "                              (setting secrets needs repo admin; grant teammates admin for the hackathon)"
echo "  secret CLAUDE_TOKEN_REVIEW  'claude setup-token' from someone else, used by the review gate"
echo "  then edit the matrix in .github/workflows/night-shift.yml: each lane's token_secret = its owner's CLAUDE_TOKEN_<PERSON>"
echo "  and set AGENTS_ENABLED=true once main is green"
