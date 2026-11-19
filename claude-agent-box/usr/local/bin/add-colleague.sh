#!/usr/bin/env bash
# Admin script: create the Linux user and agent for one colleague. Run on the box as root.
#
#   sudo add-colleague.sh [--no-docker] [--no-claude-install] [--no-jira] [--token-file FILE] <name> <work-email>
#
# The colleague's OWN GitHub fine-grained token is asked for first (hidden prompt, or --token-file) and checked on
# every repo in repos.txt (read, push, PR read, PR create). If any check fails, nothing is created. There is no
# shared GitHub token for agents: the token lives only in that colleague's home.
# The same goes for Jira: it asks for the colleague's Jira email (default: their work email) and API token, and checks them
# against Jira first. Only the LiteLLM key and the Jira site url are shared. --no-jira skips it (no Jira for that person).
#
# Creates: Linux user <name> (no password, no sudo), its own clones of the vylo repos (from the shared
# mirrors), Claude Code, the MCP starter kit, a pre-created audit log, and a 'pending' entry for the
# email in /etc/claude-agent/people.json. It does NOT start the agent: the colleague sends "hi" to the
# Chat bot, the bridge matches the email, runs the Claude sign-in over Chat, and then enables
# claude-agent@<name>. The agent is named <name>-agent.
#
# Docker: the user joins the docker group so the agent can build test images. A docker-group member is
# root-equivalent on this box (it could edit the managed policy), so the guard protects against a
# mistaken agent, not a malicious one. --no-docker withholds it.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
cd /     # colleagues run commands from here: the admin's own folder (often mode 700) is not theirs to enter
DOCKER=1; CLAUDE=1; JIRA=1; ARGS=(); TOKEN_FILE=""; JIRA_TOKEN_FILE=""; JIRA_EMAIL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-docker) DOCKER=0 ;;
    --no-claude-install) CLAUDE=0 ;;
    --no-jira) JIRA=0 ;;
    --jira-token-file) JIRA_TOKEN_FILE=${2:?--jira-token-file needs a path}; shift ;;
    --jira-email) JIRA_EMAIL=${2:?--jira-email needs a value}; shift ;;
    --token-file) TOKEN_FILE=${2:?--token-file needs a path}; shift ;;
    *) ARGS+=("$1") ;;
  esac
  shift
done
NAME=${ARGS[0]:-}; EMAIL=${ARGS[1]:-}
[[ $NAME =~ ^[a-z][a-z0-9_-]{1,19}$ ]] || { echo "usage: $0 [--no-docker] [--no-claude-install] [--no-jira] [--token-file FILE] [--jira-token-file FILE] [--jira-email EMAIL] <name> <email>   (name: a-z 0-9 _ -, 2-20 chars)" >&2; exit 1; }
[[ $EMAIL =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || { echo "bad email: $EMAIL" >&2; exit 1; }
case "$NAME" in root|ubuntu|ec2-user|admin|claude|docker|daemon|nobody|www-data) echo "reserved name: $NAME" >&2; exit 1 ;; esac
if id "$NAME" >/dev/null 2>&1; then echo "Linux user $NAME already exists" >&2; exit 1; fi

PEOPLE=/etc/claude-agent/people.json; MIRRORS=/srv/vylo/mirrors; TEMPLATE=/etc/claude-agent/template
LOG_DIR=/var/log/claude-agent; HOME_DIR=/home/$NAME; WORKDIR=$HOME_DIR/Documents/code/vylo
if [ -f /etc/claude-agent/agent.env ]; then . /etc/claude-agent/agent.env; fi   # optional: BOT_GIT_NAME, BOT_GIT_EMAIL
ls "$MIRRORS"/*.git >/dev/null 2>&1 || { echo "no repo mirrors: run vylo-mirror-sync.sh first" >&2; exit 1; }
install -d -m 750 /etc/claude-agent; install -d -m 711 "$LOG_DIR"

# the colleague's own GitHub token: checked on every repo BEFORE anything is created
if [ -n "$TOKEN_FILE" ]; then IFS= read -r TOKEN <"$TOKEN_FILE"; else read -r -s -p "GitHub token for $NAME (hidden): " TOKEN; echo; fi
TOKEN=${TOKEN//[[:space:]]/}
[ -n "$TOKEN" ] || { echo "no token entered" >&2; exit 1; }
META=$(mktemp); trap 'rm -f "$META"' EXIT
printf '%s' "$TOKEN" | /usr/local/bin/gh-token-check.sh --meta "$META" || { echo "Not created: fix the token first." >&2; exit 1; }
GH_LOGIN=$(sed -nE 's/^login=(.*)$/\1/p' "$META")
OTHER=$(jq -r --arg l "$GH_LOGIN" '.people[]|select(.github_login==$l)|.name' "$PEOPLE" 2>/dev/null | head -1 || true)
[ -z "$OTHER" ] || { echo "That token belongs to GitHub user '$GH_LOGIN', already used by '$OTHER'. Each colleague needs their own." >&2; exit 1; }

# the colleague's own Jira login, checked before anything is created (only when the box has a Jira starter kit)
if [ "$JIRA" = 1 ] && grep -qE '^WE_ATLASSIAN_URL=.+' "$TEMPLATE/wheelseye-secrets.env" 2>/dev/null; then
  if [ -z "$JIRA_EMAIL" ]; then read -r -p "Jira email for $NAME [$EMAIL]: " JIRA_EMAIL; JIRA_EMAIL=${JIRA_EMAIL:-$EMAIL}; fi
  if [ -n "$JIRA_TOKEN_FILE" ]; then IFS= read -r JTOKEN <"$JIRA_TOKEN_FILE"; else read -r -s -p "Jira API token for $NAME (hidden): " JTOKEN; echo; fi
  JTOKEN=${JTOKEN//[[:space:]]/}
  [ -n "$JTOKEN" ] || { echo "no Jira token entered (use --no-jira to create the user without Jira)" >&2; exit 1; }
  JMETA=$(mktemp)
  printf '%s' "$JTOKEN" | /usr/local/bin/jira-token-check.sh --email "$JIRA_EMAIL" --meta "$JMETA" || { echo "Not created: fix the Jira token first." >&2; exit 1; }
else
  JIRA=0; echo "note: no Jira for $NAME (no Jira starter kit on this box, or --no-jira)"
fi

CREATED_USER=0; REGISTERED=0; OK=0
people() {   # people add|remove   (atomic, under a lock)
  flock /etc/claude-agent/people.lock python3 - "$PEOPLE" "$1" "$NAME" "$EMAIL" <<'PY'
import datetime, json, os, sys
p, op, name, email = sys.argv[1:5]
d = json.load(open(p)) if os.path.exists(p) else {"people": []}
if op == "add":
    for x in d["people"]:
        if x["name"] == name or x["email"].lower() == email.lower():
            sys.exit("already registered: " + x["name"])
    d["people"].append({"name": name, "email": email.lower(), "linux_user": name, "status": "pending",
                        "chat_space": None, "chat_user": None, "session_id": None,
                        "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
else:
    d["people"] = [x for x in d["people"] if x["name"] != name]
tmp = p + ".tmp"
json.dump(d, open(tmp, "w"), indent=2)
os.chmod(tmp, 0o640)
os.replace(tmp, p)
PY
}
rollback() {
  if [ "$OK" = 1 ]; then return 0; fi
  echo "FAILED: rolling back $NAME" >&2
  if [ "$REGISTERED" = 1 ]; then people remove >/dev/null 2>&1 || true; fi
  if [ "$CREATED_USER" = 1 ]; then
    pkill -u "$NAME" 2>/dev/null || true
    userdel -r "$NAME" 2>/dev/null || true
    if [ -f "$LOG_DIR/$NAME.jsonl" ]; then mv "$LOG_DIR/$NAME.jsonl" "$LOG_DIR/$NAME.jsonl.failed-add"; fi
  fi
  return 0
}
trap 'rm -f "$META" "${JMETA:-}"; rollback' EXIT
as_user() { runuser -u "$NAME" -- env HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@"; }

people add; REGISTERED=1
useradd -m -s /bin/bash -c "Vylo agent for $EMAIL" "$NAME"; CREATED_USER=1
usermod -L "$NAME"        # lock the password (usermod is in shadow-utils everywhere; passwd is not always installed)
chmod 700 "$HOME_DIR"
if [ "$DOCKER" = 1 ]; then usermod -aG docker "$NAME"; fi
install -m 600 -o "$NAME" -g "$NAME" /dev/null "$LOG_DIR/$NAME.jsonl"

# git identity + the repos (hard-linked clones of the shared mirrors, origin pointed at GitHub)
as_user git config --global user.name "${BOT_GIT_NAME:-Vylo Agent ($NAME)}"
as_user git config --global user.email "${BOT_GIT_EMAIL:-vylo-agent@invalid.example}"
as_user git config --global url."https://github.com/".insteadOf "git@github.com:"
as_user git config --global init.defaultBranch master
as_user mkdir -p "$WORKDIR"
for m in "$MIRRORS"/*.git; do
  repo=$(basename "$m" .git)
  as_user git clone -q "$m" "$WORKDIR/$repo"
  url=$(git --git-dir="$m" config remote.origin.url)
  as_user git -C "$WORKDIR/$repo" remote set-url origin "$url"
done
if [ -f /srv/vylo/context.md ]; then ln -sf /srv/vylo/context.md "$WORKDIR/context.md"; fi
# the colleague's own GitHub login (already checked above); stored only in this user's home
printf '%s\n' "$TOKEN" | /usr/local/bin/set-gh-token.sh --verified "$META" "$NAME"
unset TOKEN

# Claude Code, then onboarding/trust pre-seeded so the unattended agent never stops at a dialog
if [ "$CLAUDE" = 1 ]; then
  as_user bash -c 'curl -fsSL https://claude.ai/install.sh | bash' >/dev/null 2>&1 || { echo "Claude install failed" >&2; exit 1; }
fi
jq -n --arg wd "$WORKDIR" '{hasCompletedOnboarding:true,theme:"dark",projects:{($wd):{hasTrustDialogAccepted:true,allowedTools:[],mcpServers:{},enabledMcpjsonServers:[],disabledMcpjsonServers:[]}}}' \
  | install -m 600 -o "$NAME" -g "$NAME" /dev/stdin "$HOME_DIR/.claude.json"

if [ -d "$TEMPLATE" ]; then
  as_user mkdir -p "$HOME_DIR/.claude"
  cp -a "$TEMPLATE/." "$HOME_DIR/.claude/"
  find "$HOME_DIR/.claude" -type f \( -name '*.sh' -o -name '*.json' -o -name '*.env' -o -name '*.md' \) -exec sed -i "s#@HOME@#$HOME_DIR#g" {} +
  chown -R "$NAME:$NAME" "$HOME_DIR/.claude"; chmod -R go-rwx "$HOME_DIR/.claude"
  if [ "$CLAUDE" = 1 ] && [ -f "$HOME_DIR/.claude/mcp-prod.json" ]; then
    for s in $(jq -r '.mcpServers | keys[]' "$HOME_DIR/.claude/mcp-prod.json"); do
      jq -c --arg s "$s" '.mcpServers[$s]' "$HOME_DIR/.claude/mcp-prod.json" \
        | as_user bash -c 'J=$(cat); claude mcp remove -s user "$1" >/dev/null 2>&1; claude mcp add-json -s user "$1" "$J" >/dev/null 2>&1' _ "$s" \
        || echo "warning: MCP $s not registered" >&2
    done
  fi
else
  echo "note: no MCP template (run claude-agent-seed-template.sh): $NAME starts without the Wheelseye MCP servers"
fi
if [ "$JIRA" = 1 ]; then      # the colleague's own Jira login, added to the secrets file copied from the kit
  printf '%s\n' "$JTOKEN" | /usr/local/bin/set-jira-token.sh --verified "$JMETA" --email "$JIRA_EMAIL" "$NAME"
  unset JTOKEN
fi
OK=1
cat <<MSG
Created $NAME ($EMAIL), status 'pending'. Agent will be named ${NAME}-agent.
Next: ask $EMAIL to send "hi" to the Vylo Agent bot in Google Chat. The bridge then runs the sign-in and
starts claude-agent@$NAME.   Check:  sudo jq . $PEOPLE
MSG
