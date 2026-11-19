#!/usr/bin/env bash
# Check one person's Jira API token before it is installed. Creates and changes nothing in Jira. The token is read from
# stdin (one line), never from a command line, and reaches curl through a header file (ps shows argv).
#
#   printf '%s' "$TOKEN" | jira-token-check.sh --email EMAIL [--url https://x.atlassian.net] [--meta FILE]
#
# Proves: the email + token pair is valid and the account is active (GET /rest/api/3/myself). If JIRA_PROJECT_KEY is set
# in /etc/claude-agent/agent.env it also proves the account can see that project. --url defaults to WE_ATLASSIAN_URL in the
# starter kit. --meta FILE writes display= and account= lines (mode 600). Exit: 0 ok | 1 not usable.
set -uo pipefail
EMAIL=""; URL=""; META=""
while [ $# -gt 0 ]; do
  case "$1" in
    --email) EMAIL=${2:?--email needs a value}; shift 2 ;;
    --url)   URL=${2:?--url needs a value}; shift 2 ;;
    --meta)  META=${2:?--meta needs a file}; shift 2 ;;
    *) echo "usage: jira-token-check.sh --email EMAIL [--url URL] [--meta FILE]   (token on stdin)" >&2; exit 64 ;;
  esac
done
[ -n "$URL" ] || URL=$(sed -nE 's/^WE_ATLASSIAN_URL=(.*)$/\1/p' /etc/claude-agent/template/wheelseye-secrets.env 2>/dev/null | head -1)
[ -n "$URL" ] || { echo "no Jira url: build the starter kit first (configure-box.sh)" >&2; exit 1; }
URL=${URL%/}; URL=${URL%/wiki}
[[ $EMAIL =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || { echo "bad Jira email: $EMAIL" >&2; exit 1; }
IFS= read -r T || true
T=${T//[[:space:]]/}
[ -n "$T" ] || { echo "no Jira token given" >&2; exit 1; }

W=$(mktemp -d -p /dev/shm 2>/dev/null || mktemp -d); chmod 700 "$W"; trap 'rm -rf "$W"' EXIT
printf 'Authorization: Basic %s\n' "$(printf '%s:%s' "$EMAIL" "$T" | base64 | tr -d '\n')" >"$W/h"; chmod 600 "$W/h"
api() { curl -s -m 20 -H @"$W/h" -H "Accept: application/json" "$@"; }

code=$(api -o "$W/me" -w '%{http_code}' "$URL/rest/api/3/myself")
if [ "$code" != 200 ]; then
  echo "Jira rejected that email and token (HTTP $code). Check the email is the Atlassian account's, and the token is an API token from id.atlassian.com." >&2
  exit 1
fi
display=$(jq -r '.displayName // empty' "$W/me"); account=$(jq -r '.accountId // empty' "$W/me")
if [ "$(jq -r '.active' "$W/me")" = false ]; then echo "That Jira account is not active." >&2; exit 1; fi
echo "Jira account '${display:-?}' ($URL)"
PK=$(sed -nE 's/^JIRA_PROJECT_KEY=(.*)$/\1/p' /etc/claude-agent/agent.env 2>/dev/null | head -1)
if [ -n "$PK" ]; then
  pc=$(api -o /dev/null -w '%{http_code}' "$URL/rest/api/3/project/$PK")
  [ "$pc" = 200 ] || { echo "That account cannot see Jira project $PK (HTTP $pc)." >&2; exit 1; }
  echo "can see project $PK"
fi
if [ -n "$META" ]; then umask 077; printf 'display=%s\naccount=%s\n' "$display" "$account" >"$META"; fi
echo "OK    the Jira token works."
