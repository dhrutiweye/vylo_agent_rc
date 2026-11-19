#!/usr/bin/env bash
# Audit log writer. One JSON line per event, metadata only (no prompt text, no tool arguments, no output)
# into /var/log/claude-agent/<linux user>.jsonl, a file that add-colleague.sh pre-creates (mode 600,
# owned by that user). If the file does not exist or is not writable this does nothing: an audit
# problem must never block the agent.
#
#   as a hook   : claude-audit.sh              (hook JSON on stdin; event from .hook_event_name)
#   from guard  : claude-audit.sh --guard SESSION TOOL DECISION
#
# Times are ISO-8601 UTC with a Z. Always exits 0.
LOG_DIR=${CLAUDE_AUDIT_DIR:-/var/log/claude-agent}
user=$(id -un)
file="$LOG_DIR/$user.jsonl"
[ -w "$file" ] || exit 0
ts=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)

# rows from a scheduled job's headless run carry "job", so they are not taken for the interactive agent
JOB=${CLAUDE_AGENT_JOB:-}
emit() {
  if [ -n "$JOB" ]; then
    jq -nc --arg ts "$ts" --arg user "$user" "$@" | jq -c --arg job "$JOB" '. + {job: $job}' >>"$file" 2>/dev/null || true
  else
    jq -nc --arg ts "$ts" --arg user "$user" "$@" >>"$file" 2>/dev/null || true
  fi
}

if [ "${1:-}" = "--guard" ]; then
  emit --arg session "${2:-}" --arg tool "${3:-}" --arg decision "${4:-}" \
    '{ts:$ts,user:$user,session:$session,event:"guard",tool:$tool,decision:$decision}'
  exit 0
fi

in=$(cat)
ev=$(jq -r '.hook_event_name // empty' <<<"$in" 2>/dev/null)
session=$(jq -r '.session_id // empty' <<<"$in" 2>/dev/null)
case "$ev" in
  SessionStart)
    emit --arg session "$session" --arg src "$(jq -r '.source // empty' <<<"$in")" \
      '{ts:$ts,user:$user,session:$session,event:"session_start",source:$src}' ;;
  SessionEnd)
    emit --arg session "$session" --arg why "$(jq -r '.reason // empty' <<<"$in")" \
      '{ts:$ts,user:$user,session:$session,event:"session_end",reason:$why}' ;;
  UserPromptSubmit)
    emit --arg session "$session" --argjson chars "$(jq -r '(.prompt // "") | length' <<<"$in" 2>/dev/null || echo 0)" \
      '{ts:$ts,user:$user,session:$session,event:"prompt",chars:$chars}' ;;
  PostToolUse)
    tool=$(jq -r '.tool_name // empty' <<<"$in")
    pr=""
    if [ "$tool" = Bash ] && [[ "$(jq -r '.tool_input.command // empty' <<<"$in")" == *"gh pr create"* ]]; then
      pr=$(jq -r '.tool_response | tostring' <<<"$in" | grep -oE 'https://[^ "\\]+/pull/[0-9]+' | head -1)
    fi
    emit --arg session "$session" --arg tool "$tool" --arg pr "$pr" \
      '{ts:$ts,user:$user,session:$session,event:"tool",tool:$tool} + (if $pr != "" then {pr_url:$pr} else {} end)' ;;
  Notification)
    nt=$(jq -r '.notification_type // empty' <<<"$in")
    if [ -z "$nt" ]; then
      msg=$(jq -r '.message // empty' <<<"$in" | tr 'A-Z' 'a-z')
      case "$msg" in *permission*|*approve*) nt=permission_prompt ;; *waiting*|*idle*) nt=idle_prompt ;; *) nt=other ;; esac
    fi
    emit --arg session "$session" --arg nt "$nt" \
      '{ts:$ts,user:$user,session:$session,event:"notification",ntype:$nt}' ;;
  Stop)
    emit --arg session "$session" '{ts:$ts,user:$user,session:$session,event:"stop"}' ;;
esac
exit 0
