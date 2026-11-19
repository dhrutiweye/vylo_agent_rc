"""Tests for the doorbell (agent_outbox.py) and scheduled jobs (agent_jobs.py). No network, no systemd.

  python3 -m unittest tests/test_jobs.py          (from the bundle directory)
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "usr", "local", "lib", "claude-agent"))
import agent_jobs as aj  # noqa: E402
import agent_outbox as ob  # noqa: E402

IST = aj.get_tz("Asia/Kolkata")
UID = os.getuid()


def at(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=IST).timestamp()


class TestOutbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name

    def test_write_then_take_in_order_and_files_are_removed(self):
        ob.write(self.home, "first"); ob.write(self.home, "second", "job:standup")
        got = ob.take(self.home, UID)
        self.assertEqual([(m["source"], m["text"]) for m in got], [("agent", "first"), ("job:standup", "second")])
        self.assertEqual(os.listdir(os.path.join(self.home, ob.OUTBOX)), [])
        self.assertEqual(ob.take(self.home, UID), [])

    def test_files_are_private_and_long_text_is_cut(self):
        n = ob.write(self.home, "x" * 5000)
        p = os.path.join(self.home, ob.OUTBOX, n)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.assertLessEqual(len(ob.take(self.home, UID)[0]["text"]), ob.MAX_CHARS)

    def test_refuses_empty_bad_source_and_a_full_outbox(self):
        self.assertRaises(ValueError, ob.write, self.home, "   ")
        self.assertRaises(ValueError, ob.write, self.home, "hi", "root")
        for i in range(3):
            ob.write(self.home, "m%d" % i, max_pending=3)
        self.assertRaises(ValueError, ob.write, self.home, "one more", "agent", 3)

    def test_message_to_anyone_else_is_dropped(self):
        d = os.path.join(self.home, ob.OUTBOX); os.makedirs(d)
        with open(os.path.join(d, "1800000000000000000-0000abcd.json"), "w") as f:
            json.dump({"to": "ravi", "text": "hello ravi"}, f)
        self.assertEqual(ob.take(self.home, UID), [])

    def test_symlinked_file_is_not_followed_and_is_removed(self):
        secret = os.path.join(self.tmp.name, "secret.txt")
        with open(secret, "w") as f:
            f.write(json.dumps({"text": "SECRET"}))
        d = os.path.join(self.home, ob.OUTBOX); os.makedirs(d)
        os.symlink(secret, os.path.join(d, "1800000000000000000-0000abcd.json"))
        self.assertEqual(ob.take(self.home, UID), [])
        self.assertTrue(os.path.exists(secret))

    def test_hard_linked_file_is_refused(self):
        d = os.path.join(self.home, ob.OUTBOX); os.makedirs(d)
        other = os.path.join(self.tmp.name, "other.json")
        with open(other, "w") as f:
            json.dump({"text": "linked"}, f)
        os.link(other, os.path.join(d, "1800000000000000000-0000abcd.json"))
        self.assertEqual(ob.take(self.home, UID), [])

    def test_symlinked_outbox_directory_is_not_read(self):
        elsewhere = os.path.join(self.tmp.name, "elsewhere"); os.makedirs(elsewhere)
        ob.write(elsewhere, "from elsewhere")
        os.makedirs(os.path.join(self.home, ".claude"))
        os.symlink(os.path.join(elsewhere, ob.OUTBOX), os.path.join(self.home, ob.OUTBOX))
        self.assertEqual(ob.take(self.home, UID), [])

    def test_wrong_owner_is_refused(self):
        ob.write(self.home, "hi")
        self.assertEqual(ob.take(self.home, UID + 1), [])

    def test_labels(self):
        self.assertEqual(ob.label("agent"), "[agent]"); self.assertEqual(ob.label("job:standup"), "[job standup]")

    def test_reading_both_ends_of_a_transcript_safely(self):
        d = os.path.join(self.home, ".claude", "projects", "p"); os.makedirs(d)
        with open(os.path.join(d, "t.jsonl"), "w") as f:
            f.write("HEAD" + "x" * 1000 + "TAIL")
        head, tail = ob.read_home_file_ends(self.home, ".claude/projects/p/t.jsonl", UID, head=10, tail=10)
        self.assertTrue(head.startswith("HEAD")); self.assertTrue(tail.endswith("TAIL")); self.assertEqual(len(tail), 10)
        secret = os.path.join(self.tmp.name, "secret.txt")
        with open(secret, "w") as f:
            f.write("SECRET")
        os.symlink(secret, os.path.join(d, "s.jsonl"))
        self.assertIsNone(ob.read_home_file_ends(self.home, ".claude/projects/p/s.jsonl", UID))
        os.link(os.path.join(d, "t.jsonl"), os.path.join(d, "h.jsonl"))
        self.assertIsNone(ob.read_home_file_ends(self.home, ".claude/projects/p/h.jsonl", UID))
        self.assertIsNone(ob.read_home_file_ends(self.home, ".claude/projects/p/t.jsonl", UID + 1))
        self.assertIsNone(ob.read_home_file_ends(self.home, ".claude/projects/p/none.jsonl", UID))


class TestSchedules(unittest.TestCase):
    def setUp(self):
        self.L = aj.Limits()
        self.now = at(2026, 11, 6, 12, 0)              # a Friday, 12:00 IST

    def test_parse_forms(self):
        self.assertEqual(aj.parse_when("in 30m", self.now, IST), {"type": "at", "ts": int(self.now + 1800)})
        self.assertEqual(aj.parse_when("every 2h", self.now, IST), {"type": "every", "seconds": 7200})
        self.assertEqual(aj.parse_when("daily 9:30", self.now, IST), {"type": "daily", "time": "09:30"})
        self.assertEqual(aj.parse_when("weekdays 10:00", self.now, IST)["type"], "weekdays")
        self.assertEqual(aj.parse_when("at 16:00", self.now, IST)["ts"], int(at(2026, 11, 6, 16, 0)))
        self.assertEqual(aj.parse_when("at 11:00", self.now, IST)["ts"], int(at(2026, 11, 7, 11, 0)))   # tomorrow
        self.assertEqual(aj.parse_when("at 2026-11-09 08:15", self.now, IST)["ts"], int(at(2026, 11, 9, 8, 15)))
        for bad in ("", "soon", "every", "every fortnight", "daily 25:00", "at 2020-01-01 10:00", "at noon"):
            self.assertRaises(ValueError, aj.parse_when, bad, self.now, IST)

    def job(self, when, kind="remind", created=None):
        j = aj.new_job("j", kind, "do it", when, created or self.now, self.L, [])
        return j

    def test_floors_and_counts_and_names(self):
        self.assertRaises(ValueError, aj.new_job, "r", "remind", "x", "every 10m", self.now, self.L, [])
        aj.new_job("r", "remind", "x", "every 15m", self.now, self.L, [])
        self.assertRaises(ValueError, aj.new_job, "t", "task", "x", "every 30m", self.now, self.L, [])
        aj.new_job("t", "task", "x", "every 1h", self.now, self.L, [])
        self.assertRaises(ValueError, aj.new_job, "Bad Name", "remind", "x", "daily 10:00", self.now, self.L, [])
        ten = [{"name": "j%d" % i} for i in range(10)]
        self.assertRaises(ValueError, aj.new_job, "k", "remind", "x", "daily 10:00", self.now, self.L, ten)
        self.assertRaises(ValueError, aj.new_job, "j1", "remind", "x", "daily 10:00", self.now, self.L, [{"name": "j1"}])
        self.assertRaises(ValueError, aj.new_job, "p", "task", "x" * 5000, "daily 10:00", self.now, self.L, [])

    def test_once(self):
        j = self.job("in 30m")
        self.assertFalse(aj.due(j, {}, self.now + 1700, IST, self.L)[0])
        self.assertTrue(aj.due(j, {}, self.now + 1800, IST, self.L)[0])
        self.assertFalse(aj.due(j, {"done": True, "last_run": self.now + 1800}, self.now + 9999, IST, self.L)[0])

    def test_every_runs_once_after_downtime_and_the_floor_holds_even_if_the_file_is_edited(self):
        j = self.job("every 1h")
        self.assertFalse(aj.due(j, {}, self.now + 3599, IST, self.L)[0])
        self.assertTrue(aj.due(j, {}, self.now + 3600, IST, self.L)[0])
        self.assertTrue(aj.due(j, {"last_run": self.now}, self.now + 5 * 3600, IST, self.L)[0])   # 5 missed -> one run
        j["schedule"]["seconds"] = 60                     # hand-edited to every minute
        self.assertFalse(aj.due(j, {"last_run": self.now}, self.now + 120, IST, self.L)[0])
        self.assertTrue(aj.due(j, {"last_run": self.now}, self.now + 900, IST, self.L)[0])     # reminder floor 15 min
        j["kind"], j["prompt"] = "task", "x"
        self.assertFalse(aj.due(j, {"last_run": self.now}, self.now + 900, IST, self.L)[0])    # task floor 60 min

    def test_daily_and_weekdays(self):
        d = self.job("daily 10:00")                       # created 12:00 Fri: first run is Sat 10:00
        self.assertFalse(aj.due(d, {}, at(2026, 11, 6, 23, 0), IST, self.L)[0])
        self.assertTrue(aj.due(d, {}, at(2026, 11, 7, 10, 0), IST, self.L)[0])
        self.assertFalse(aj.due(d, {"last_run": at(2026, 11, 7, 10, 0)}, at(2026, 11, 7, 18, 0), IST, self.L)[0])
        self.assertTrue(aj.due(d, {"last_run": at(2026, 11, 7, 10, 0)}, at(2026, 11, 9, 15, 0), IST, self.L)[0])  # missed Sun: once
        w = self.job("weekdays 10:00")
        self.assertFalse(aj.due(w, {}, at(2026, 11, 7, 10, 30), IST, self.L)[0])   # Saturday
        self.assertFalse(aj.due(w, {}, at(2026, 11, 8, 10, 30), IST, self.L)[0])   # Sunday
        self.assertTrue(aj.due(w, {}, at(2026, 11, 9, 10, 0), IST, self.L)[0])     # Monday
        self.assertEqual(aj.next_run(w, {}, at(2026, 11, 7, 9, 0), IST, self.L), at(2026, 11, 9, 10, 0))

    def test_pause_resume_and_run_now(self):
        j = self.job("daily 10:00")
        j["paused"] = True
        self.assertEqual(aj.due(j, {}, at(2026, 11, 7, 10, 0), IST, self.L), (False, "paused"))
        j["paused"] = False
        st = {"paused_at": self.now + 10}                 # paused by the runner after failures
        self.assertFalse(aj.due(j, st, at(2026, 11, 7, 10, 0), IST, self.L)[0])
        j["resumed_at"] = self.now + 20
        self.assertTrue(aj.due(j, st, at(2026, 11, 7, 10, 0), IST, self.L)[0])
        j["run_now"] = int(self.now + 30)
        self.assertEqual(aj.due(j, {"last_run": self.now}, self.now + 40, IST, self.L), (True, "run now"))
        self.assertFalse(aj.due(j, {"last_run": self.now + 40, "run_now_done": j["run_now"]}, self.now + 60, IST, self.L)[0])

    def test_parse_jobs_drops_invalid_and_duplicates_and_caps_the_count(self):
        good = [aj.new_job("j%d" % i, "remind", "x", "daily 10:00", self.now, self.L, []) for i in range(12)]
        raw = json.dumps({"jobs": [{"name": "BAD"}, good[0], dict(good[0])] + good[1:]})
        jobs = aj.parse_jobs(raw, self.L)
        self.assertEqual(len(jobs), 10); self.assertEqual(jobs[0]["name"], "j0"); self.assertEqual(jobs[1]["name"], "j1")
        self.assertEqual(aj.parse_jobs("not json", self.L), []); self.assertEqual(aj.parse_jobs(None, self.L), [])


class FakeHandle:
    def __init__(self):
        self.rc = None

    def poll(self):
        return self.rc


class FakeHost:
    def __init__(self):
        self.persons = [{"name": "asha", "linux_user": "asha", "status": "active"},
                        {"name": "ravi", "linux_user": "ravi", "status": "active"},
                        {"name": "noor", "linux_user": "noor", "status": "pending"}]
        self.jobs = {}
        self.active = {"asha": True, "ravi": True, "noor": True}
        self.notes, self.launched, self.units = [], [], []

    def people(self):
        return self.persons

    def agent_active(self, user):
        return self.active[user]

    def read_jobs(self, user):
        return json.dumps({"jobs": self.jobs.get(user, [])})

    def notify(self, user, text, source):
        self.notes.append((user, source, text))

    def launch(self, user, job, limits):
        h = FakeHandle(); self.launched.append((user, job["name"], h)); return h

    def running_units(self):
        return list(self.units)

    def gid(self, user):
        return None


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.L = aj.Limits({"JOB_TASK_DAILY_CAP": "3", "JOB_CONCURRENT_BOX": "2"})
        self.clock = [at(2026, 11, 9, 9, 0)]            # Monday 09:00 IST
        self.host = FakeHost()
        self.r = aj.Runner(self.host, self.L, now=lambda: self.clock[0], state_dir=self.tmp.name)

    def add(self, user, name, kind, when, created=None):
        j = aj.new_job(name, kind, "body " + name, when, created or self.clock[0] - 60, self.L, [])
        self.host.jobs.setdefault(user, []).append(j)
        return j

    def st(self, user):
        return aj.load_state(user, self.tmp.name)

    def test_reminder_is_posted_once_at_its_time(self):
        self.add("asha", "standup", "remind", "weekdays 09:45")
        self.r.tick(); self.assertEqual(self.host.notes, [])
        self.clock[0] += 45 * 60; self.r.tick()
        self.assertEqual(self.host.notes, [("asha", "job:standup", "body standup")])
        self.clock[0] += 60; self.r.tick(); self.assertEqual(len(self.host.notes), 1)

    def test_one_off_runs_once_then_is_done(self):
        self.add("asha", "ping", "remind", "in 15m")
        self.clock[0] += 16 * 60; self.r.tick(); self.r.tick()
        self.assertEqual(len(self.host.notes), 1); self.assertTrue(self.st("asha")["jobs"]["ping"]["done"])

    def test_pending_people_and_stopped_agents_run_nothing(self):
        self.add("noor", "a", "remind", "in 15m"); self.add("ravi", "b", "remind", "in 15m")
        self.host.active["ravi"] = False
        self.clock[0] += 16 * 60; self.r.tick()
        self.assertEqual(self.host.notes, [])

    def test_tasks_one_per_person_and_two_per_box(self):
        for u in ("asha", "ravi"):
            self.add(u, "t1", "task", "every 1h"); self.add(u, "t2", "task", "every 1h")
        self.host.persons.append({"name": "zed", "linux_user": "zed", "status": "active"}); self.host.active["zed"] = True
        self.add("zed", "t1", "task", "every 1h")
        self.clock[0] += 3600; self.r.tick()
        self.assertEqual(sorted((u, n) for u, n, _ in self.host.launched), [("asha", "t1"), ("ravi", "t1")])
        self.r.tick(); self.assertEqual(len(self.host.launched), 2)          # still running: nothing new
        for _, _, h in self.host.launched:
            h.rc = 0
        self.r.tick()
        self.assertEqual(sorted((u, n) for u, n, _ in self.host.launched[2:]), [("asha", "t2"), ("ravi", "t2")])

    def test_units_from_before_a_restart_count_as_busy(self):
        self.add("asha", "t1", "task", "every 1h"); self.host.units = [("ravi", "x"), ("zed", "y")]
        self.clock[0] += 3600; self.r.tick(); self.assertEqual(self.host.launched, [])

    def test_daily_cap_with_one_notice(self):
        self.add("asha", "t", "task", "every 1h")
        for _ in range(5):
            self.clock[0] += 3600; self.r.tick()
            for _, _, h in self.host.launched:
                h.rc = 0
            self.r.reap()
        self.assertEqual(len(self.host.launched), 3)
        caps = [n for n in self.host.notes if "limit" in n[2]]
        self.assertEqual(len(caps), 1)
        self.clock[0] = at(2026, 11, 10, 9, 30); self.r.tick()              # next day: runs again
        self.assertEqual(len(self.host.launched), 4)

    def test_three_failures_pause_the_job_and_resume_clears_it(self):
        self.r.limits = aj.Limits()                       # default daily cap, so only the failures matter
        j = self.add("asha", "t", "task", "every 1h")
        for _ in range(3):
            self.clock[0] += 3600; self.r.tick()
            self.host.launched[-1][2].rc = 1; self.r.reap()
        self.assertIn("paused", self.host.notes[-1][2])
        self.clock[0] += 3600; self.r.tick(); self.assertEqual(len(self.host.launched), 3)
        j["resumed_at"] = int(self.clock[0]); self.clock[0] += 3600; self.r.tick()
        self.assertEqual(len(self.host.launched), 4)

    def test_success_resets_failures(self):
        self.r.limits = aj.Limits()
        self.add("asha", "t", "task", "every 1h")
        for rc in (1, 1, 0, 1, 1):
            self.clock[0] += 3600; self.r.tick(); self.host.launched[-1][2].rc = rc; self.r.reap()
        self.assertEqual(self.st("asha")["jobs"]["t"]["failures"], 2)
        self.assertNotIn("paused_at", self.st("asha")["jobs"]["t"])

    def test_removed_jobs_leave_the_state(self):
        self.add("asha", "a", "remind", "in 15m"); self.clock[0] += 16 * 60; self.r.tick()
        self.host.jobs["asha"] = []; self.add("asha", "b", "remind", "daily 23:00"); self.r.tick()
        self.assertNotIn("a", self.st("asha")["jobs"])

    def test_run_now_ignores_the_schedule_but_not_the_daily_cap(self):
        j = self.add("asha", "t", "task", "daily 18:00")
        j["run_now"] = int(self.clock[0]); self.r.tick()
        self.assertEqual(len(self.host.launched), 1)
        self.host.launched[-1][2].rc = 0; self.r.tick(); self.assertEqual(len(self.host.launched), 1)

    def test_state_file_mode(self):
        self.add("asha", "a", "remind", "in 15m"); self.clock[0] += 16 * 60; self.r.tick()
        self.assertEqual(os.stat(aj.state_path("asha", self.tmp.name)).st_mode & 0o777, 0o640)


class TestJobStore(unittest.TestCase):
    def test_change_is_saved_private_and_a_refusal_changes_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            s = aj.JobStore(home)
            s.change(lambda d: d["jobs"].append({"name": "a"}))
            self.assertEqual(s.read()["jobs"], [{"name": "a"}])
            self.assertEqual(os.stat(s.path).st_mode & 0o777, 0o600)

            def refuse(d):
                d["jobs"].append({"name": "b"}); raise ValueError("no")
            self.assertRaises(ValueError, s.change, refuse)
            self.assertEqual(s.read()["jobs"], [{"name": "a"}])


class TestLaunch(unittest.TestCase):
    """The command the runner hands to systemd-run for a task."""
    def test_launch_uses_agents_slice(self):
        import importlib.machinery
        import importlib.util
        import types
        from unittest import mock
        path = os.path.join(os.path.dirname(__file__), "..", "usr", "local", "bin", "claude-agent-jobs")
        loader = importlib.machinery.SourceFileLoader("claude_agent_jobs_bin", path)
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(mod)
        host = mod.SystemHost()
        limits = types.SimpleNamespace(memory_max="1G", cpu_quota="100%", timeout=600)
        with mock.patch.object(mod.SystemHost, "_pw", return_value=types.SimpleNamespace(pw_dir="/home/asha")), \
                mock.patch.object(mod.subprocess, "Popen") as popen:
            host.launch("asha", {"name": "t1"}, limits)
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[0], "systemd-run")
        self.assertIn("--slice", cmd)
        self.assertEqual(cmd[cmd.index("--slice") + 1], "claudeagents.slice")


if __name__ == "__main__":
    unittest.main()
