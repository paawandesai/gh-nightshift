#!/usr/bin/env bash
# Map Idea Factory epics to lanes after the github-issues stage has published the tickets.
#   .github/night-shift/assign_lanes.sh <owner/repo> <EPIC>=<lane> [<EPIC>=<lane> ...]
# Example: .github/night-shift/assign_lanes.sh team/hackapp E01=api E02=web E03=data E04=api
# Adds lane:<lane> to every open issue labeled epic:<EPIC>. Safe to re-run.
# Then lane owners add `night-safe` to the tickets an agent can do unattended:
#   gh issue edit 12 13 14 -R <owner/repo> --add-label night-safe
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <owner/repo> <EPIC>=<lane> [<EPIC>=<lane> ...]" >&2
  exit 1
fi
REPO="$1"; shift

for pair in "$@"; do
  epic="${pair%%=*}"; lane="${pair#*=}"
  if [ -z "$epic" ] || [ -z "$lane" ] || [ "$epic" = "$pair" ]; then
    echo "bad mapping '$pair' (expected EPIC=lane)" >&2; exit 1
  fi
  gh label create "lane:$lane" -R "$REPO" --color 1d76db --description "Work owned by the $lane lane" --force >/dev/null
  mapfile -t nums < <(gh issue list -R "$REPO" --label "epic:$epic" --state open --limit 500 --json number --jq '.[].number')
  for n in "${nums[@]}"; do
    gh issue edit "$n" -R "$REPO" --add-label "lane:$lane" >/dev/null
  done
  echo "epic:$epic -> lane:$lane (${#nums[@]} issues)"
done

echo
echo "Issues with no lane yet (fix these before turning agents on):"
gh issue list -R "$REPO" --state open --limit 500 --json number,title,labels \
  --jq '.[] | select([.labels[].name | startswith("lane:")] | any | not) | "  #\(.number) \(.title)"'
