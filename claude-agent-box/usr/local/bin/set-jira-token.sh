#!/usr/bin/env bash
# Give ONE colleague their own Jira login (or replace it when the token is rotated). Run on the box as root.
#
#   sudo set-jira-token.sh [--email EMAIL] [--token-file FILE] <name>
#
# The email defaults to the colleague's work email from people.json. The token is checked against Jira BEFORE anything is
# changed; on failure the old values stay. It is stored only in /home/<name>/.claude/wheelseye-secrets.env (mode 600),
# next to the shared LiteLLM key, and read by that user's Jira MCP wrapper. add-colleague.sh does the same check before
# it creates the user, then calls this with --verified.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
cd /
PEOPLE=/etc/claude-agent/people.json; EMAIL=""; TOKEN_FILE=""; VERIFIED=""; NAME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --email) EMAIL=${2:?--email needs a value}; shift 2 ;;
    --token-file) TOKEN_FILE=${2:?--token-file needs a path}; shift 2 ;;
    --verified) VERIFIED=${2:?--verified needs the meta file}; shift 2 ;;   # internal: add-colleague.sh already checked it
    -*) echo "unknown option $1" >&2; exit 64 ;;
    *) NAME=$1; shift ;;
  esac
done
[[ $NAME =~ ^[a-z][a-z0-9_-]{1,19}$ ]] || { echo "usage: $0 [--email EMAIL] [--token-file FILE] <name>" >&2; exit 64; }
id "$NAME" >/dev/null 2>&1 || { echo "no Linux user $NAME" >&2; exit 1; }
HOME_DIR=/home/$NAME; SEC=$HOME_DIR/.claude/wheelseye-secrets.env
[ -f "$SEC" ] || { echo "$NAME has no starter kit ($SEC): build it with configure-box.sh, then add the colleague again" >&2; exit 1; }
[ -n "$EMAIL" ] || EMAIL=$(jq -r --arg n "$NAME" '.people[]|select(.name==$n)|.email' "$PEOPLE" 2>/dev/null | head -1)
[ -n "$EMAIL" ] || { echo "no email known for $NAME: pass --email" >&2; exit 1; }

if [ -n "$VERIFIED" ]; then IFS= read -r TOKEN
elif [ -n "$TOKEN_FILE" ]; then IFS= read -r TOKEN <"$TOKEN_FILE"
else read -r -s -p "Jira API token for $NAME ($EMAIL) (hidden): " TOKEN; echo; fi
TOKEN=${TOKEN//[[:space:]]/}
[ -n "$TOKEN" ] || { echo "no token entered" >&2; exit 1; }
if [ -z "$VERIFIED" ]; then
  printf '%s' "$TOKEN" | /usr/local/bin/jira-token-check.sh --email "$EMAIL" || { echo "Nothing was changed for $NAME." >&2; exit 1; }
fi

tmp=$(mktemp -p "$HOME_DIR/.claude")
grep -vE '^WE_ATLASSIAN_(USERNAME|TOKEN)=' "$SEC" >"$tmp" || true
printf 'WE_ATLASSIAN_USERNAME=%s\nWE_ATLASSIAN_TOKEN=%s\n' "$EMAIL" "$TOKEN" >>"$tmp"
chown "$NAME:$NAME" "$tmp"; chmod 600 "$tmp"; mv "$tmp" "$SEC"
if [ -f "$PEOPLE" ]; then
  flock /etc/claude-agent/people.lock python3 - "$PEOPLE" "$NAME" "$EMAIL" <<'PY'
import json, os, sys
p, name, email = sys.argv[1:4]
d = json.load(open(p))
for x in d["people"]:
    if x["name"] == name: x["jira_user"] = email
json.dump(d, open(p + ".tmp", "w"), indent=2); os.chmod(p + ".tmp", 0o640); os.replace(p + ".tmp", p)
PY
fi
echo "$NAME now uses Jira as $EMAIL."
