#!/usr/bin/env bash
# Check this box end to end. Prints PASS / FAIL / WARN per item and exits non-zero if anything FAILs.
#
#   sudo ./verify-box.sh            local checks (no network calls to Google or GitHub)
#   sudo ./verify-box.sh --online   also pull-check the Pub/Sub subscription and the GitHub bot login
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd); ONLINE=0; [ "${1:-}" = "--online" ] && ONLINE=1
fails=0
ok()   { printf 'PASS  %s\n' "$*"; }
bad()  { printf 'FAIL  %s\n' "$*"; fails=$((fails+1)); }
warn() { printf 'WARN  %s\n' "$*"; }
chk()  { local what=$1; shift; if "$@" >/dev/null 2>&1; then ok "$what"; else bad "$what"; fi; }
ENV=/etc/claude-agent/bridge.env
val() { sed -nE "s/^$1=(.*)$/\1/p" "$ENV" 2>/dev/null | head -1; }

echo "-- tools"
for t in git tmux jq make docker node npx gh python3; do chk "$t installed" command -v $t; done
node -v | grep -q '^v22' && ok "node 22" || bad "node is not v22"
chk "docker daemon answers" docker info
chk "docker can run a container" docker run --rm hello-world
chk "bridge libraries import" /usr/local/lib/claude-agent/venv/bin/python -c 'import google.cloud.pubsub_v1, google.auth, requests'

echo "-- platform files"
for f in /usr/local/bin/{agent-start,agent-run,claude-guard.sh,claude-audit.sh,add-colleague.sh,remove-colleague.sh,vylo-mirror-sync.sh,claude-agent-seed-template.sh,claude-agent-report,gh-token-check.sh,set-gh-token.sh,jira-token-check.sh,set-jira-token.sh,notify-me,agent-job,agent-job-exec,claude-agent-jobs,agent-max-sessions,sessions-enable,claude-agent-monitor} \
         /usr/local/lib/claude-agent/{chat_bridge.py,agent_outbox.py,agent_jobs.py,agent_sessions.py} /etc/claude-code/{managed-settings.json,mcp-allow.txt,CLAUDE.md,agent-jobs.env}; do
  [ -e "$f" ] && ok "$f" || bad "missing $f"
done
chk "managed settings are valid JSON" jq -e . /etc/claude-code/managed-settings.json
S=/etc/claude-code/agent-sessions.conf
[ -f $S ] && [ "$(stat -c %a $S)" = 644 ] && [ "$(stat -c %U $S)" = root ] && ok "$S root-owned, 644" || bad "$S must exist, root-owned, mode 644"
n=$(runuser -u nobody -- /usr/local/bin/agent-max-sessions 2>/dev/null); [[ $n =~ ^[1-5]$ ]] && ok "agent-max-sessions runs (nobody: $n)" || bad "agent-max-sessions does not run"
for u in $(jq -r '.people[] | select(.status=="active") | .linux_user' /etc/claude-agent/people.json 2>/dev/null); do
  if python3 -I -c 'import sys; sys.path.insert(0, "/usr/local/lib/claude-agent"); import agent_sessions as s, pwd
p = pwd.getpwnam(sys.argv[1]); sys.exit(0 if s.org_policy_ok(p.pw_dir, p.pw_uid) else 1)' "$u" 2>/dev/null; then
    ok "$u: the box's guard and audit hooks run"
  else
    warn "$u: the box's guard does NOT run (org policy without merge): sudo lock-box-rules $u"
  fi
done
[ "$(systemctl show -p LoadState --value claudeagents.slice)" = loaded ] && ok "claudeagents.slice loaded" || bad "claudeagents.slice is not loaded (sudo systemctl daemon-reload)"
cg=$(systemctl show -p ControlGroup --value claudeagents.slice 2>/dev/null)
[ "$cg" = /claudeagents.slice ] && [ -f "/sys/fs/cgroup$cg/memory.events" ] && ok "the monitor's cgroup path exists ($cg)" \
  || bad "claudeagents.slice is at '${cg:-nowhere}', not /sys/fs/cgroup/claudeagents.slice: the monitor would see no one (is an agent running in it?)"
grep -qx jira /etc/claude-code/mcp-allow.txt && ok "MCP allow-list names jira" || bad "mcp-allow.txt does not list jira"
chk "watchdog timer enabled" systemctl is-enabled claude-agent-watch-all.timer
chk "mirror-sync timer enabled" systemctl is-enabled vylo-mirror-sync.timer
chk "load monitor timer enabled" systemctl is-enabled claude-agent-monitor.timer
grep -qE '^ADMIN_PERSON=[a-z_]' /etc/claude-agent/bridge.env && ok "box alerts go to ADMIN_PERSON" || warn "ADMIN_PERSON is empty in bridge.env: box alerts are logged, not sent"
chk "jobs runner enabled" systemctl is-enabled claude-agent-jobs.service
chk "per-user limits present" test -f "/etc/systemd/system/claude-agent@.service.d/limits.conf"
chk "git rewrites ssh urls to https" bash -c 'git config --system --get url.https://github.com/.insteadOf | grep -q git@github.com:'

echo "-- policy and bridge tests (the same ones used to build it)"
if [ -d "$HERE/claude-agent-box/tests" ]; then
  out=$(bash "$HERE/claude-agent-box/tests/test_guard.sh" /usr/local/bin/claude-guard.sh 2>&1 | tail -1); echo "$out" | grep -q ' 0 failed' && ok "guard policy: $out" || bad "guard policy: $out"
  out=$(python3 -I -B -m unittest discover -s "$HERE/claude-agent-box/tests" -p 'test_chat_bridge.py' 2>&1 | tail -1); [ "$out" = OK ] && ok "bridge tests" || bad "bridge tests: $out"
  out=$(python3 -I -B -m unittest discover -s "$HERE/claude-agent-box/tests" -p 'test_jobs.py' 2>&1 | tail -1); [ "$out" = OK ] && ok "doorbell and job tests" || bad "doorbell and job tests: $out"
else warn "tests folder not found next to this script"; fi

echo "-- configuration"
[ -n "$(val PROJECT_ID)" ] && ok "PROJECT_ID set" || bad "PROJECT_ID empty (run configure-box.sh)"
[ -n "$(val SUBSCRIPTION_ID)" ] && ok "SUBSCRIPTION_ID set" || bad "SUBSCRIPTION_ID empty (run configure-box.sh)"
K=$(val GOOGLE_APPLICATION_CREDENTIALS)
if [ -n "$K" ] && [ -f "$K" ]; then
  [ "$(stat -c %a "$K")" = 600 ] && [ "$(stat -c %U "$K")" = root ] && ok "chat key present, root-only (600)" || bad "chat key must be root-owned, mode 600"
else bad "chat key missing (run configure-box.sh)"; fi
T=/etc/claude-agent/template
if [ -f $T/mcp-prod.json ]; then
  [ "$(jq -r '.mcpServers|keys|join(",")' $T/mcp-prod.json)" = wheelseye-jira-mcp ] && ok "starter kit is Jira only" || bad "starter kit has servers other than Jira"
  [ "$(stat -c %a $T/wheelseye-secrets.env)" = 600 ] && ok "kit secrets are mode 600" || bad "kit secrets must be mode 600"
  if grep -qE '^WE_ATLASSIAN_(USERNAME|TOKEN)=' $T/wheelseye-secrets.env; then bad "the kit still holds a shared Jira user/token (run: sudo claude-agent-seed-template.sh --strip-personal)"; else ok "kit holds no personal Jira login"; fi
else bad "no Jira starter kit (run configure-box.sh)"; fi
[ -f /etc/claude-agent/gh-hosts.yml ] && ok "GitHub mirror login saved" || bad "no GitHub mirror login (run github-bot-login.sh)"
n=$(ls -d /srv/vylo/mirrors/*.git 2>/dev/null | wc -l); [ "$n" -gt 0 ] && ok "$n repo mirrors" || bad "no repo mirrors (run vylo-mirror-sync.sh)"
for u in $(jq -r '.people[].linux_user' /etc/claude-agent/people.json 2>/dev/null); do
  [ -d "/home/$u" ] || continue
  if grep -qE '^WE_ATLASSIAN_TOKEN=.+' "/home/$u/.claude/wheelseye-secrets.env" 2>/dev/null; then ok "$u has a personal Jira token"; else warn "$u has no personal Jira token (run: sudo set-jira-token.sh $u)"; fi
  if [ -s "/home/$u/.config/gh/hosts.yml" ]; then ok "$u has a personal GitHub token"; else warn "$u has no personal GitHub token (run: sudo set-gh-token.sh $u)"; fi
done
[ -s /srv/vylo/context.md ] && ok "context.md in place" || warn "no /srv/vylo/context.md"
chk "audit log directory" test -d /var/log/claude-agent

echo "-- bridge service"
systemctl is-active --quiet claude-agent-bridge && ok "bridge is running" || warn "bridge is not running (start it with: sudo systemctl enable --now claude-agent-bridge)"
systemctl is-active --quiet claude-agent-jobs && ok "jobs runner is running" || bad "jobs runner is not running (sudo systemctl restart claude-agent-jobs; journalctl -u claude-agent-jobs)"

if [ $ONLINE = 1 ]; then
  echo "-- online"
  P=$(val PROJECT_ID); S=$(val SUBSCRIPTION_ID)
  if GOOGLE_APPLICATION_CREDENTIALS="$K" /usr/local/lib/claude-agent/venv/bin/python - "$P" "$S" >/tmp/.vb.$$ 2>&1 <<'PY'
import sys
from google.cloud import pubsub_v1
p, s = sys.argv[1:3]
sub = pubsub_v1.SubscriberClient()
path = sub.subscription_path(p, s)
got = sub.test_iam_permissions(request={"resource": path, "permissions": ["pubsub.subscriptions.consume"]}).permissions
if "pubsub.subscriptions.consume" not in got:
    sys.exit("the bot account cannot consume " + path)
print("subscription reachable, bot can consume:", s)
PY
  then ok "$(cat /tmp/.vb.$$)"; else bad "Pub/Sub: $(grep -vE '^[][{}]|^ *(key|value|metadata)' /tmp/.vb.$$ | tail -2 | tr '\n' ' ') (is the Subscriber role granted to the bot?)"; fi; rm -f /tmp/.vb.$$
  if [ -f /etc/claude-agent/gh-hosts.yml ]; then
    G=$(mktemp -d); install -m 600 /etc/claude-agent/gh-hosts.yml "$G/hosts.yml"
    who=$(GH_CONFIG_DIR=$G gh api user --jq .login 2>/dev/null); rm -rf "$G"
    [ -n "$who" ] && ok "GitHub mirror login works as '$who'" || bad "GitHub mirror login does not work"
  fi
  h=$(sed -nE 's/^WE_LITELLM_HOST=(.*)$/\1/p' $T/wheelseye-secrets.env 2>/dev/null); h=${h:-litellm.prod-we.com}
  curl -s -m 8 -o /dev/null -w '%{http_code}' "http://$h/" | grep -qE '^[1-5][0-9][0-9]$' && ok "LiteLLM gateway $h reachable" || bad "cannot reach the LiteLLM gateway $h (VPN/route?)"
fi
echo; [ $fails = 0 ] && echo "ALL CHECKS PASSED" || echo "$fails check(s) failed"
exit $((fails>0))
