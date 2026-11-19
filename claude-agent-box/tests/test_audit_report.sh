#!/usr/bin/env bash
# Feeds hook events to claude-audit.sh, then checks the log lines and the report. Temp dir only.
# Usage: test_audit_report.sh [dir-with-usr/local/bin]   (default: this bundle)
B=${1:-$(cd "$(dirname "$0")/.." && pwd)}/usr/local/bin
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
export CLAUDE_AUDIT_DIR=$D CLAUDE_AUDIT_BIN=$B/claude-audit.sh
me=$(id -un); : >"$D/$me.jsonl"; chmod 600 "$D/$me.jsonl"
fail=0; ok() { [ "$1" = 0 ] && echo "ok   $2" || { echo "FAIL $2"; fail=1; }; }
hook() { "$B/claude-audit.sh" <<<"$1"; }

hook '{"hook_event_name":"SessionStart","session_id":"s1","source":"startup"}'
hook '{"hook_event_name":"UserPromptSubmit","session_id":"s1","prompt":"SECRET PROMPT TEXT do not log me"}'
hook '{"hook_event_name":"PostToolUse","session_id":"s1","tool_name":"Read","tool_input":{"file_path":"/etc/shadow"},"tool_response":"x"}'
hook '{"hook_event_name":"PostToolUse","session_id":"s1","tool_name":"Bash","tool_input":{"command":"gh pr create --draft --fill"},"tool_response":{"stdout":"https://github.com/WeyeTech/voiceLog/pull/701\n"}}'
hook '{"hook_event_name":"Notification","session_id":"s1","message":"Claude needs your permission to use Bash"}'
hook '{"hook_event_name":"Notification","session_id":"s1","notification_type":"idle_prompt","message":"waiting"}'
hook '{"hook_event_name":"Stop","session_id":"s1"}'
hook '{"hook_event_name":"SessionEnd","session_id":"s1","reason":"other"}'
"$B/claude-audit.sh" --guard s1 Bash deny
hook 'not json at all'; hook ''

L=$D/$me.jsonl
[ "$(wc -l <"$L")" -eq 9 ]; ok $? "9 events written, junk input ignored"
! grep -q -e 'SECRET PROMPT' -e '/etc/shadow' -e 'gh pr create' "$L"; ok $? "no prompt text, file paths or commands in the log"
jq -e 'select(.event=="prompt") | .chars == 32' "$L" >/dev/null; ok $? "prompt logged as a character count only"
jq -e 'select(.pr_url) | .pr_url == "https://github.com/WeyeTech/voiceLog/pull/701"' "$L" >/dev/null; ok $? "PR url captured from gh pr create"
jq -e 'select(.event=="notification" and .ntype=="permission_prompt")' "$L" >/dev/null; ok $? "notification classified from message"
jq -e 'select(.event=="guard" and .decision=="deny")' "$L" >/dev/null; ok $? "guard decision logged"
jq -e '.ts | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z$")' "$L" >/dev/null; ok $? "timestamps are ISO-8601 UTC with Z"
CLAUDE_AUDIT_DIR=/nonexistent "$B/claude-audit.sh" <<<'{"hook_event_name":"Stop"}'; ok $? "missing log file: exits 0, writes nothing"

# a second user with a nudge and a long wait, written like the bridge writes them
python3 - "$D" <<'EOF'
import json, sys, datetime as dt
d = sys.argv[1]
now = dt.datetime.now(dt.timezone.utc)
def ts(m): return (now - dt.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
rows = [(50,"session_start"),(49,"prompt"),(40,"notification"),(20,"nudge"),(10,"prompt"),(9,"tool")]
with open(d + "/asha.jsonl", "w") as f:
    for m, e in rows:
        f.write(json.dumps({"ts": ts(m), "user": "asha", "session": "s9", "event": e}) + "\n")
EOF
OUT=$("$B/claude-agent-report" --dir "$D" --days 1 --json)
jq -e '.total.active_users == 2 and .total.sessions == 2 and .total.prompts == 3 and .total.prs_created == 1 and .total.guard_denies == 1' <<<"$OUT" >/dev/null; ok $? "report totals (2 users, 2 sessions, 3 prompts, 1 PR, 1 deny)"
jq -e '.users.asha.nudges == 1 and .users.asha.idle_waits == 1 and .users.asha.idle_wait_max_min == 30' <<<"$OUT" >/dev/null; ok $? "report: nudge counted, 30 minute idle wait measured"
"$B/claude-agent-report" --dir "$D" | grep -q 'Draft PRs created: 1'; ok $? "plain-text report renders"
"$B/claude-agent-report" --dir "$D" --date 2001-01-01 | grep -q 'Active users (sent a prompt): 0'; ok $? "empty window reports zeros"

# box-load samples (claude-agent-monitor), all at noon UTC yesterday: asha peaks at 2 sessions, the box at 3
LOADF=$(mktemp); trap 'rm -rf "$D" "$LOADF"' EXIT
python3 - "$LOADF" <<'PY'
import json, sys, datetime as dt
noon = dt.datetime.now(dt.timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) - dt.timedelta(days=1)
def ts(m): return (noon - dt.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
samples = [(30, 4000, 0, {"asha": (2, 3000, 1, 0), "ravi": (1, 900, 0, 0)}),
           (20, 1200, 1, {"asha": (1, 3500, 4, 1), "ravi": (1, 1000, 0, 0)}),
           (10, 3000, 1, {"asha": (0, 2000, 4, 1), "ravi": (2, 1100, 0, 0)})]
with open(sys.argv[1], "w") as f:
    for m, mem, oom, ppl in samples:
        f.write(json.dumps({"ts": ts(m), "box": {"mem_available_mb": mem, "disk_free_gb": 30.0, "load1": 1.0, "oom_kills": oom},
                            "people": {u: {"active_sessions": a, "mem_mb": mb, "mem_peak_mb": mb, "high_events": h, "max_events": x}
                                       for u, (a, mb, h, x) in ppl.items()}}) + "\n")
PY
OUT=$("$B/claude-agent-report" --dir "$D" --days 3 --load-file "$LOADF")
grep -q '^Load' <<<"$OUT"; ok $? "report has a Load section"
grep -qE 'box peak sessions 3' <<<"$OUT"; ok $? "Load: box-wide peak 3 sessions"
grep -qE '^ +asha +peak sessions 2' <<<"$OUT"; ok $? "Load: asha peak 2 sessions"
grep -qE 'lowest memory 1\.2 GB' <<<"$OUT"; ok $? "Load: lowest memory available"
grep -qE 'OOM kills 1' <<<"$OUT"; ok $? "Load: OOM kills counted from the counter"
J=$("$B/claude-agent-report" --dir "$D" --days 3 --load-file "$LOADF" --json)
jq -e '[.load.days[]][0] | .box_peak_sessions == 3 and .people.asha.peak_sessions == 2 and .people.asha.max_events == 1 and .people.asha.high_events == 3' <<<"$J" >/dev/null; ok $? "Load in --json"
"$B/claude-agent-report" --dir "$D" --load-file /nonexistent | grep -q 'Load: no samples yet'; ok $? "Load: missing file says no samples yet"
cp "$LOADF" "$D/claude-agent-load.jsonl"; cp "$LOADF" "$D/box-load.jsonl"          # misplaced into the audit folder
J=$("$B/claude-agent-report" --dir "$D" --days 3 --load-file /nonexistent --json)
[ $? = 0 ] && jq -e '(.users | keys) == (["asha", "'"$me"'"] | sort)' <<<"$J" >/dev/null; ok $? "load rows in the audit folder are never read as a person"
exit $fail
