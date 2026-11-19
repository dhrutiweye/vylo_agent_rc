"""Tests for claude-agent-monitor against fixture /proc, cgroup, home and audit trees. No systemd, no network.

  python3 -m unittest tests/test_monitor.py          (from the bundle directory)
"""
import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "usr", "local", "lib", "claude-agent"))
import agent_outbox as ob  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("claude_agent_monitor", os.path.join(HERE, "..", "usr", "local", "bin",
                                                                                  "claude-agent-monitor"))
mon = importlib.util.module_from_spec(importlib.util.spec_from_loader(_loader.name, _loader))
_loader.exec_module(mon)

NOW = 1_800_000_000.0
CONV_A = "11111111-2222-4333-8444-555555555555"
CONV_B = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
MB = 1024 * 1024


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        r = self.tmp.name
        self.paths = mon.Paths(proc=r + "/proc", cgroup=r + "/cg", homes=r + "/home", audit=r + "/audit",
                               people=r + "/people.json", disk=r, load_log=r + "/load.jsonl", state=r + "/state.json")
        put(r + "/proc/meminfo", "MemTotal:       15990000 kB\nMemFree:          900000 kB\nMemAvailable:    4096000 kB\n")
        put(r + "/proc/loadavg", "1.50 1.20 0.90 3/400 12345\n")
        sl = r + "/cg/claudeagents.slice"
        put(sl + "/memory.events", "low 0\nhigh 0\nmax 0\noom 0\noom_kill 2\n")
        asha = sl + "/claude-agent@asha.service"
        put(asha + "/memory.current", str(3000 * MB) + "\n")
        put(asha + "/memory.peak", str(3500 * MB) + "\n")
        put(asha + "/memory.events", "low 0\nhigh 7\nmax 1\noom 0\noom_kill 0\n")
        put(asha + "/cgroup.procs", "101\n102\n103\n")
        for pid, comm in (("101", "claude"), ("102", "claude"), ("103", "bash")):
            put(r + "/proc/%s/comm" % pid, comm + "\n")
        v = r + "/home/asha/Documents/code/vylo"
        os.makedirs(v + "/.worktrees/voiceLog-a"); os.makedirs(v + "/.worktrees/voiceLog-b")
        os.makedirs(v + "/voiceLog/.claude/worktrees/helper")
        put(v + "/.worktrees/voiceLog-a/big.bin", "x" * (2 * MB))
        put(r + "/people.json", json.dumps({"people": [
            {"name": "asha", "linux_user": "asha", "status": "active"},
            {"name": "ravi", "linux_user": "ravi", "status": "pending"}]}))
        rows = [{"ts": iso(NOW - 120), "event": "tool", "session": CONV_A},
                {"ts": iso(NOW - 60), "event": "prompt", "session": CONV_B},
                {"ts": iso(NOW - 30), "event": "tool", "session": CONV_A},
                {"ts": iso(NOW - 3600), "event": "tool", "session": "cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee"},
                {"ts": iso(NOW - 10), "event": "tool", "session": CONV_B, "job": "digest"}]
        put(r + "/audit/asha.jsonl", "".join(json.dumps(x) + "\n" for x in rows))


class TestSample(Fixture):
    def test_sample_reads_cgroup_and_meminfo(self):
        s = mon.sample(self.paths, NOW, measure_sizes=True)
        self.assertEqual(s["ts"], iso(NOW))
        self.assertEqual(s["box"]["mem_available_mb"], 4000)
        self.assertEqual(s["box"]["load1"], 1.5)
        self.assertEqual(s["box"]["oom_kills"], 2)
        self.assertIsInstance(s["box"]["disk_free_gb"], float)
        a = s["people"]["asha"]
        self.assertEqual((a["mem_mb"], a["mem_peak_mb"], a["high_events"], a["max_events"]), (3000, 3500, 7, 1))
        self.assertEqual(a["active_sessions"], 2)             # two transcripts in 10 min; the job and the old one do not count
        self.assertEqual(a["claude_procs"], 2)
        self.assertEqual(a["worktrees"], 3)                   # vylo/.worktrees/* and <repo>/.claude/worktrees/*
        self.assertGreaterEqual(a["worktrees_mb"], 2)
        self.assertNotIn("ravi", s["people"])                 # no agent unit, nothing to report

    def test_sizes_are_skipped_unless_asked(self):
        self.assertNotIn("worktrees_mb", mon.sample(self.paths, NOW)["people"]["asha"])

    def test_missing_files_do_not_fail(self):
        os.remove(self.paths.cgroup + "/claudeagents.slice/claude-agent@asha.service/memory.peak")
        os.remove(self.paths.cgroup + "/claudeagents.slice/memory.events")
        s = mon.sample(self.paths, NOW)
        self.assertIsNone(s["people"]["asha"]["mem_peak_mb"]); self.assertIsNone(s["box"]["oom_kills"])


def box(mem=4000, disk=40.0, oom=0):
    return {"ts": iso(NOW), "box": {"mem_available_mb": mem, "disk_free_gb": disk, "load1": 1.0, "oom_kills": oom},
            "people": {"asha": {"mem_mb": 3900, "active_sessions": 2}, "ravi": {"mem_mb": 800, "active_sessions": 1}}}


class TestAlerts(unittest.TestCase):
    def test_memory_alert_needs_two_low_samples(self):
        msgs, st = mon.alerts({}, box(mem=1200), NOW)
        self.assertEqual(msgs, [])
        msgs, st = mon.alerts(st, box(mem=1300), NOW + 300)
        self.assertEqual(len(msgs), 1)
        self.assertIn("1.3 GB", msgs[0]); self.assertIn("asha", msgs[0])
        msgs, st = mon.alerts(st, box(mem=4000), NOW + 600)                 # recovered: the streak resets
        msgs, st = mon.alerts(st, box(mem=1000), NOW + 900)
        self.assertEqual(msgs, [])

    def test_alert_once_per_hour_per_kind(self):
        st = {}
        for i in range(2):
            msgs, st = mon.alerts(st, box(mem=1000), NOW + i * 300)
        self.assertEqual(len(msgs), 1)
        sent = []
        for i in range(2, 12):                                              # still low, up to 55 min after the first
            msgs, st = mon.alerts(st, box(mem=1000), NOW + i * 300); sent += msgs
        self.assertEqual(sent, [])
        msgs, st = mon.alerts(st, box(mem=1000), NOW + 300 + 61 * 60)
        self.assertEqual(len(msgs), 1)

    def test_disk_and_oom_alert_immediately(self):
        msgs, st = mon.alerts({}, box(disk=9.0, oom=0), NOW)
        self.assertEqual(len(msgs), 1); self.assertIn("9.0 GB", msgs[0])
        msgs, st = mon.alerts(st, box(disk=40.0, oom=1), NOW + 300)
        self.assertEqual(len(msgs), 1); self.assertIn("killed", msgs[0])
        msgs, st = mon.alerts(st, box(disk=40.0, oom=1), NOW + 600)
        self.assertEqual(msgs, [])

    def test_oom_baseline_and_reboot_do_not_alert(self):
        msgs, st = mon.alerts({}, box(oom=5), NOW)                            # first run: a baseline, not a kill
        self.assertEqual(msgs, [])
        msgs, st = mon.alerts(st, box(oom=0), NOW + 300)                      # counter reset by a reboot
        self.assertEqual(msgs, [])


class TestRun(Fixture):
    def test_alert_goes_to_admin_outbox_with_box_source(self):
        put(self.paths.proc + "/meminfo", "MemAvailable:    1000000 kB\n")
        admin_home = os.path.join(self.tmp.name, "home", "dhrutiman"); os.makedirs(admin_home)

        def writer(person, text):
            self.assertEqual(person, "dhrutiman")
            ob.write(admin_home, text, "box")
        for i in range(2):
            mon.run(self.paths, NOW + i * 300, {"ADMIN_PERSON": "dhrutiman"}, writer)
        msgs = ob.take(admin_home, os.getuid())
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["source"], "box")
        self.assertEqual(ob.label("box"), "[box]")
        with open(self.paths.load_log) as f:
            self.assertEqual(len([x for x in f if '"box"' in x]), 2)

    def test_no_admin_means_no_message_but_the_sample_is_logged(self):
        put(self.paths.proc + "/meminfo", "MemAvailable:    1000000 kB\n")
        calls = []
        for i in range(2):
            mon.run(self.paths, NOW + i * 300, {}, lambda p, t: calls.append(p))
        self.assertEqual(calls, [])
        with open(self.paths.load_log) as f:
            rows = [json.loads(x) for x in f]
        samples = [r for r in rows if "box" in r]
        self.assertEqual(len(samples), 2); self.assertEqual(samples[0]["box"]["mem_available_mb"], 976)
        sizes = [r for r in rows if "worktree_sizes_mb" in r]               # once an hour, on a line of its own
        self.assertEqual(len(sizes), 1); self.assertGreaterEqual(sizes[0]["worktree_sizes_mb"]["asha"], 2)

    def test_a_writer_failure_still_logs_and_keeps_the_alert_for_next_time(self):
        put(self.paths.proc + "/meminfo", "MemAvailable:    1000000 kB\n")

        def broken(person, text):
            raise OSError("no outbox")
        for i in range(2):
            mon.run(self.paths, NOW + i * 300, {"ADMIN_PERSON": "dhrutiman"}, broken)
        sent = []
        mon.run(self.paths, NOW + 600, {"ADMIN_PERSON": "dhrutiman"}, lambda p, t: sent.append(t))
        self.assertEqual(len(sent), 1)

    def test_slice_name_has_no_dash(self):
        # systemd nests a slice under the part before a dash (a-b.slice lives in a.slice/a-b.slice), so the flat
        # cgroup path this monitor reads only exists for a dash-free name
        self.assertTrue(mon.SLICE.endswith(".slice"))
        self.assertNotIn("-", mon.SLICE[: -len(".slice")])

    def test_admin_person_may_be_quoted(self):
        env_file = os.path.join(self.tmp.name, "bridge.env")
        put(env_file, 'ADMIN_PERSON="dhrutiman"\nOTHER=\'x\'\n# ADMIN_PERSON=nobody\n')
        self.assertEqual(mon._admin(mon.bridge_env(env_file)), "dhrutiman")

    def test_a_symlinked_worktree_is_not_walked(self):
        outside = os.path.join(self.tmp.name, "outside"); os.makedirs(outside)
        put(outside + "/huge.bin", "x" * (3 * MB))
        os.symlink(outside, self.paths.homes + "/asha/Documents/code/vylo/.worktrees/link")
        os.symlink(outside, self.paths.homes + "/asha/Documents/code/vylo/.worktrees/voiceLog-a/inner-link")
        a = mon.sample(self.paths, NOW, measure_sizes=True)["people"]["asha"]
        self.assertEqual(a["worktrees"], 3)                                  # the link is not a worktree
        self.assertLess(a["worktrees_mb"], 3)                                # and nothing behind a link is counted

    def test_the_sample_is_logged_before_sizes_are_measured(self):
        def boom(*a, **k):
            raise RuntimeError("killed while walking")
        orig = mon._size_mb
        mon._size_mb = boom
        try:
            with self.assertRaises(RuntimeError):
                mon.run(self.paths, NOW, {}, lambda p, t: None)
        finally:
            mon._size_mb = orig
        with open(self.paths.load_log) as f:
            self.assertEqual(len(f.read().splitlines()), 1)
        with open(self.paths.state) as f:
            self.assertEqual(json.load(f)["sizes_at"], NOW)                  # the next run does not walk again at once

    def test_the_size_walk_stops_at_its_time_budget(self):
        clock = iter(range(0, 10_000, 100))
        self.assertIsNone(mon._size_mb(self.paths.homes, deadline=150, clock=lambda: next(clock)))

    def test_load_log_path_is_outside_audit_dir(self):
        self.assertEqual(mon.LOAD_LOG, "/var/log/claude-agent-load.jsonl")
        self.assertFalse(os.path.dirname(mon.LOAD_LOG).startswith("/var/log/claude-agent/"))
        self.assertNotEqual(os.path.dirname(mon.LOAD_LOG), "/var/log/claude-agent")

    def test_box_source_is_accepted_by_the_outbox(self):
        home = os.path.join(self.tmp.name, "home", "x"); os.makedirs(home)
        ob.write(home, "hello", "box")
        self.assertEqual(ob.take(home, os.getuid())[0]["source"], "box")


if __name__ == "__main__":
    unittest.main()
