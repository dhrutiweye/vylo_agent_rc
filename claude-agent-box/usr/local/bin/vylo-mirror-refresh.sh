#!/usr/bin/env bash
# Build/refresh the shared, read-only repo mirrors that new colleagues clone from, so a new agent gets
# the vylo code in seconds with no GitHub credentials (and hard-linked objects: almost no extra disk).
#
#   sudo vylo-mirror-refresh.sh [SRC]        SRC defaults to /home/ubuntu/Documents/code/vylo
#
# Mirrors live in /srv/vylo/mirrors/<repo>.git; each branch of the source's `origin` becomes a branch
# of the mirror, and `remote.origin.url` is kept as the REAL GitHub url so a colleague's clone points
# at GitHub. The mirror is only as fresh as SRC's last fetch. context.md is copied to /srv/vylo/.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
SRC=${1:-/home/ubuntu/Documents/code/vylo}
DST=/srv/vylo
install -d -m 755 "$DST" "$DST/mirrors"
n=0
for d in "$SRC"/*/; do
  [ -d "$d/.git" ] || continue
  repo=$(basename "$d"); m="$DST/mirrors/$repo.git"
  url=$(git -c safe.directory='*' -C "$d" remote get-url origin 2>/dev/null) || { echo "skip $repo: no origin"; continue; }
  [ -d "$m" ] || git init -q --bare "$m"
  git --git-dir="$m" config remote.origin.url "$url"
  git -c safe.directory='*' --git-dir="$m" fetch -q --no-tags "$d" '+refs/remotes/origin/*:refs/heads/*' '+refs/tags/*:refs/tags/*'
  git --git-dir="$m" update-ref -d refs/heads/HEAD 2>/dev/null || true
  def=$(git -c safe.directory='*' -C "$d" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##' || true)
  for cand in "$def" master main; do
    if [ -n "$cand" ] && git --git-dir="$m" show-ref -q --verify "refs/heads/$cand"; then
      git --git-dir="$m" symbolic-ref HEAD "refs/heads/$cand"; break
    fi
  done
  # a local clone of a root-owned bare repo is "dubious ownership" to git without this
  git config --system --get-all safe.directory | grep -qxF "$m" || git config --system --add safe.directory "$m"
  n=$((n+1)); echo "mirrored $repo ($(git --git-dir="$m" symbolic-ref --short HEAD))"
done
[ -f "$SRC/context.md" ] && install -m 644 "$SRC/context.md" "$DST/context.md"
chmod -R a+rX "$DST"
echo "$n repos in $DST/mirrors ($(du -sh "$DST/mirrors" | cut -f1))"
