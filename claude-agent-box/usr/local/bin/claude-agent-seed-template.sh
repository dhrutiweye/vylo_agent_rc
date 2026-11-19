#!/usr/bin/env bash
# Build the per-colleague starter kit /etc/claude-agent/template from an existing agent home: the MCP
# wrapper scripts, the shared MCP secrets file, mcp-prod.json and skills. Credentials, history and
# caches are NOT copied. /home/<user> is rewritten to @HOME@ and filled in by add-colleague.sh.
#
#   sudo claude-agent-seed-template.sh [SRC_HOME]     default /home/ubuntu
#   sudo claude-agent-seed-template.sh --from-env FILE    fresh box: build the Jira kit from FILE (KEY=value lines:
#                                                         WE_PROD_KEY, WE_ATLASSIAN_URL, optional WE_LITELLM_HOST)
#   sudo claude-agent-seed-template.sh --strip-personal   remove any shared Jira user/token an older kit still holds
#
# Jira ONLY: colleagues' agents get the Jira MCP server, the shared LiteLLM key and the Jira site url. Each colleague's
# own Jira email and API token are added to THEIR home by add-colleague.sh / set-jira-token.sh, never to the kit.
# The other servers, the wrapper scripts for them and their credentials (Jenkins, Mongo, Redshift, lake) stay with
# the admin's own home. The template holds the shared LiteLLM key, so it is root-only (700).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
T=/etc/claude-agent/template
if [ "${1:-}" = "--strip-personal" ]; then
  [ -f "$T/wheelseye-secrets.env" ] || { echo "no starter kit at $T" >&2; exit 1; }
  grep -vE '^WE_ATLASSIAN_(USERNAME|TOKEN)=' "$T/wheelseye-secrets.env" >"$T/wheelseye-secrets.env.new" || true
  mv "$T/wheelseye-secrets.env.new" "$T/wheelseye-secrets.env"; chmod 600 "$T/wheelseye-secrets.env"
  echo "kit now holds: $(sed -E 's/=.*//' "$T/wheelseye-secrets.env" | tr '\n' ' ')"
  exit 0
fi
if [ "${1:-}" = "--from-env" ]; then
  ENVF=${2:?usage: $0 --from-env FILE}
  [ -r "$ENVF" ] || { echo "cannot read $ENVF" >&2; exit 1; }
  for v in WE_PROD_KEY WE_ATLASSIAN_URL; do
    grep -qE "^$v=.+" "$ENVF" || { echo "$ENVF: $v is missing or empty" >&2; exit 1; }
  done
  install -d -m 750 /etc/claude-agent
  if [ -d "$T" ]; then find "${T:?}" -mindepth 1 -delete; fi
  install -d -m 700 "$T" "$T/bin"
  install -m 755 /usr/local/share/claude-agent/kit/bin/mcp-jira.sh "$T/bin/mcp-jira.sh"
  grep -E '^(WE_PROD_KEY|WE_ATLASSIAN_URL|WE_LITELLM_HOST)=' "$ENVF" >"$T/wheelseye-secrets.env"
  chmod 600 "$T/wheelseye-secrets.env"
  printf '{"mcpServers":{"wheelseye-jira-mcp":{"command":"@HOME@/.claude/bin/mcp-jira.sh","args":[]}}}\n' >"$T/mcp-prod.json"
  chown -R root:root "$T"; chmod -R go-rwx "$T"
  echo "template ready: Jira only, $(find "$T" -type f | wc -l) files in $T (root-only; holds the shared LiteLLM key and the Jira url)"
  exit 0
fi
case "${1:-}" in -*) echo "unknown option $1 (options: --from-env FILE, --strip-personal)" >&2; exit 64 ;; esac
SRC=${1:-/home/ubuntu}
[ -f "$SRC/.claude/mcp-prod.json" ] && [ -f "$SRC/.claude/wheelseye-secrets.env" ] \
  || { echo "nothing to copy from $SRC/.claude (need mcp-prod.json and wheelseye-secrets.env); the existing kit was NOT touched" >&2; exit 1; }
install -d -m 750 /etc/claude-agent
if [ -d "$T" ]; then find "${T:?}" -mindepth 1 -delete; fi
install -d -m 700 "$T"
for item in bin mcp-wrappers skills wheelseye-secrets.env mcp-prod.json; do
  if [ -e "$SRC/.claude/$item" ]; then cp -a "$SRC/.claude/$item" "$T/"; fi
done
jq '.mcpServers |= with_entries(select(.key == "wheelseye-jira-mcp"))' "$T/mcp-prod.json" >"$T/mcp-prod.json.new" && mv "$T/mcp-prod.json.new" "$T/mcp-prod.json"
grep -E '^(WE_PROD_KEY|WE_ATLASSIAN_URL|WE_LITELLM_HOST)=' "$T/wheelseye-secrets.env" >"$T/wheelseye-secrets.env.new"; mv "$T/wheelseye-secrets.env.new" "$T/wheelseye-secrets.env"
find "$T/bin" -type f ! -name mcp-wheelseye.sh -delete; rm -rf "$T/mcp-wrappers"
if [ -d "$T/skills/synced" ]; then find "${T:?}/skills/synced" -delete; fi   # account-synced skills come from each user's own account
find "$T" -type f \( -name '*.sh' -o -name '*.json' -o -name '*.env' -o -name '*.md' \) -exec sed -i "s#$SRC#@HOME@#g" {} +
chown -R root:root "$T"
chmod -R go-rwx "$T"
echo "template ready: $(find "$T" -type f | wc -l) files in $T (root-only; contains the shared MCP secret)"
