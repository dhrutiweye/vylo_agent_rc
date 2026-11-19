#!/usr/bin/env bash
# Give ONE colleague their own GitHub token (or replace it when it expires). Run on the box as root.
#
#   sudo set-gh-token.sh <name>                          hidden prompt; the token is checked first, then installed
#   sudo set-gh-token.sh --token-file FILE <name>        read the token from a file instead (delete the file yourself)
#
# The token is checked on every repo in repos.txt (read, push, PR read, PR create) BEFORE anything is installed; if a
# repo fails nothing changes. It is stored only in /home/<name>/.config/gh/hosts.yml (mode 600) and is never copied
# anywhere else. add-colleague.sh does the same check before it creates the user, then calls this with --verified.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
cd /
PEOPLE=/etc/claude-agent/people.json; TOKEN_FILE=""; VERIFIED=""; NAME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --token-file) TOKEN_FILE=${2:?--token-file needs a path}; shift 2 ;;
    --verified)   VERIFIED=${2:?--verified needs the meta file}; shift 2 ;;   # internal: add-colleague.sh already checked it
    -*) echo "unknown option $1" >&2; exit 64 ;;
    *) NAME=$1; shift ;;
  esac
done
[[ $NAME =~ ^[a-z][a-z0-9_-]{1,19}$ ]] || { echo "usage: $0 [--token-file FILE] <name>" >&2; exit 64; }
id "$NAME" >/dev/null 2>&1 || { echo "no Linux user $NAME (add-colleague.sh creates it)" >&2; exit 1; }
HOME_DIR=/home/$NAME

if [ -n "$VERIFIED" ]; then IFS= read -r TOKEN
elif [ -n "$TOKEN_FILE" ]; then IFS= read -r TOKEN <"$TOKEN_FILE"
else read -r -s -p "GitHub token for $NAME (hidden): " TOKEN; echo; fi
TOKEN=${TOKEN//[[:space:]]/}
[ -n "$TOKEN" ] || { echo "no token entered" >&2; exit 1; }

META=${VERIFIED:-}
if [ -z "$VERIFIED" ]; then
  META=$(mktemp); trap 'rm -f "$META"' EXIT
  printf '%s' "$TOKEN" | /usr/local/bin/gh-token-check.sh --meta "$META" || { echo "Nothing was changed for $NAME." >&2; exit 1; }
fi
login=$(sed -nE 's/^login=(.*)$/\1/p' "$META"); expires=$(sed -nE 's/^expires=(.*)$/\1/p' "$META")

# one token, one person: refuse a GitHub login another colleague already uses
other=$(jq -r --arg l "$login" --arg n "$NAME" '.people[]|select(.github_login==$l and .name!=$n)|.name' "$PEOPLE" 2>/dev/null | head -1)
[ -z "$other" ] || { echo "That token belongs to GitHub user '$login', already used by '$other'. Each colleague needs their own." >&2; exit 1; }

install -d -m 700 -o "$NAME" -g "$NAME" "$HOME_DIR/.config" "$HOME_DIR/.config/gh"
printf '%s' "$TOKEN" | runuser -u "$NAME" -- env HOME="$HOME_DIR" gh auth login --hostname github.com --git-protocol https \
  --with-token --insecure-storage || { echo "gh could not store the login for $NAME" >&2; exit 1; }
chmod 600 "$HOME_DIR/.config/gh/hosts.yml"
runuser -u "$NAME" -- env HOME="$HOME_DIR" gh auth setup-git >/dev/null 2>&1 || echo "warning: gh auth setup-git failed for $NAME" >&2

if [ -f "$PEOPLE" ]; then
  flock /etc/claude-agent/people.lock python3 - "$PEOPLE" "$NAME" "$login" "$expires" <<'PY'
import json, os, sys
p, name, login, exp = sys.argv[1:5]
d = json.load(open(p))
for x in d["people"]:
    if x["name"] == name:
        x["github_login"] = login; x["github_token_expires"] = exp or None
json.dump(d, open(p + ".tmp", "w"), indent=2); os.chmod(p + ".tmp", 0o640); os.replace(p + ".tmp", p)
PY
fi
echo "$NAME now pushes and opens PRs as GitHub user '$login' (token expires ${expires:-never})."
