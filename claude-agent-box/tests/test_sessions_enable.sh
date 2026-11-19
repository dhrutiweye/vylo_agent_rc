#!/usr/bin/env bash
# sessions-enable against throwaway homes and a temp conf file; no systemctl, no runuser. Temp dir only.
# Usage: test_sessions_enable.sh [bundle dir]   (default: this bundle)
B=${1:-$(cd "$(dirname "$0")/.." && pwd)}/usr/local/bin
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok() { if [ "$1" = 0 ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL $2"; fi; }
export AGENT_SESSIONS_CONF=$T/agent-sessions.conf VYLO_HOME_ROOT=$T/home SESSIONS_NO_SYSTEMCTL=1 SESSIONS_LOCK=$T/sessions.lock
g() { git -C "$1" -c user.email=t@t -c user.name=t "${@:2}"; }

setup() {
  rm -rf "$T/home"; printf 'default=1\nbob=5\n' >"$AGENT_SESSIONS_CONF"
  V=$T/home/ann/Documents/code/vylo; mkdir -p "$V" "$T/home/ann/.claude"
  for r in r1 r2; do
    git init -q "$V/$r" && g "$V/$r" commit -q --allow-empty -m i && g "$V/$r" checkout -q -B master
    g "$V/$r" remote add origin "$V/$r" && g "$V/$r" fetch -q origin && g "$V/$r" remote set-head origin master >/dev/null
  done
  g "$V/r1" worktree add -q "$V/.worktrees/r1-feat" -b feat               # worktrees are not base copies
  echo wip >"$V/.worktrees/r1-feat/wip.txt"
  mkdir -p "$V/notes"                                                      # not a repo
}
run() { out=$("$B/sessions-enable" "$@" 2>&1); rc=$?; }

echo "== clean copies on the default branch"
setup; run --yes ann
[ $rc = 0 ]; ok $? "clean: exit 0 ($out)"
grep -qx 'ann=5' "$AGENT_SESSIONS_CONF"; ok $? "clean: ann=5 written"
grep -qx 'bob=5' "$AGENT_SESSIONS_CONF" && grep -qx 'default=1' "$AGENT_SESSIONS_CONF"; ok $? "other lines kept"
run --yes ann
[ "$(grep -c '^ann=' "$AGENT_SESSIONS_CONF")" = 1 ]; ok $? "twice: one ann= line"
run --yes ann 3
grep -qx 'ann=3' "$AGENT_SESSIONS_CONF" && [ "$(grep -c '^ann=' "$AGENT_SESSIONS_CONF")" = 1 ]; ok $? "a number replaces the line"
[ "$(stat -c %a "$AGENT_SESSIONS_CONF" 2>/dev/null || stat -f %Lp "$AGENT_SESSIONS_CONF")" = 644 ]; ok $? "conf stays 644"

echo "== refusals leave the conf unchanged"
setup; g "$V/r2" checkout -q -b feat2; before=$(cat "$AGENT_SESSIONS_CONF"); run --yes ann
[ $rc = 1 ] && [[ $out == *r2* && $out == *feat2* ]]; ok $? "r2 on a branch: refused, names r2 and feat2 ($out)"
[ "$(cat "$AGENT_SESSIONS_CONF")" = "$before" ]; ok $? "r2 on a branch: conf unchanged"

setup; echo x >"$V/r1/new.txt"; before=$(cat "$AGENT_SESSIONS_CONF"); run --yes ann
[ $rc = 1 ] && [[ $out == *r1* ]] && [[ $out != *r2* ]]; ok $? "r1 uncommitted: refused, names r1 only ($out)"
[ "$(cat "$AGENT_SESSIONS_CONF")" = "$before" ]; ok $? "r1 uncommitted: conf unchanged"
[ -f "$V/r1/new.txt" ]; ok $? "r1 uncommitted: the file is untouched"

setup; echo '{"permissions":{"disableBypassPermissionsMode":"disable"}}' >"$T/home/ann/.claude/remote-settings.json"
before=$(cat "$AGENT_SESSIONS_CONF"); run --yes ann
[ $rc = 1 ] && [[ $out == *"must allow merging"* ]]; ok $? "org policy without merge: refused ($out)"
[ "$(cat "$AGENT_SESSIONS_CONF")" = "$before" ]; ok $? "org policy: conf unchanged"
echo '{"managedSourcesBehavior":"merge"}' >"$T/home/ann/.claude/remote-settings.json"; run --yes ann
[ $rc = 0 ]; ok $? "org policy with merge: allowed ($out)"

setup; run --yes nobody
[ $rc != 0 ]; ok $? "unknown person: refused"
run --yes ann 9
[ $rc != 0 ] && ! grep -q '^ann=' "$AGENT_SESSIONS_CONF"; ok $? "N outside 1..5: refused"
run --yes '../x'
[ $rc != 0 ]; ok $? "bad name: refused"

echo "== --off"
setup; run --yes ann; run --yes --off ann
[ $rc = 0 ] && ! grep -q '^ann=' "$AGENT_SESSIONS_CONF" && grep -qx 'bob=5' "$AGENT_SESSIONS_CONF"; ok $? "--off removes ann= and keeps the rest ($out)"
g "$V/r2" checkout -q -b dirty-branch; run --yes --off ann
[ $rc = 0 ]; ok $? "--off needs no clean copies"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" = 0 ]
