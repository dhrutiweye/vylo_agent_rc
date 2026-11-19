#!/usr/bin/env bash
# Check that a GitHub fine-grained token can do what one agent needs on every repo in repos.txt. Creates and changes
# nothing on GitHub. The token is read from stdin (one line), never from a command line.
#
#   printf '%s' "$TOKEN" | gh-token-check.sh [--meta FILE]
#
# Per repo it proves: read (git ls-remote), push (git push --dry-run to a throwaway ref: nothing is sent),
# pr-read (GET /pulls) and pr-create (a draft PR built to fail validation: 422 = allowed, 403/404 = not allowed).
# --meta FILE writes "login=" and "expires=" lines (mode 600) for the caller.
# Exit: 0 all repos pass | 1 the token itself is unusable | 2 the token is valid but lacks access on some repo.
set -uo pipefail
LIST=${REPOS_FILE:-/etc/claude-agent/repos.txt}; META=""
while [ $# -gt 0 ]; do
  case "$1" in
    --meta) META=${2:?--meta needs a file}; shift 2 ;;
    *) echo "usage: gh-token-check.sh [--meta FILE]   (token on stdin)" >&2; exit 64 ;;
  esac
done
[ -r "$LIST" ] || { echo "missing $LIST" >&2; exit 1; }

IFS= read -r T || true
T=${T//[[:space:]]/}
[ -n "$T" ] || { echo "no token given" >&2; exit 1; }
case "$T" in
  github_pat_*) ;;
  ghp_*|gho_*|ghu_*|ghs_*) echo "That is a classic or app token. Use a fine-grained token limited to the repos on the list." >&2; exit 1 ;;
  *) echo "That does not look like a GitHub token (a fine-grained one starts with github_pat_)." >&2; exit 1 ;;
esac

W=$(mktemp -d -p /dev/shm 2>/dev/null || mktemp -d); chmod 700 "$W"; trap 'rm -rf "$W"' EXIT
# the token reaches curl through a header file and git through its environment, never through argv (ps shows argv)
printf 'Authorization: Bearer %s\n' "$T" >"$W/h"; chmod 600 "$W/h"
api() { curl -s -m 20 -H @"$W/h" -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" "$@"; }

code=$(api -D "$W/hdr" -o "$W/user" -w '%{http_code}' https://api.github.com/user)
if [ "$code" != 200 ]; then
  msg=$(jq -r '.message // empty' "$W/user" 2>/dev/null)
  echo "GitHub rejected the token (HTTP $code${msg:+: $msg}). Wrong value, expired, revoked, or the org has not approved it yet." >&2
  exit 1
fi
login=$(jq -r .login "$W/user")
expires=$(awk -F': ' 'tolower($1)=="github-authentication-token-expiration"{print $2}' "$W/hdr" | tr -d '\r')
echo "token of GitHub user '$login', expires ${expires:-never}"
if [ -n "$expires" ]; then
  exp_s=$(date -u -d "$expires" +%s 2>/dev/null || echo 0)
  if [ "$exp_s" -gt 0 ] && [ "$exp_s" -lt $(( $(date -u +%s) + 14*86400 )) ]; then echo "WARN  this token expires in under 14 days"; fi
fi
if [ -n "$META" ]; then
  umask 077; printf 'login=%s\nexpires=%s\n' "$login" "$expires" >"$META"
fi

export GIT_TERMINAL_PROMPT=0 GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=credential.helper GIT_CONFIG_VALUE_0= \
       GIT_CONFIG_KEY_1=http.extraHeader
GIT_CONFIG_VALUE_1="Authorization: Basic $(printf 'x-access-token:%s' "$T" | base64 | tr -d '\n')"; export GIT_CONFIG_VALUE_1
git -C "$W" init -q probe && git -C "$W/probe" -c user.email=t@t -c user.name=t commit -q --allow-empty -m probe

printf '%-26s %-5s %-5s %-8s %-9s\n' repo read push pr-read pr-create
missing=0
while read -r repo url _; do
  case "$repo" in ''|\#*) continue ;; esac
  slug=${url#https://github.com/}; slug=${slug%.git}
  rd=no; git ls-remote --exit-code "$url" HEAD >/dev/null 2>&1 && rd=yes
  ps=no; (cd "$W/probe" && git push --dry-run "$url" HEAD:refs/heads/zz-token-check-do-not-create >/dev/null 2>&1) && ps=yes
  pr=no; [ "$(api -o /dev/null -w '%{http_code}' "https://api.github.com/repos/$slug/pulls?per_page=1")" = 200 ] && pr=yes
  pc=$(api -o /dev/null -w '%{http_code}' -X POST "https://api.github.com/repos/$slug/pulls" \
        -d '{"title":"token check","head":"zz-token-check-no-such-branch","base":"master","draft":true}')
  if [ "$pc" = 422 ]; then pcr=yes; else pcr=no; fi
  printf '%-26s %-5s %-5s %-8s %-9s\n' "$repo" "$rd" "$ps" "$pr" "$pcr"
  if [ "$rd$ps$pr$pcr" != yesyesyesyes ]; then missing=$((missing+1)); fi
done <"$LIST"

if [ "$missing" -gt 0 ]; then
  echo "FAIL  the token lacks access on $missing repo(s). It needs: Metadata read, Contents read+write, Pull requests read+write," >&2
  echo "      on every repo above, with the org's approval." >&2
  exit 2
fi
echo "OK    the token can read, push and open PRs on every repo."
