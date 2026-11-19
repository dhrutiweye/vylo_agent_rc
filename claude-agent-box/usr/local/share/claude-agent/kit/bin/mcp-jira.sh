#!/bin/bash
# Jira + Confluence MCP through the LiteLLM gateway (mcp-remote). Settings come from ~/.claude/wheelseye-secrets.env,
# which add-colleague.sh copies from the starter kit (mode 600) and adds this person's own Jira email and token to.
# Nothing here is a secret.
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
set -a; source "$HOME/.claude/wheelseye-secrets.env"; set +a

host="${WE_LITELLM_HOST:-litellm.prod-we.com}"
[ -n "${WE_PROD_KEY:-}" ] || { echo "WE_PROD_KEY is empty in wheelseye-secrets.env" >&2; exit 1; }
[ -n "${WE_ATLASSIAN_TOKEN:-}" ] && [ -n "${WE_ATLASSIAN_USERNAME:-}" ] || { echo "no personal Jira login for this user: ask the admin to run set-jira-token.sh" >&2; exit 1; }
[ -n "${WE_ATLASSIAN_URL:-}" ] || { echo "WE_ATLASSIAN_URL is empty in wheelseye-secrets.env" >&2; exit 1; }
base="${WE_ATLASSIAN_URL%/}"; base="${base%/wiki}"

exec npx mcp-remote "http://$host/mcp/jira_mcp" \
  --header "x-litellm-api-key:Bearer $WE_PROD_KEY" \
  --header "X-JIRA-URL:$base" \
  --header "X-JIRA-USERNAME:$WE_ATLASSIAN_USERNAME" \
  --header "X-JIRA-API-TOKEN:$WE_ATLASSIAN_TOKEN" \
  --header "X-CONFLUENCE-URL:$base/wiki" \
  --header "X-CONFLUENCE-USERNAME:$WE_ATLASSIAN_USERNAME" \
  --header "X-CONFLUENCE-API-TOKEN:$WE_ATLASSIAN_TOKEN" \
  --allow-http
