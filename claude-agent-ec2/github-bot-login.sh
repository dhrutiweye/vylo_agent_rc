#!/usr/bin/env bash
# Give the box its READ-ONLY mirror login: a GitHub token typed at a hidden prompt (never on a command line, in
# shell history or in this folder). It is stored root-only in /etc/claude-agent/gh-hosts.yml and used ONLY by
# vylo-mirror-sync.sh to keep the shared repo mirrors current. Agents never get it: each colleague has their own
# token, set by add-colleague.sh or set-gh-token.sh.
# For the mirrors the token only needs Metadata: read and Contents: read; the list below is for a colleague's token.
#
# Token: Resource owner = the org; Repository access = only the repos in /etc/claude-agent/repos.txt;
#        Metadata: read, Contents: read+write, Pull requests: read+write (optional: Issues, Commit statuses, Actions: read).
#        Leave Workflows OFF so pushes that edit .github/workflows are refused.
# Keep branch protection on master/main/release*: the agent's guard also refuses those pushes and merges, but a
# server-side rule is what holds if the guard ever fails.
#
#   sudo ./github-bot-login.sh            paste a fine-grained token
#   sudo ./github-bot-login.sh --device   use GitHub's device login instead (for a machine user account)
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
export GH_CONFIG_DIR; GH_CONFIG_DIR=$(mktemp -d); trap 'rm -rf "$GH_CONFIG_DIR"' EXIT
if [ "${1:-}" = "--device" ]; then
  gh auth login --hostname github.com --git-protocol https --web
else
  read -r -s -p "Fine-grained GitHub token (hidden): " TOKEN; echo
  [ -n "$TOKEN" ] || { echo "no token entered" >&2; exit 1; }
  printf '%s' "$TOKEN" | gh auth login --hostname github.com --git-protocol https --with-token --insecure-storage \
    || { echo "GitHub rejected the token (wrong value, expired, or the org has not approved it yet)" >&2; exit 1; }
  unset TOKEN
fi
login=$(gh api user --jq .login)
install -m 600 -o root -g root "$GH_CONFIG_DIR/hosts.yml" /etc/claude-agent/gh-hosts.yml
echo "Signed in as '$login' and saved to /etc/claude-agent/gh-hosts.yml."

# Read access to every repo on the list (push is proven by the first draft PR).
if [ -r /etc/claude-agent/repos.txt ]; then
  echo "Checking read access to each repo:"
  while read -r repo url _; do
    case "$repo" in ''|\#*) continue ;; esac
    if GIT_TERMINAL_PROMPT=0 git -c credential.helper= -c "credential.helper=!gh auth git-credential" ls-remote --exit-code "$url" HEAD >/dev/null 2>&1; then
      printf '  ok    %s\n' "$repo"
    else
      printf '  FAIL  %s  (token not given this repo, or org approval pending)\n' "$repo"
    fi
  done </etc/claude-agent/repos.txt
fi
echo "Next: sudo vylo-mirror-sync.sh"
