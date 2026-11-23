#!/usr/bin/env bash
# lock-box-rules against throwaway users and homes. Needs root (it creates users), so it runs in the test container;
# anywhere else it says so and passes. The immutable flag is not supported there (overlayfs): LOCK_NO_IMMUTABLE=1,
# honoured only by a copy that is not the installed one, skips just that step.
# Usage: test_lock_rules.sh [bundle dir]   (default: this bundle)
B=${1:-$(cd "$(dirname "$0")/.." && pwd)}/usr/local/bin
[ "$(id -u)" = 0 ] && command -v useradd >/dev/null || { echo "skipped: needs root and useradd (run in the container)"; exit 0; }
T=$(mktemp -d); trap 'rm -rf "$T"; for u in lrann lrbob lrcid; do userdel "$u" 2>/dev/null; done' EXIT
pass=0; fail=0
ok() { if [ "$1" = 0 ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL $2"; fi; }
export VYLO_HOME_ROOT=$T/home MANAGED_SETTINGS=$T/managed.json LOCK_NO_IMMUTABLE=1
mkdir -p "$T/home"; chmod 755 "$T" "$T/home"
for u in lrann lrbob lrcid; do
  useradd -M -d "$T/home/$u" "$u"; mkdir -p "$T/home/$u/.claude"; chown -R "$u:$u" "$T/home/$u"; chmod 700 "$T/home/$u"
done
cat >"$MANAGED_SETTINGS" <<'EOF'
{"env": {"X": "1"}, "permissions": {"defaultMode": "acceptEdits"}, "statusLine": {"type": "command"},
 "hooks": {"PreToolUse": [{"matcher": "Bash|Edit", "hooks": [{"type": "command", "command": "/usr/local/bin/claude-guard.sh"}]}]}}
EOF
F=$T/home/lrann/.claude/settings.json
run() { out=$("$B/lock-box-rules" "$@" 2>&1); rc=$?; }

echo "== install"
run lrann
[ $rc = 0 ]; ok $? "lrann: exit 0 ($out)"
[ "$(stat -c '%U %a' "$F")" = "root 644" ]; ok $? "owned by root, 644 ($(stat -c '%U %a' "$F"))"
jq -e '.hooks.PreToolUse[0].hooks[0].command == "/usr/local/bin/claude-guard.sh" and .env.X == "1" and .permissions.defaultMode == "acceptEdits"' "$F" >/dev/null
ok $? "hooks, env and permissions copied"
! jq -e 'has("statusLine")' "$F" >/dev/null; ok $? "nothing but env, hooks and permissions"
[ "$(stat -c %U "$T/home/lrann/.claude")" = lrann ]; ok $? "~/.claude stays the person's"
! ls "$T/home/lrann/.claude" | grep -q tmp; ok $? "no temp file left"

echo "== refusals"
echo '{"theme":"dark"}' >"$T/home/lrbob/.claude/settings.json"; chown lrbob:lrbob "$T/home/lrbob/.claude/settings.json"
run lrbob
[ $rc != 0 ] && [[ $out == *"own settings"* ]]; ok $? "lrbob has own settings: refused ($out)"
[ "$(cat "$T/home/lrbob/.claude/settings.json")" = '{"theme":"dark"}' ]; ok $? "lrbob's file untouched"
mkdir -p "$T/elsewhere"; rm -rf "$T/home/lrcid/.claude"; ln -s "$T/elsewhere" "$T/home/lrcid/.claude"
run lrcid
[ $rc != 0 ] && [ ! -e "$T/elsewhere/settings.json" ]; ok $? "symlinked ~/.claude: refused, nothing written through it"
run nobody-here
[ $rc != 0 ]; ok $? "unknown person: refused"

echo "== refresh after the box rules change"
jq '.env.X = "2"' "$MANAGED_SETTINGS" >"$T/m2" && mv "$T/m2" "$MANAGED_SETTINGS"
run --refresh-all
[ $rc = 0 ] && [ "$(jq -r .env.X "$F")" = 2 ]; ok $? "--refresh-all rewrites lrann's copy ($out)"
[ "$(cat "$T/home/lrbob/.claude/settings.json")" = '{"theme":"dark"}' ]; ok $? "--refresh-all leaves a person's own file alone"

echo "== unlock"
run --unlock lrann
[ $rc = 0 ] && [ ! -e "$F" ]; ok $? "--unlock removes it ($out)"
run --unlock lrbob
[ $rc != 0 ] && [ -f "$T/home/lrbob/.claude/settings.json" ]; ok $? "--unlock never removes a person's own file"

echo "== the installed copy ignores the test switches"
grep -q 'INSTALLED' "$B/lock-box-rules"; ok $? "test switches are tied to the installed path"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" = 0 ]
