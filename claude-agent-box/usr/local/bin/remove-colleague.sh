#!/usr/bin/env bash
# Admin script: stop and remove one colleague's agent. Run on the box as root.
#
#   sudo remove-colleague.sh <name>                 show what would be removed (changes nothing)
#   sudo remove-colleague.sh --yes [--keep-home] <name>
#
# Stops and disables claude-agent@<name> and its scheduled job runs, kills its processes, removes the people.json entry and the
# Linux user (and home, unless --keep-home). The audit log is KEPT, renamed <name>.jsonl.removed-<time>,
# so adoption history survives.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
YES=0; KEEP=0; NAME=""
for a in "$@"; do case "$a" in --yes) YES=1 ;; --keep-home) KEEP=1 ;; *) NAME=$a ;; esac; done
[[ $NAME =~ ^[a-z][a-z0-9_-]{1,19}$ ]] || { echo "usage: $0 [--yes] [--keep-home] <name>" >&2; exit 1; }
case "$NAME" in root|ubuntu|ec2-user) echo "refusing to remove $NAME" >&2; exit 1 ;; esac
id "$NAME" >/dev/null 2>&1 || { echo "no Linux user $NAME" >&2; exit 1; }
PEOPLE=/etc/claude-agent/people.json; LOG=/var/log/claude-agent/$NAME.jsonl
if [ "$YES" != 1 ]; then
  echo "Would remove: unit claude-agent@$NAME, processes of $NAME, people.json entry, Linux user $NAME$([ "$KEEP" = 1 ] || echo " + /home/$NAME ($(du -sh "/home/$NAME" 2>/dev/null | cut -f1))")"
  echo "Audit log kept as $LOG.removed-<time>. Re-run with --yes."
  exit 0
fi
systemctl disable --now "claude-agent@$NAME.service" 2>/dev/null || true
runuser -u "$NAME" -- tmux kill-server 2>/dev/null || true
systemctl stop "claude-job-$NAME.*" 2>/dev/null || true          # scheduled task runs in flight
rm -f "/var/lib/claude-agent-jobs/$NAME.json"                     # job runner state (the jobs go with the home)
pkill -u "$NAME" 2>/dev/null || true; sleep 1; pkill -9 -u "$NAME" 2>/dev/null || true
flock /etc/claude-agent/people.lock python3 - "$PEOPLE" "$NAME" <<'PY'
import json, os, sys
p, name = sys.argv[1:3]
if os.path.exists(p):
    d = json.load(open(p)); d["people"] = [x for x in d["people"] if x["name"] != name]
    json.dump(d, open(p + ".tmp", "w"), indent=2); os.chmod(p + ".tmp", 0o640); os.replace(p + ".tmp", p)
PY
if [ -f "$LOG" ]; then mv "$LOG" "$LOG.removed-$(date -u +%Y%m%dT%H%M%SZ)"; fi
if [ "$KEEP" = 1 ]; then userdel "$NAME"; else userdel -r "$NAME" 2>/dev/null || userdel "$NAME"; fi
echo "Removed $NAME."
