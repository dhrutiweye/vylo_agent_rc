#!/usr/bin/env bash
# Static checks on the bundle's limits, units, installer and agent instructions. Reads files only.
# Usage: test_install_files.sh [bundle dir]   (default: this bundle)
R=${1:-$(cd "$(dirname "$0")/.." && pwd)}
pass=0; fail=0
ok() { if [ "$1" = 0 ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL $2"; fi; }
L=$R/etc/systemd/system/claude-agent@.service.d/limits.conf
S=$R/etc/systemd/system/claudeagents.slice
I=$R/install-box.sh
C=$R/etc/claude-code/CLAUDE.md

echo "== memory: 4 GB per person, 12 GB for all agents and jobs together"
grep -qx 'MemoryHigh=3584M' "$L"; ok $? "limits.conf MemoryHigh=3584M"
grep -qx 'MemoryMax=4G' "$L"; ok $? "limits.conf MemoryMax=4G"
grep -qx 'CPUQuota=150%' "$L"; ok $? "limits.conf CPUQuota unchanged"
grep -qx 'MemoryMax=12G' "$S"; ok $? "slice MemoryMax=12G"
case "$(basename "$S" .slice)" in *-*) false ;; *) true ;; esac; ok $? "slice name has no dash (a dash nests it in another slice)"
grep -qx 'Slice=claudeagents.slice' "$R/etc/systemd/system/claude-agent@.service"; ok $? "claude-agent@ runs in the slice"

echo "== installer"
for f in usr/local/lib/claude-agent/agent_sessions.py usr/local/bin/agent-max-sessions usr/local/bin/sessions-enable \
         etc/systemd/system/claudeagents.slice usr/local/bin/claude-agent-monitor \
         etc/systemd/system/claude-agent-monitor.service etc/systemd/system/claude-agent-monitor.timer \
         usr/local/bin/lock-box-rules usr/local/bin/set-litellm-key.sh usr/local/bin/agent-chat-exec \
         usr/local/lib/claude-agent/agent_secrets.py usr/local/lib/claude-agent/chat_state.py; do
  grep -q "$(basename "$f")" "$I"; ok $? "install-box.sh installs $f"
done
grep -q 'agent-sessions.conf' "$I"; ok $? "install-box.sh installs agent-sessions.conf"
grep -qE 'install -d -m 700 /var/lib/claude-agent-chat' "$I"; ok $? "install-box.sh creates /var/lib/claude-agent-chat (700)"
grep -E 'agent-sessions\.conf' "$I" | grep -q '\[ ! -f /etc/claude-code/agent-sessions.conf \]'; ok $? "agent-sessions.conf is installed only when missing"
grep -E 'install .*agent-sessions\.conf' "$I" | grep -q -- '-m 644'; ok $? "agent-sessions.conf is mode 644"

grep -q 'enable --now claude-agent-monitor.timer' "$I"; ok $? "install-box.sh starts the monitor timer"
grep -q 'lock-box-rules --refresh-all' "$I"; ok $? "install-box.sh refreshes locked copies of the box rules"
[ "$(grep -n 'lock-box-rules --refresh-all' "$I" | cut -d: -f1)" -gt "$(grep -n 'put 644 etc/claude-code/managed-settings.json' "$I" | cut -d: -f1)" ]
ok $? "the refresh runs after the new managed-settings.json is in place"
grep -qx 'OnUnitActiveSec=5min' "$R/etc/systemd/system/claude-agent-monitor.timer"; ok $? "monitor every 5 minutes"
grep -q '^ADMIN_PERSON=' "$R/etc/claude-agent/bridge.env.example"; ok $? "bridge.env.example has ADMIN_PERSON"

echo "== what every agent reads"
grep -q '\.worktrees' "$C"; ok $? "CLAUDE.md: work in .worktrees"
grep -q 'at most 2 helpers' "$C"; ok $? "CLAUDE.md: at most 2 helpers"
grep -q 'set_session_title' "$C" && grep -q 'AGENT_NAME' "$C"; ok $? "CLAUDE.md: sessions are titled with the agent name"
grep -q 'worktree remove' "$C"; ok $? "CLAUDE.md: remove the worktree when done"
grep -q 'If `agent-max-sessions` prints more than 1' "$C"; ok $? "CLAUDE.md: the worktree rule is only for several-session people"
jq -e '.hooks.PreToolUse[0].matcher | test("MultiEdit")' "$R/etc/claude-code/managed-settings.json" >/dev/null; ok $? "guard sees MultiEdit"
jq -e '.hooks.PreToolUse[0].matcher | test("Edit") and test("Write") and test("NotebookEdit")' \
  "$R/etc/claude-code/managed-settings.json" >/dev/null; ok $? "guard sees Edit/Write/NotebookEdit"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" = 0 ]
