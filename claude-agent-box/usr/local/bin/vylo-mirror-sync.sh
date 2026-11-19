#!/usr/bin/env bash
# Build/refresh the shared, read-only repo mirrors from GitHub, using the shared bot login. New colleagues clone from
# these mirrors (add-colleague.sh), so a new agent gets the code in seconds and no per-person GitHub login is needed.
#
#   sudo vylo-mirror-sync.sh            reads /etc/claude-agent/repos.txt  (lines: <folder-name> <https-url>)
#
# Needs /etc/claude-agent/gh-hosts.yml (the bot login; see github-bot-login.sh). Run daily by vylo-mirror-sync.timer.
# Only branches and tags are mirrored (not refs/pull/*), so the mirrors stay small.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
LIST=/etc/claude-agent/repos.txt; HOSTS=/etc/claude-agent/gh-hosts.yml
DST=/srv/vylo; [ -r "$LIST" ] || { echo "missing $LIST" >&2; exit 1; }
[ -r "$HOSTS" ] || { echo "missing $HOSTS: run github-bot-login.sh first" >&2; exit 1; }
install -d -m 755 "$DST" "$DST/mirrors"
GHCFG=$(mktemp -d); trap 'rm -rf "$GHCFG"' EXIT
install -m 600 "$HOSTS" "$GHCFG/hosts.yml"; export GH_CONFIG_DIR=$GHCFG GIT_TERMINAL_PROMPT=0
cred=(-c credential.helper= -c "credential.helper=!gh auth git-credential")
n=0; fail=0
while read -r repo url _; do
  case "$repo" in ''|\#*) continue ;; esac
  m="$DST/mirrors/$repo.git"
  [ -d "$m" ] || git init -q --bare "$m"
  git --git-dir="$m" config remote.origin.url "$url"
  if ! git "${cred[@]}" --git-dir="$m" fetch -q --no-tags --prune "$url" '+refs/heads/*:refs/heads/*' '+refs/tags/*:refs/tags/*'; then
    echo "FAILED $repo (login lacks access, or no network)" >&2; fail=$((fail+1)); continue
  fi
  def=$(git "${cred[@]}" ls-remote --symref "$url" HEAD 2>/dev/null | awk '/^ref:/{sub("refs/heads/","",$2); print $2; exit}')
  for cand in "$def" master main; do
    if [ -n "$cand" ] && git --git-dir="$m" show-ref -q --verify "refs/heads/$cand"; then git --git-dir="$m" symbolic-ref HEAD "refs/heads/$cand"; break; fi
  done
  git config --system --get-all safe.directory | grep -qxF "$m" || git config --system --add safe.directory "$m"
  n=$((n+1)); echo "mirrored $repo ($(git --git-dir="$m" symbolic-ref --short HEAD))"
done <"$LIST"
chmod -R a+rX "$DST"
echo "$n repos mirrored, $fail failed ($(du -sh "$DST/mirrors" | cut -f1) in $DST/mirrors)"
[ "$fail" = 0 ]
