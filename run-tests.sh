#!/usr/bin/env bash
# Run every claude-agent-box test in an Ubuntu container (the box runs Ubuntu; several shell tests need GNU tools).
# Needs Docker. Exits non-zero if any suite fails.
set -euo pipefail
cd "$(dirname "$0")"
docker run --rm -v "$PWD/claude-agent-box:/src:ro" ubuntu:24.04 bash -c '
  set -u
  apt-get update -qq >/dev/null && apt-get install -y -qq jq git python3 tmux >/dev/null 2>&1
  cp -r /src /b && cd /b
  git config --global user.email t@t && git config --global user.name t
  rc=0
  out=$(python3 -m unittest discover -s tests -p "test_*.py" 2>&1) || rc=1
  echo "$out" | tail -3
  for t in tests/test_*.sh; do
    echo "== $t"
    if [ "$t" = tests/test_guard.sh ]; then out=$(bash "$t" usr/local/bin/claude-guard.sh 2>&1); else out=$(bash "$t" 2>&1); fi
    s=$?
    echo "$out" | tail -1
    { [ $s -ne 0 ] || echo "$out" | grep -qE "^FAIL| [1-9][0-9]* failed"; } && rc=1
  done
  exit $rc
'
