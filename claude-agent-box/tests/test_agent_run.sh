#!/usr/bin/env bash
# Runs agent-run once against a fake `claude` and a fake `agent-max-sessions`, and checks which claude commands
# it tries. agent-run puts $HOME/.local/bin first on PATH, so the fakes live there. Temp dir only.
# Usage: test_agent_run.sh [bundle dir]   (default: this bundle)
B=${1:-$(cd "$(dirname "$0")/.." && pwd)}/usr/local/bin
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok() { if [ "$1" = 0 ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL $2"; fi; }

setup() {   # fresh home with the two fakes
  rm -rf "$T/home"; mkdir -p "$T/home/.local/bin" "$T/home/.claude"
  cat >"$T/home/.local/bin/claude" <<EOF
#!/bin/bash
echo "\$*" >>"$T/calls"
exit 1
EOF
  mkdir -p "$T/bin"
  cat >"$T/bin/agent-max-sessions" <<'EOF'
#!/bin/bash
echo "$FAKE_N"
EOF
  printf '#!/bin/bash\necho 5\n' >"$T/home/.local/bin/agent-max-sessions"   # a copy in the user's own PATH: never used
  chmod +x "$T/home/.local/bin/"* "$T/bin/agent-max-sessions"
  : >"$T/calls"
}
run() { AGENT_MAX_SESSIONS_BIN=$T/bin/agent-max-sessions HOME=$T/home AGENT_RUN_ONCE=1 AGENT_NAME=x-agent AGENT_WORKDIR=$T/work FAKE_N=$1 timeout 20 bash "$B/agent-run" >/dev/null 2>&1; }
calls() { cat "$T/calls"; }
mixed() { grep -E -- '--capacity' "$T/calls" | grep -qE -- '--session-id|--continue'; }

echo "== several sessions"
setup; run 5
[ "$(calls)" = "remote-control --name x-agent --capacity 5 --spawn same-dir" ]; ok $? "N=5: one server-mode call ($(calls | tr '\n' '|'))"

setup; echo abc >"$T/home/.claude/agent-session-id"; run 5
[ "$(calls)" = "remote-control --name x-agent --capacity 5 --spawn same-dir" ]; ok $? "N=5 with a saved id: same call, no --session-id ($(calls | tr '\n' '|'))"
! mixed; ok $? "N=5: --capacity never with --session-id or --continue"

echo "== one session: today's chain"
setup; echo abc >"$T/home/.claude/agent-session-id"; run 1
expected=$'remote-control --name x-agent --session-id abc\nremote-control --name x-agent --continue\nremote-control --name x-agent --capacity 1'
[ "$(calls)" = "$expected" ]; ok $? "N=1 with a saved id: reattach, continue, fresh ($(calls | tr '\n' '|'))"
! mixed; ok $? "N=1: --capacity never with --session-id or --continue"

setup; touch "$T/home/.claude/agent-fresh"; run 1
[ "$(calls)" = "remote-control --name x-agent --capacity 1" ]; ok $? "N=1 fresh: one --capacity 1 call ($(calls | tr '\n' '|'))"

echo "== the admin lowers 5 to 1 while sessions are open (saved id from the multi-session run)"
setup; echo abc >"$T/home/.claude/agent-session-id"; run 5; : >"$T/calls"; run 1
[ "$(head -1 "$T/calls")" = "remote-control --name x-agent --session-id abc" ]; ok $? "5 -> 1: next start reattaches the saved id"
! mixed; ok $? "5 -> 1: --capacity never with --session-id or --continue"

echo "== the helper is the box's, and only 1..5 counts"
setup; run 1
[ "$(calls | tail -1)" = "remote-control --name x-agent --capacity 1" ]; ok $? "a helper in ~/.local/bin is never asked ($(calls | tr '\n' '|'))"
setup; run 9
[ "$(calls | tail -1)" = "remote-control --name x-agent --capacity 1" ] && ! grep -q -- '--capacity 9' "$T/calls"; ok $? "an answer of 9 means one session"
grep -q 'AGENT_MAX_SESSIONS_BIN' "$B/agent-run" && grep -q '/usr/local/bin/agent-run' "$B/agent-run"; ok $? "the helper override is for a test copy only"

echo "== a broken helper means one session"
setup; run ''
[ "$(calls | tail -1)" = "remote-control --name x-agent --capacity 1" ]; ok $? "empty answer from agent-max-sessions: single-session chain"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" = 0 ]
