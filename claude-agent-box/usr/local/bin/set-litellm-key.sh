#!/usr/bin/env bash
# Replace the shared LiteLLM key (WE_PROD_KEY) everywhere the box holds it: the starter kit and every agent home
# (/home/*/.claude/wheelseye-secrets.env). Run on the box as root.
#
#   sudo set-litellm-key.sh [--user NAME] [--key-file FILE] [--host GATEWAY] [--yes]
#
# --user writes only that person's own key (/home/NAME/.claude/wheelseye-secrets.env), never the kit or anyone else's;
# the Chat bot uses it when someone pastes their key there, feeding the key on stdin.
# --host also moves every file to another gateway (WE_LITELLM_HOST); without it the current host is kept.
# The key is read at a hidden prompt (or the first line of FILE) and checked against the gateway BEFORE anything is
# written: 200 writes, 401/403 refuses, anything else asks (or refuses with --yes). Each file keeps its owner and mode;
# only the WE_PROD_KEY line changes. The key never appears on a command line.
# Sessions already open keep the old key until their Jira MCP restarts: start a new session, or /mcp -> reconnect.
# Tests only: VYLO_HOME_ROOT (default /home), CLAUDE_AGENT_TEMPLATE (default /etc/claude-agent/template),
# LITELLM_NO_CHECK=1 (skip the gateway check), TEST_NO_ROOT=1.
set -uo pipefail
HOMES=${VYLO_HOME_ROOT:-/home}
T=${CLAUDE_AGENT_TEMPLATE:-/etc/claude-agent/template}
die() { echo "set-litellm-key: $*" >&2; exit 1; }

KEY_FILE="" YES="" NEW_HOST="" ONE_USER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --key-file) KEY_FILE=${2:?--key-file needs a path}; shift 2 ;;
    --user) ONE_USER=${2:?--user needs a name}; shift 2 ;;
    --host) NEW_HOST=${2:?--host needs a gateway host}; shift 2 ;;
    --yes|-y) YES=1; shift ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument $1" ;;
  esac
done
[ -n "${TEST_NO_ROOT:-}" ] || [ "$(id -u)" = 0 ] || die "run it as root (sudo)"
cd /

files=()
if [ -n "$ONE_USER" ]; then
  [[ $ONE_USER =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "not a user name: $ONE_USER"
  f="$HOMES/$ONE_USER/.claude/wheelseye-secrets.env"
  [ -f "$f" ] && [ ! -L "$f" ] || die "$ONE_USER has no wheelseye-secrets.env; nothing changed"
  files=("$f")
else
  [ -f "$T/wheelseye-secrets.env" ] && files+=("$T/wheelseye-secrets.env")
  for f in "$HOMES"/*/.claude/wheelseye-secrets.env; do [ -f "$f" ] && files+=("$f"); done
  [ "${#files[@]}" -gt 0 ] || die "no wheelseye-secrets.env found (kit: $T, homes: $HOMES/*/.claude)"
fi

cur_host=$(sed -nE 's/^WE_LITELLM_HOST=(.*)$/\1/p' "${files[0]}" | head -1)
host=${NEW_HOST:-${cur_host:-litellm.prod-we.com}}
[[ $host =~ ^[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || die "not a host name: $host"

if [ -n "$KEY_FILE" ]; then IFS= read -r KEY <"$KEY_FILE" || true
else read -r -s -p "New LiteLLM key for $host (hidden): " KEY; echo; fi
KEY=${KEY//[[:space:]]/}
[ -n "$KEY" ] || die "no key entered; nothing changed"
[[ $KEY =~ ^[A-Za-z0-9._-]+$ ]] || die "the key has unexpected characters; nothing changed"

if [ -z "${LITELLM_NO_CHECK:-}" ]; then
  # The same MCP handshake the agents make. (/key/info is no use: a key limited to jira_mcp gets 403 there even when
  # valid.) The header comes from a file descriptor, so the key is not visible in ps.
  init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"set-litellm-key","version":"1"}}}'
  out=$(curl -s -m 15 -w '\n%{http_code}' -X POST -H @<(printf 'x-litellm-api-key: Bearer %s\n' "$KEY") \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' --data "$init" "http://$host/mcp/jira_mcp")
  code=$(tail -n1 <<<"$out")
  if [ "$code" = 200 ] && grep -q '"protocolVersion"' <<<"$out"; then code=ok; fi
  case "$code" in
    ok) echo "gateway $host accepts the key for jira_mcp." ;;
    401|403) die "gateway $host rejected the key for jira_mcp (HTTP $code); nothing changed" ;;
    *) echo "could not confirm the key with $host (HTTP ${code:-none})."
       [ -z "$YES" ] || die "refusing with --yes; nothing changed"
       read -r -p "Write it anyway? [y/N] " a; [[ $a =~ ^[Yy] ]] || die "nothing changed" ;;
  esac
fi

changed=0 same=0 failed=0
for f in "${files[@]}"; do
  if grep -qxF "WE_PROD_KEY=$KEY" "$f" && { [ -z "$NEW_HOST" ] || grep -qxF "WE_LITELLM_HOST=$host" "$f"; }; then
    same=$((same+1)); continue
  fi
  owner=$(stat -c %u:%g "$f") mode=$(stat -c %a "$f")
  tmp=$(mktemp "$f.XXXXXX") || { echo "  $f: cannot write next to it" >&2; failed=$((failed+1)); continue; }
  { if [ -n "$NEW_HOST" ]; then grep -vE '^WE_(PROD_KEY|LITELLM_HOST)=' "$f"; printf 'WE_LITELLM_HOST=%s\n' "$host"
    else grep -v '^WE_PROD_KEY=' "$f"; fi
    printf 'WE_PROD_KEY=%s\n' "$KEY"; } >"$tmp" \
    && chown "$owner" "$tmp" && chmod "$mode" "$tmp" && mv -f "$tmp" "$f" \
    && { changed=$((changed+1)); echo "  updated $f"; } \
    || { rm -f "$tmp"; echo "  $f: could not update it" >&2; failed=$((failed+1)); }
done
echo "LiteLLM key: $changed file(s) updated, $same already had it, $failed failed."
echo "Open sessions keep the old key until their Jira MCP restarts (new session, or /mcp -> reconnect)."
[ "$failed" = 0 ]
