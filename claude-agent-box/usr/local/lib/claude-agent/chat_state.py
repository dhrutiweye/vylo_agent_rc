"""The bridge's own record of Chat tasks and colleague questions (asks), one JSON file.

  /var/lib/claude-agent-chat/state.json   mode 600, written via temp file + rename under an flock on state.lock

Nothing here is a secret: thread names, ids, statuses and counters. An ask's question text is kept only until it
is closed (`close` drops it); a queued task's run request (its text, already secret-checked) only until the bridge
starts it (`request` is then set to None). Every change reads, edits and writes the whole file under the lock, so two
ChatState objects (or two processes) on one file never lose each other's updates.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os

TASKS_KEPT_PER_USER = 20
THREADS_KEPT = 20                          # more threads of one item: the bot's posts about it
SEEN_KEPT = 500                            # Chat message names already handled, so a redelivery acts once
ASK_KEPT_SECONDS = 7 * 86400
TASK_LOG_KEPT_SECONDS = 3 * 86400          # per-task creation stamps for the daily limit, outliving the 20-task trim
NOT_APPROVED = ("awaiting_ok", "declined", "expired")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")


def _tz():
    try:                                                   # same zone agent_jobs uses (JOB_TZ)
        from agent_jobs import get_tz
        return get_tz("Asia/Kolkata")
    except Exception:                                      # noqa: BLE001
        return IST


def ask_thread_role(a, user, thread):
    """"approval", "received" or "copy" when `thread` is one of ask a's threads on user's side, else None."""
    threads = [("approval", a.get("approval_thread")), ("received", a.get("received_thread")),
               ("copy", a.get("copy_thread"))] + [tuple(x) for x in a.get("threads") or []]
    for role, th in threads:
        if th and th == thread and user == (a["from"] if role == "approval" else a["to"]):
            return role                                    # "copy": "Your agent answered ...", a correction
    return None


class ChatState:
    def __init__(self, path, now):
        self.path = path
        self.now = now
        self.lock_path = os.path.join(os.path.dirname(path) or ".", "state.lock")
        self.tz = _tz()

    # ---- storage
    def _empty(self):
        return {"next_ref": 1, "tasks": [], "asks": [], "task_log": [], "seen": []}

    def _read(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
        except (FileNotFoundError, ValueError):
            return self._empty()
        if not isinstance(d, dict):                        # a list or a number: as corrupt as bad JSON
            return self._empty()
        for k, v in self._empty().items():
            d.setdefault(k, v)
        return d

    def _write(self, d):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def _modify(self, fn):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        lock = open(self.lock_path, "a+")
        try:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            d = self._read()
            out = fn(d)
            self._write(d)
            return out
        finally:
            lock.close()

    @staticmethod
    def _find(d, key):
        if key.startswith("t"):
            return next((t for t in d["tasks"] if t["ref"] == key), None)
        return next((a for a in d["asks"] if a["ask"] == key), None)

    def _day(self, ts):
        return dt.datetime.fromtimestamp(ts, self.tz).date()

    # ---- tasks
    def new_task(self, user, thread, session_uuid):
        def fn(d):
            now = self.now()
            t = {"ref": "t%d" % d["next_ref"], "user": user, "thread": thread, "session_uuid": session_uuid,
                 "status": "running", "run_status": "running", "last_at": now, "created_at": now}
            d["next_ref"] += 1
            d["tasks"].append(t)
            mine = [x for x in d["tasks"] if x["user"] == user]
            for old in mine[:-TASKS_KEPT_PER_USER]:
                d["tasks"].remove(old)
            d["task_log"] = [e for e in d["task_log"] if now - e[1] < TASK_LOG_KEPT_SECONDS] + [[user, now]]
            return dict(t)
        return self._modify(fn)

    def log_task_run(self, user):
        """A reply in a task's thread runs the agent again: it counts against the daily task limit like a new task."""
        def fn(d):
            now = self.now()
            d["task_log"] = [e for e in d["task_log"] if now - e[1] < TASK_LOG_KEPT_SECONDS] + [[user, now]]
        self._modify(fn)

    def add_thread(self, key, role, thread):
        """Another thread that belongs to task or ask `key`. The bot posts everything at the top level, and a plain
        post starts its own thread, so each post about an item is a thread of its own: a reply inside any of them
        finds the item again. role is
        "task", or for an ask "approval" (the asker's side), "received" or "copy" (the colleague's)."""
        if not thread:
            return

        def fn(d):
            item = self._find(d, key)
            if item is None:
                return
            kept = [t for t in item.get("threads") or [] if t[1] != thread]
            item["threads"] = (kept + [[role, thread]])[-THREADS_KEPT:]
        self._modify(fn)

    def mark_seen(self, msg_id):
        """True the first time a Chat message name is seen, False on a redelivery (also one after a bridge restart).
        Only the names are kept (the last SEEN_KEPT), never any text."""
        def fn(d):
            seen = d["seen"] if isinstance(d.get("seen"), list) else []
            if msg_id in seen:
                return False
            d["seen"] = (seen + [msg_id])[-SEEN_KEPT:]
            return True
        return self._modify(fn)

    # ---- asks
    def new_ask(self, from_user, to_user, text, ask_id):
        def fn(d):
            a = {"ask": ask_id, "from": from_user, "to": to_user, "text": text, "status": "awaiting_ok",
                 "run_status": "idle", "approval_thread": None, "received_thread": None, "created_at": self.now()}
            d["asks"].append(a)
            return dict(a)
        return self._modify(fn)

    def close(self, ask_id):
        def fn(d):
            a = self._find(d, ask_id)
            if a:
                a.pop("text", None)
                a["closed_at"] = self.now()            # status is left alone: a declined ask must stay unapproved
        self._modify(fn)

    def prune(self):
        def fn(d):
            cutoff = self.now() - ASK_KEPT_SECONDS
            d["asks"] = [a for a in d["asks"] if a.get("created_at", 0) >= cutoff]
        self._modify(fn)

    # ---- common
    def update(self, ref_or_ask, **fields):
        def fn(d):
            item = self._find(d, ref_or_ask)
            if item is None:
                return None
            item.update(fields)
            return dict(item)
        return self._modify(fn)

    def get(self, ref_or_ask):
        item = self._find(self._read(), ref_or_ask)
        return dict(item) if item else None

    def tasks(self, user=None):
        """Copies of the kept tasks, oldest first; only `user`'s when given."""
        return [dict(t) for t in self._read()["tasks"] if user is None or t["user"] == user]

    def asks(self):
        """Copies of the kept asks, oldest first, closed ones included (the bridge's expiry and DM-fallback sweep)."""
        return [dict(a) for a in self._read()["asks"]]

    def item_by_thread(self, user, thread):
        if not thread:
            return None
        d = self._read()
        for t in d["tasks"]:
            if t["user"] == user and thread in [t["thread"]] + [x[1] for x in t.get("threads") or []]:
                return "task", dict(t)
        for a in d["asks"]:
            if a.get("closed_at") is None:
                role = ask_thread_role(a, user, thread)
                if role:
                    return role, dict(a)
        return None

    def count_today(self, kind, user):
        d = self._read()
        today = self._day(self.now())
        if kind == "task":
            return sum(1 for u, ts in d["task_log"] if u == user and self._day(ts) == today)
        asks = [a for a in d["asks"] if self._day(a.get("created_at", 0)) == today]
        if kind == "ask_sent":
            return sum(1 for a in asks if a["from"] == user)
        if kind == "ask_received":      # only asks the asker approved: a refused or unanswered request is not the colleague's load
            return sum(1 for a in asks if a["to"] == user and a["status"] not in NOT_APPROVED)
        raise ValueError("unknown count kind: %s" % kind)

    def running(self):
        d = self._read()
        return sum(1 for i in d["tasks"] + d["asks"] if i.get("run_status") == "running")
