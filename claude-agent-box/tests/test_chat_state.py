"""Tests for chat_state.py: the bridge's on-disk record of Chat tasks and colleague questions.

  python3 -m unittest tests/test_chat_state.py          (from the bundle directory)
"""
import datetime as dt
import json
import os
import stat
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "usr", "local", "lib", "claude-agent"))
import chat_state as cs  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def epoch(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=IST).timestamp()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "state.json")
        self.now = [epoch(2026, 11, 26, 10, 0)]
        self.st = cs.ChatState(self.path, lambda: self.now[0])


class TestChatState(Base):
    def test_state_round_trip_and_thread_lookup(self):
        t = self.st.new_task("asha", "spaces/D/threads/T1", "uuid-1")
        self.assertEqual(t["ref"], "t1")
        self.assertEqual((t["user"], t["thread"], t["session_uuid"], t["status"], t["run_status"]),
                         ("asha", "spaces/D/threads/T1", "uuid-1", "running", "running"))
        self.assertEqual(self.st.new_task("asha", "spaces/D/threads/T2", "uuid-2")["ref"], "t2")
        a = self.st.new_ask("asha", "ravi", "what is the port?", "a0123456789")
        self.assertEqual((a["ask"], a["from"], a["to"], a["status"], a["run_status"], a["approval_thread"],
                          a["received_thread"]), ("a0123456789", "asha", "ravi", "awaiting_ok", "idle", None, None))
        self.st.update("a0123456789", approval_thread="spaces/D/threads/AP", received_thread="spaces/R/threads/RC")
        fresh = cs.ChatState(self.path, lambda: self.now[0])                      # a restart: read from disk
        role, item = fresh.item_by_thread("asha", "spaces/D/threads/T1")
        self.assertEqual((role, item["ref"]), ("task", "t1"))
        self.assertEqual(fresh.item_by_thread("asha", "spaces/D/threads/AP")[0], "approval")
        self.assertEqual(fresh.item_by_thread("ravi", "spaces/R/threads/RC")[0], "received")
        self.assertIsNone(fresh.item_by_thread("ravi", "spaces/D/threads/AP"))     # not the asker
        self.assertIsNone(fresh.item_by_thread("asha", "spaces/D/threads/nope"))
        self.assertEqual(fresh.get("t2")["session_uuid"], "uuid-2")
        self.assertEqual(fresh.get("a0123456789")["text"], "what is the port?")
        self.assertIsNone(fresh.get("t99")); self.assertIsNone(fresh.get("a999"))
        self.assertEqual(fresh.update("t1", status="done")["status"], "done")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_running_counts_run_status(self):
        self.st.new_task("asha", "T1", "u1"); self.st.new_task("ravi", "T2", "u2")
        self.st.new_ask("asha", "ravi", "q", "aaaaaaaaaaa")
        self.assertEqual(self.st.running(), 2)
        self.st.update("t1", run_status="idle"); self.st.update("aaaaaaaaaaa", run_status="running")
        self.assertEqual(self.st.running(), 2)
        self.st.update("aaaaaaaaaaa", run_status="queued")
        self.assertEqual(self.st.running(), 1)

    def test_newest_20_tasks_kept_per_user(self):
        for i in range(25):
            self.st.new_task("asha", "T%d" % i, "u%d" % i)
        self.st.new_task("ravi", "R", "ur")
        self.assertIsNone(self.st.get("t1")); self.assertIsNotNone(self.st.get("t25"))
        self.assertIsNone(self.st.item_by_thread("asha", "T4"))
        self.assertEqual(self.st.item_by_thread("asha", "T5")[1]["ref"], "t6")
        self.assertEqual(self.st.item_by_thread("ravi", "R")[1]["ref"], "t26")

    def test_counts_reset_at_ist_midnight(self):
        self.now[0] = epoch(2026, 11, 26, 23, 50)
        self.st.new_task("asha", "T1", "u1")
        self.st.new_ask("asha", "ravi", "q", "a000000001x")
        self.st.update("a000000001x", status="asked")
        self.assertEqual((self.st.count_today("task", "asha"), self.st.count_today("ask_sent", "asha"),
                          self.st.count_today("ask_received", "ravi")), (1, 1, 1))
        self.now[0] = epoch(2026, 11, 27, 0, 10)               # 20 minutes later: a new IST day
        self.assertEqual((self.st.count_today("task", "asha"), self.st.count_today("ask_sent", "asha"),
                          self.st.count_today("ask_received", "ravi")), (0, 0, 0))
        self.st.new_task("asha", "T2", "u2")
        self.assertEqual(self.st.count_today("task", "asha"), 1)
        self.assertEqual(self.st.count_today("task", "ravi"), 0)

    def test_declined_asks_do_not_count_as_received(self):
        for i, status in enumerate(["awaiting_ok", "declined", "expired", "answered"]):
            self.st.new_ask("asha", "ravi", "q", "a%010d" % i)
            self.st.update("a%010d" % i, status=status)
        self.assertEqual(self.st.count_today("ask_received", "ravi"), 1)
        self.assertEqual(self.st.count_today("ask_sent", "asha"), 4)

    def test_closed_ask_forgets_its_text(self):
        self.st.new_ask("asha", "ravi", "secret-ish question", "aclosed0001")
        self.st.update("aclosed0001", approval_thread="AP", received_thread="RC")
        self.st.close("aclosed0001")
        a = self.st.get("aclosed0001")
        self.assertEqual(a["status"], "awaiting_ok"); self.assertIsNotNone(a["closed_at"]); self.assertNotIn("text", a)
        self.assertNotIn("secret-ish", open(self.path).read())
        self.assertIsNone(self.st.item_by_thread("asha", "AP"))     # closed asks do not own threads
        self.assertIsNone(self.st.item_by_thread("ravi", "RC"))

    def test_declined_or_expired_ask_stays_unapproved_after_close(self):
        for i, status in enumerate(["declined", "expired"]):
            ask_id = "a%010d" % i
            self.st.new_ask("asha", "ravi", "q", ask_id)
            self.st.update(ask_id, status=status, approval_thread="AP%d" % i, received_thread="RC%d" % i)
            self.st.close(ask_id)
            self.assertEqual(self.st.get(ask_id)["status"], status)
            self.assertNotIn("text", self.st.get(ask_id))
            self.assertIsNone(self.st.item_by_thread("asha", "AP%d" % i))
            self.assertIsNone(self.st.item_by_thread("ravi", "RC%d" % i))
        self.assertEqual(self.st.count_today("ask_received", "ravi"), 0)
        self.assertEqual(self.st.count_today("ask_sent", "asha"), 2)

    def test_old_asks_are_pruned_after_7_days(self):
        self.st.new_ask("asha", "ravi", "old", "aold0000001")
        self.now[0] += 6 * 86400
        self.st.new_ask("asha", "ravi", "newer", "anew0000001")
        self.st.prune()
        self.assertIsNotNone(self.st.get("aold0000001"))
        self.now[0] += 2 * 86400                               # the first is now 8 days old, the second 2
        self.st.prune()
        self.assertIsNone(self.st.get("aold0000001")); self.assertIsNotNone(self.st.get("anew0000001"))

    def test_concurrent_writes_do_not_lose_updates(self):
        a = cs.ChatState(self.path, lambda: self.now[0]); b = cs.ChatState(self.path, lambda: self.now[0])
        errs = []

        def run(st, who):
            try:
                for i in range(15):
                    st.new_task(who, "%s-%d" % (who, i), "u")
            except Exception as e:                              # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=run, args=(a, "asha")), threading.Thread(target=run, args=(b, "ravi"))]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(errs, [])
        tasks = json.load(open(self.path))["tasks"]
        self.assertEqual(len(tasks), 30)
        self.assertEqual(len({t["ref"] for t in tasks}), 30)    # no ref handed out twice


if __name__ == "__main__":
    unittest.main()
