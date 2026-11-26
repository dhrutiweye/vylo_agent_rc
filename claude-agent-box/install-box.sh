#!/usr/bin/env bash
# Install the claude-agent box tooling onto this host (idempotent). Run as root from the unpacked bundle.
#
#   sudo ./install-box.sh [--no-venv]
#
# Installs: agent-start/agent-run, the session limit (agent-max-sessions, sessions-enable, claudeagents.slice), the policy hook (claude-guard.sh) and managed settings, the audit hook
# and report, add/remove-colleague, mirror/template helpers, the Chat bridge, and the systemd units.
# Files it would overwrite are copied to /var/lib/claude-agent/backup/<time>/ first. It never starts or
# restarts claude-agent.service, and it does not start the Chat bridge: both are left to the admin.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
VENV=1; [ "${1:-}" = "--no-venv" ] && VENV=0
TS=$(date -u +%Y%m%dT%H%M%SZ); BK=/var/lib/claude-agent/backup/$TS
install -d -m 700 /var/lib/claude-agent
install -d -m 750 /etc/claude-agent
install -d -m 711 /var/log/claude-agent

put() {  # put <mode> <relative path in bundle>   (destination = / + relative path)
  local mode=$1 rel=$2 dst=/$2
  if [ -f "$dst" ] && ! cmp -s "$HERE/$rel" "$dst"; then
    install -d "$BK$(dirname "$dst")"; cp -p "$dst" "$BK$dst"
  fi
  install -D -m "$mode" -o root -g root "$HERE/$rel" "$dst"
}

# scripts first, the policy files (what the live agent reads) last
for f in agent-start agent-run claude-agent-watch-all claude-audit.sh claude-agent-report add-colleague.sh \
         remove-colleague.sh vylo-mirror-refresh.sh vylo-mirror-sync.sh gh-token-check.sh set-gh-token.sh jira-token-check.sh set-jira-token.sh set-litellm-key.sh claude-agent-seed-template.sh \
         notify-me agent-job agent-job-exec claude-agent-jobs agent-max-sessions sessions-enable claude-agent-monitor lock-box-rules; do put 755 "usr/local/bin/$f"; done
put 644 usr/local/lib/claude-agent/chat_bridge.py
put 644 usr/local/lib/claude-agent/agent_outbox.py
put 644 usr/local/lib/claude-agent/agent_jobs.py
put 644 usr/local/lib/claude-agent/agent_sessions.py
put 755 usr/local/share/claude-agent/kit/bin/mcp-jira.sh
for u in claude-agent.service claude-agent@.service claude-agent-watch.service claude-agent-watch.timer \
         claude-agent-watch-all.service claude-agent-watch-all.timer claude-agent-bridge.service \
         vylo-mirror-sync.service vylo-mirror-sync.timer claude-agent-jobs.service claudeagents.slice claude-agent-monitor.service claude-agent-monitor.timer; do put 644 "etc/systemd/system/$u"; done
put 644 "etc/systemd/system/claude-agent@.service.d/limits.conf"   # per-colleague CPU, memory and task caps
put 644 etc/needrestart/conf.d/50-claude-agent.conf
if [ ! -f /etc/claude-agent/bridge.env ]; then install -m 640 -o root -g root "$HERE/etc/claude-agent/bridge.env.example" /etc/claude-agent/bridge.env; fi
put 644 etc/claude-agent/bridge.env.example
install -d -m 711 /var/lib/claude-agent-jobs     # runner state, one file per colleague (readable by that colleague)
put 755 usr/local/bin/claude-guard.sh
put 644 etc/claude-code/managed-settings.json
/usr/local/bin/lock-box-rules --refresh-all || true   # org-plan colleagues' locked copies of the box rules follow it
if [ ! -f /etc/claude-code/agent-jobs.env ]; then put 644 etc/claude-code/agent-jobs.env; fi   # job limits: kept if you changed them
# sessions per person: never overwritten (sessions-enable edits it); readable by agents, the guard and agent-run
if [ ! -f /etc/claude-code/agent-sessions.conf ]; then install -D -m 644 -o root -g root "$HERE/etc/claude-code/agent-sessions.conf" /etc/claude-code/agent-sessions.conf; fi
put 644 etc/claude-code/CLAUDE.md            # instructions every agent on the box reads: notify-me, agent-job
put 644 etc/claude-code/mcp-allow.txt        # MCP servers colleagues' agents may use (substrings): jira, github
put 644 etc/claude-code/mcp-all-users.txt    # admin users, unrestricted: ubuntu

# audit log file for the original agent user, so its hooks have somewhere to write
if id ubuntu >/dev/null 2>&1 && [ ! -f /var/log/claude-agent/ubuntu.jsonl ]; then
  install -m 600 -o ubuntu -g ubuntu /dev/null /var/log/claude-agent/ubuntu.jsonl
fi

if [ "$VENV" = 1 ] && [ ! -x /usr/local/lib/claude-agent/venv/bin/python ]; then
  ${PYTHON:-python3} -m venv /usr/local/lib/claude-agent/venv
  /usr/local/lib/claude-agent/venv/bin/pip install -q google-cloud-pubsub google-auth requests
fi

systemctl daemon-reload
systemctl enable --now claude-agent-watch-all.timer >/dev/null 2>&1 || true
systemctl enable --now vylo-mirror-sync.timer >/dev/null 2>&1 || true
systemctl enable --now claude-agent-monitor.timer >/dev/null 2>&1 || true   # box load every 5 min -> /var/log/claude-agent-load.jsonl
systemctl enable claude-agent-jobs.service >/dev/null 2>&1 || true
systemctl restart claude-agent-jobs.service >/dev/null 2>&1 || true   # picks up a new runner; running task units are not touched
[ -d "$BK" ] && echo "previous versions saved in $BK"
echo "installed. Not started: claude-agent-bridge.service (configure /etc/claude-agent/bridge.env first)."
