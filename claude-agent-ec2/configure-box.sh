#!/usr/bin/env bash
# Enter the settings and secrets for this box. Secrets are typed at hidden prompts and written straight to
# root-only files: nothing is stored in this folder, in shell history, or on a command line.
#
#   sudo ./configure-box.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
ENVF=/etc/claude-agent/bridge.env
[ -f "$ENVF" ] || { echo "run ./ec2-setup.sh first" >&2; exit 1; }
ask()    { local v; read -r -p "$1${2:+ [$2]}: " v; printf '%s' "${v:-${2:-}}"; }
secret() { local v; read -r -s -p "$1: " v; echo >&2; printf '%s' "$v"; }
yes_no() { local a; read -r -p "$1 [Y/n]: " a; [[ ! $a =~ ^[Nn] ]]; }
setenvf() { # setenvf KEY VALUE
  if grep -qE "^$1=" "$ENVF"; then
    awk -v k="$1" -v v="$2" 'BEGIN{FS=OFS="="} $1==k {print k"="v; next} {print}' "$ENVF" >"$ENVF.new" && mv "$ENVF.new" "$ENVF"
  else printf '%s=%s\n' "$1" "$2" >>"$ENVF"; fi
  chmod 640 "$ENVF"
}

if yes_no "1/2  Configure the Google Chat bridge?"; then
  setenvf PROJECT_ID "$(ask 'Google Cloud project id')"
  setenvf SUBSCRIPTION_ID "$(ask 'Pub/Sub subscription id (short name, not the full path)')"
  setenvf ALLOWED_EMAIL_DOMAIN "$(ask 'Only accept messages from this email domain (blank = any registered email)')"
  K=$(ask 'Path of the Chat service-account key file you copied to this box')
  [ -f "$K" ] || { echo "no such file: $K" >&2; exit 1; }
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d.get("type")=="service_account" and d.get("client_email"), "not a service-account key"; print("key is for", d["client_email"])' "$K"
  install -m 600 -o root -g root "$K" /etc/claude-agent/chat-bot-key.json
  setenvf GOOGLE_APPLICATION_CREDENTIALS /etc/claude-agent/chat-bot-key.json
  if yes_no "     Securely delete the original key file ($K)?"; then shred -u "$K" && echo "     deleted."; fi
  echo "     bridge settings saved in $ENVF (nudges: first at 5 min, second 5 min later, then every 30 min)"
fi

if yes_no "2/2  Build the Jira starter kit for colleagues (shared LiteLLM key and Jira url only)?"; then
  T=$(mktemp -p /dev/shm 2>/dev/null || mktemp); chmod 600 "$T"; trap 'shred -u "$T" 2>/dev/null || rm -f "$T"' EXIT
  {
    printf 'WE_ATLASSIAN_URL=%s\n'      "$(ask 'Jira/Confluence base url, e.g. https://yourcompany.atlassian.net')"
    printf 'WE_PROD_KEY=%s\n'           "$(secret 'LiteLLM key, limited to the jira_mcp server (hidden)')"
    h=$(ask 'LiteLLM gateway host' litellm.prod-we.com); printf 'WE_LITELLM_HOST=%s\n' "$h"
  } >"$T"
  /usr/local/bin/claude-agent-seed-template.sh --from-env "$T"
fi
echo; echo "Saved. Next: sudo ./github-bot-login.sh"
