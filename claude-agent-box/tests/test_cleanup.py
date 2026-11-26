"""Tests for claude-agent-cleanup: fake command runner for docker/journalctl/notify-me, real temporary git repos
for the worktree step (git run as the current user). No systemd, no network.

  python3 -m unittest tests/test_cleanup.py          (from the bundle directory)
"""
import getpass
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "usr", "local", "lib", "claude-agent"))
_loader = importlib.machinery.SourceFileLoader("claude_agent_cleanup", os.path.join(HERE, "..", "usr", "local", "bin",
                                                                                  "claude-agent-cleanup"))
cl = importlib.util.module_from_spec(importlib.util.spec_from_loader(_loader.name, _loader))
_loader.exec_module(cl)

DAY = 86400
ME = getpass.getuser()
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_TERMINAL_PROMPT="0")


def put(path, text="x", age_days=0, atime_days=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    age(path, age_days, atime_days)


def age(path, days, atime_days=None):
    t = time.time() - days * DAY
    a = time.time() - (days if atime_days is None else atime_days) * DAY
    os.utime(path, (a, t), follow_symlinks=False)


def git(*args, cwd="/"):
    return subprocess.run(("git",) + args, cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True).stdout


class FakeRun:
    """Records every call; git runs for real (as this user), notify-me is recorded, docker/journalctl are scripted."""

    def __init__(self, outputs=None, fail=()):
        self.calls, self.notes, self.outputs, self.fail = [], [], outputs or {}, set(fail)

    def __call__(self, argv, user=None, home=None, timeout=600):
        self.calls.append((list(argv), user))
        if argv[0].endswith("notify-me"):
            self.notes.append((user, argv))
            return 0, ""
        if argv[0] == "git":
            p = subprocess.run(argv, cwd="/", env=GIT_ENV, capture_output=True, text=True)
            return p.returncode, p.stdout
        if argv[0] in self.fail:
            raise RuntimeError("boom")
        return 0, self.outputs.get(argv[0], "")

    def argvs(self):
        return [a for a, _ in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = os.path.realpath(self.td.name)
        self.home = os.path.join(self.root, "home")
        self.tmp = os.path.join(self.root, "tmp")
        self.backup = os.path.join(self.root, "backup")
        for d in (self.home, self.tmp, self.backup):
            os.makedirs(d)
        self.people = os.path.join(self.root, "people.json")
        self.write_people([{"name": ME, "linux_user": ME, "status": "active"}])
        self.state = os.path.join(self.root, "state.json")
        self.run_ = FakeRun()
        self.printed = []

    def write_people(self, people):
        with open(self.people, "w") as f:
            json.dump({"people": people}, f)

    def cfg(self, **kw):
        c = cl.Cfg(dry_run=False, now=time.time(), tmp=self.tmp, backup=self.backup, people_file=self.people,
                   state_file=self.state, run=self.run_, which=lambda n: "/usr/bin/" + n,
                   home_of=lambda u: (os.getuid(), self.home) if u == ME else None, cwds=lambda: [],
                   out=self.printed.append)
        for k, v in kw.items():
            setattr(c, k, v)
        return c


class DockerJournal(Base):
    def test_docker_commands_exactly(self):
        cl.step_docker(self.cfg())
        self.assertEqual(self.run_.argvs(), [
            ["docker", "image", "prune", "-a", "-f", "--filter", "until=72h"],
            ["docker", "volume", "prune", "-f"],
            ["docker", "builder", "prune", "-f", "--filter", "until=72h"]])

    def test_docker_never_all_volumes(self):
        cl.step_docker(self.cfg())
        vol = [a for a in self.run_.argvs() if a[1] == "volume"][0]
        self.assertNotIn("-a", vol)
        self.assertNotIn("--all", vol)

    def test_docker_missing_is_skipped(self):
        r = cl.step_docker(self.cfg(which=lambda n: None))
        self.assertEqual(self.run_.calls, [])
        self.assertEqual(r["freed"], 0)

    def test_docker_freed_parsed(self):
        self.run_.outputs["docker"] = "Total reclaimed space: 1.5GB\n"
        r = cl.step_docker(self.cfg())
        self.assertEqual(r["freed"], 3 * 1_500_000_000)

    def test_journal_command_and_parse(self):
        self.run_.outputs["journalctl"] = "Vacuuming done, freed 2.0M of archived journals from /var/log/journal.\n"
        r = cl.step_journal(self.cfg())
        self.assertEqual(self.run_.argvs(), [["journalctl", "--vacuum-time=7d"]])
        self.assertEqual(r["freed"], 2 * 1024 * 1024)

    def test_nothing_run_in_dry_run(self):
        cl.step_docker(self.cfg(dry_run=True))
        cl.step_journal(self.cfg(dry_run=True))
        self.assertEqual(self.run_.calls, [])
        self.assertTrue(any("docker image prune" in p for p in self.printed))
        self.assertTrue(any("journalctl --vacuum-time=7d" in p for p in self.printed))


class TmpStep(Base):
    def setUp(self):
        super().setUp()
        self.top = os.path.join(self.tmp, "claude-1000")
        self.outside = os.path.join(self.root, "outside.txt")
        put(self.outside, "keep me", age_days=30)

    def test_old_removed_recent_kept(self):
        put(self.top + "/old.txt", "x" * 100, age_days=3)
        put(self.top + "/recent.txt", age_days=0)
        put(self.top + "/read-recently.txt", age_days=5, atime_days=0)    # mtime old but atime new: kept
        r = cl.step_tmp(self.cfg())
        self.assertFalse(os.path.exists(self.top + "/old.txt"))
        self.assertTrue(os.path.exists(self.top + "/recent.txt"))
        self.assertTrue(os.path.exists(self.top + "/read-recently.txt"))
        self.assertEqual(r["freed"], 100)

    def test_symlink_removed_as_link_target_untouched(self):
        os.makedirs(self.top)
        link = self.top + "/lnk"
        os.symlink(self.outside, link)
        age(link, 5)
        cl.step_tmp(self.cfg())
        self.assertFalse(os.path.lexists(link))
        self.assertEqual(open(self.outside).read(), "keep me")

    def test_symlinked_dir_not_followed(self):
        victim = os.path.join(self.root, "victim")
        put(victim + "/f.txt", "keep", age_days=30)
        os.makedirs(self.top)
        os.symlink(victim, self.top + "/dirlink")
        age(self.top + "/dirlink", 5)
        cl.step_tmp(self.cfg())
        self.assertTrue(os.path.exists(victim + "/f.txt"))
        self.assertFalse(os.path.lexists(self.top + "/dirlink"))

    def test_symlinked_top_dir_refused(self):
        victim = os.path.join(self.root, "victim")
        put(victim + "/f.txt", "keep", age_days=30)
        os.symlink(victim, self.top)
        cl.step_tmp(self.cfg())
        self.assertTrue(os.path.exists(victim + "/f.txt"))

    def test_empty_subdir_removed_top_kept(self):
        put(self.top + "/sub/deeper/old.txt", age_days=4)
        put(self.top + "/keepdir/recent.txt", age_days=0)
        cl.step_tmp(self.cfg())
        self.assertFalse(os.path.exists(self.top + "/sub"))
        self.assertTrue(os.path.exists(self.top + "/keepdir/recent.txt"))
        self.assertTrue(os.path.isdir(self.top))

    def test_top_dir_kept_even_when_everything_goes(self):
        put(self.top + "/old.txt", age_days=4)
        cl.step_tmp(self.cfg())
        self.assertTrue(os.path.isdir(self.top))

    def test_other_tmp_entries_untouched(self):
        put(self.tmp + "/other/old.txt", age_days=40)
        put(self.tmp + "/claude-notes.txt", age_days=40)
        cl.step_tmp(self.cfg())
        self.assertTrue(os.path.exists(self.tmp + "/other/old.txt"))
        self.assertTrue(os.path.exists(self.tmp + "/claude-notes.txt"))

    def test_dry_run_changes_nothing(self):
        put(self.top + "/sub/old.txt", "x" * 10, age_days=4)
        r = cl.step_tmp(self.cfg(dry_run=True))
        self.assertTrue(os.path.exists(self.top + "/sub/old.txt"))
        self.assertEqual(r["freed"], 10)


class ResultsStep(Base):
    def results(self, kind="chat-results"):
        return os.path.join(self.home, ".claude", kind)

    def test_old_removed_recent_kept(self):
        for kind in ("chat-results", "job-results"):
            put(self.results(kind) + "/old.txt", "x" * 50, age_days=15)
            put(self.results(kind) + "/new.txt", age_days=3)
        r = cl.step_results(self.cfg())
        for kind in ("chat-results", "job-results"):
            self.assertFalse(os.path.exists(self.results(kind) + "/old.txt"))
            self.assertTrue(os.path.exists(self.results(kind) + "/new.txt"))
        self.assertEqual(r["freed"], 100)

    def test_symlinked_results_dir_refused(self):
        victim = os.path.join(self.root, "victim")
        put(victim + "/old.txt", "keep", age_days=40)
        os.makedirs(os.path.join(self.home, ".claude"))
        os.symlink(victim, self.results())
        cl.step_results(self.cfg())
        self.assertTrue(os.path.exists(victim + "/old.txt"))

    def test_symlinked_dot_claude_refused(self):
        victim = os.path.join(self.root, "victim")
        put(victim + "/chat-results/old.txt", "keep", age_days=40)
        os.symlink(victim, os.path.join(self.home, ".claude"))
        cl.step_results(self.cfg())
        self.assertTrue(os.path.exists(victim + "/chat-results/old.txt"))

    def test_symlink_entry_not_followed_and_not_removed(self):
        outside = os.path.join(self.root, "o.txt")
        put(outside, "keep", age_days=40)
        os.makedirs(self.results())
        os.symlink(outside, self.results() + "/lnk")
        age(self.results() + "/lnk", 40)
        cl.step_results(self.cfg())
        self.assertEqual(open(outside).read(), "keep")

    def test_only_active_people(self):
        self.write_people([{"name": ME, "linux_user": ME, "status": "pending"}])
        put(self.results() + "/old.txt", age_days=40)
        cl.step_results(self.cfg())
        self.assertTrue(os.path.exists(self.results() + "/old.txt"))

    def test_dry_run_changes_nothing(self):
        put(self.results() + "/old.txt", "x" * 7, age_days=40)
        r = cl.step_results(self.cfg(dry_run=True))
        self.assertTrue(os.path.exists(self.results() + "/old.txt"))
        self.assertEqual(r["freed"], 7)


class BackupsStep(Base):
    def test_old_dir_removed_recent_kept(self):
        put(self.backup + "/20260101T000000Z/etc/f", "x" * 20)
        age(self.backup + "/20260101T000000Z", 20)
        put(self.backup + "/20261120T000000Z/etc/f")
        age(self.backup + "/20261120T000000Z", 3)
        r = cl.step_backups(self.cfg())
        self.assertFalse(os.path.exists(self.backup + "/20260101T000000Z"))
        self.assertTrue(os.path.exists(self.backup + "/20261120T000000Z/etc/f"))
        self.assertGreaterEqual(r["freed"], 20)

    def test_symlink_entry_never_followed(self):
        victim = os.path.join(self.root, "victim")
        put(victim + "/f", "keep")
        os.symlink(victim, self.backup + "/lnk")
        age(self.backup + "/lnk", 40)
        cl.step_backups(self.cfg())
        self.assertTrue(os.path.exists(victim + "/f"))

    def test_dry_run(self):
        put(self.backup + "/old/f")
        age(self.backup + "/old", 20)
        cl.step_backups(self.cfg(dry_run=True))
        self.assertTrue(os.path.exists(self.backup + "/old/f"))


class Worktrees(Base):
    def setUp(self):
        super().setUp()
        base = os.path.join(self.home, "Documents", "code", "vylo")
        self.remote = os.path.join(self.root, "remote.git")
        git("init", "-q", "--bare", "-b", "main", self.remote)
        self.repo = os.path.join(base, "voiceLog")
        os.makedirs(base)
        git("clone", "-q", self.remote, self.repo)
        git("checkout", "-q", "-b", "main", cwd=self.repo)
        put(self.repo + "/a.txt", "a")
        git("add", ".", cwd=self.repo)
        git("commit", "-q", "-m", "init", cwd=self.repo)
        git("push", "-q", "origin", "main", cwd=self.repo)
        self.wts = os.path.join(base, ".worktrees")
        os.makedirs(self.wts)

    def add(self, name, nested=False):
        path = os.path.join(self.repo, ".claude", "worktrees", name) if nested else os.path.join(self.wts, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        git("worktree", "add", "-q", "-b", "br-" + name, path, "main", cwd=self.repo)
        self.settle(path)
        return path

    def settle(self, path, days=10):
        """Make everything in the worktree old, as a forgotten one would be."""
        for root, dirs, files in os.walk(path):
            for n in files + dirs:
                age(os.path.join(root, n), days)
        age(path, days)

    def listed(self):
        return git("worktree", "list", "--porcelain", cwd=self.repo)

    def test_clean_pushed_old_removed(self):
        wt = self.add("clean")
        r = cl.step_worktrees(self.cfg())
        self.assertFalse(os.path.exists(wt))
        self.assertNotIn(wt, self.listed())
        self.assertEqual((r["removed"], r["kept"]), (1, 0))
        self.assertTrue(os.path.isdir(self.repo))

    def test_nested_claude_worktrees_candidates(self):
        wt = self.add("nested", nested=True)
        cl.step_worktrees(self.cfg())
        self.assertFalse(os.path.exists(wt))

    def test_git_runs_as_the_user_never_bare(self):
        self.add("clean")
        cl.step_worktrees(self.cfg())
        gits = [(a, u) for a, u in self.run_.calls if a[0] == "git"]
        self.assertTrue(gits)
        self.assertTrue(all(u == ME for _, u in gits))
        for a, _ in gits:
            self.assertNotIn("--force", a)
            self.assertNotIn("-f", a)

    def test_dirty_kept_and_reported_once(self):
        wt = self.add("dirty")
        put(wt + "/new.txt", "unsaved", age_days=10)
        self.settle(wt)
        r = cl.step_worktrees(self.cfg())
        self.assertTrue(os.path.exists(wt + "/new.txt"))
        self.assertEqual((r["removed"], r["kept"]), (0, 1))
        self.assertEqual(len(self.run_.notes), 1)
        user, argv = self.run_.notes[0]
        self.assertEqual(user, ME)
        self.assertEqual(argv[:3], ["/usr/local/bin/notify-me", "--source", "job:cleanup"])
        self.assertEqual(argv[3], "Old worktree kept because it has unsaved or unpushed work: %s. "
                                  "Push or remove it when you're done." % wt)
        cl.step_worktrees(self.cfg())
        self.assertEqual(len(self.run_.notes), 1)
        self.assertIn(wt, json.load(open(self.state))["reported"])

    def test_state_file_private(self):
        wt = self.add("dirty")
        put(wt + "/new.txt", age_days=10)
        self.settle(wt)
        cl.step_worktrees(self.cfg())
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o600)

    def test_young_dirty_kept_not_reported(self):
        wt = self.add("young")
        age(wt + "/.git", 2)
        put(wt + "/new.txt", age_days=10)
        self.settle(wt, days=10)
        age(wt + "/.git", 2)
        cl.step_worktrees(self.cfg())
        self.assertEqual(self.run_.notes, [])

    def test_unpushed_commit_kept_and_reported(self):
        wt = self.add("unpushed")
        put(wt + "/b.txt", "b")
        git("add", ".", cwd=wt)
        git("commit", "-q", "-m", "local only", cwd=wt)
        self.settle(wt)
        r = cl.step_worktrees(self.cfg())
        self.assertTrue(os.path.exists(wt + "/b.txt"))
        self.assertEqual((r["removed"], r["kept"]), (0, 1))
        self.assertEqual(len(self.run_.notes), 1)

    def test_recent_change_kept_not_reported(self):
        wt = self.add("busy")
        put(wt + "/a.txt", "a")                      # touched now, then committed state is unchanged -> clean
        os.utime(wt + "/a.txt", None)
        r = cl.step_worktrees(self.cfg())
        self.assertTrue(os.path.exists(wt))
        self.assertEqual((r["removed"], r["kept"]), (0, 1))
        self.assertEqual(self.run_.notes, [])

    def test_process_cwd_inside_kept(self):
        wt = self.add("inuse")
        os.makedirs(wt + "/sub", exist_ok=True)
        self.settle(wt)
        r = cl.step_worktrees(self.cfg(cwds=lambda: [os.path.realpath(wt) + "/sub"]))
        self.assertTrue(os.path.exists(wt))
        self.assertEqual((r["removed"], r["kept"]), (0, 1))
        self.assertEqual(self.run_.notes, [])

    def test_cwd_in_sibling_with_same_prefix_does_not_block(self):
        wt = self.add("a")
        r = cl.step_worktrees(self.cfg(cwds=lambda: [os.path.realpath(wt) + "-other"]))
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(r["removed"], 1)

    def test_standalone_clone_never_removed(self):
        clone = os.path.join(self.wts, "standalone")
        git("clone", "-q", self.remote, clone)
        self.settle(clone)
        cl.step_worktrees(self.cfg())
        self.assertTrue(os.path.isdir(clone))

    def test_user_not_on_the_box_skipped(self):
        self.write_people([{"name": "ghost", "linux_user": "ghost", "status": "pending"}])
        wt = self.add("clean")
        r = cl.step_worktrees(self.cfg())
        self.assertTrue(os.path.exists(wt))
        self.assertEqual((r["removed"], r["kept"]), (0, 0))

    def test_any_status_is_covered(self):
        self.write_people([{"name": ME, "linux_user": ME, "status": "pending"}])
        wt = self.add("clean")
        cl.step_worktrees(self.cfg())
        self.assertFalse(os.path.exists(wt))

    def test_dry_run_changes_nothing(self):
        wt = self.add("clean")
        d = self.add("dirty")
        put(d + "/new.txt", age_days=10)
        self.settle(d)
        r = cl.step_worktrees(self.cfg(dry_run=True))
        self.assertTrue(os.path.isdir(wt))
        self.assertEqual(r["removed"], 1)
        self.assertEqual(self.run_.notes, [])
        self.assertFalse(os.path.exists(self.state))
        self.assertTrue(any(wt in p for p in self.printed))

    def test_vanished_worktree_forgotten_in_state(self):
        d = self.add("dirty")
        put(d + "/new.txt", age_days=10)
        self.settle(d)
        cl.step_worktrees(self.cfg())
        git("worktree", "remove", "--force", d, cwd=self.repo)
        cl.step_worktrees(self.cfg())
        self.assertEqual(json.load(open(self.state))["reported"], {})


class WholeRun(Base):
    def test_a_failing_step_does_not_stop_the_rest(self):
        self.run_ = FakeRun(fail=("docker",))
        put(self.tmp + "/claude-1000/old.txt", age_days=5)
        s = cl.run_all(self.cfg(run=self.run_))
        self.assertFalse(os.path.exists(self.tmp + "/claude-1000/old.txt"))
        self.assertIn("journalctl", [a[0] for a in self.run_.argvs()])
        self.assertIn("docker", s["failed"])

    def test_summary_one_line_no_contents(self):
        put(self.tmp + "/claude-1000/old.txt", "SECRETCONTENT", age_days=5)
        s = cl.run_all(self.cfg())
        line = cl.summary_line(s, dry_run=False)
        self.assertEqual(len(line.splitlines()), 1)
        self.assertNotIn("SECRETCONTENT", line)
        for word in ("docker", "journal", "tmp", "results", "backups", "worktrees", "removed", "kept"):
            self.assertIn(word, line)

    def test_dry_run_changes_nothing_anywhere(self):
        put(self.tmp + "/claude-1000/old.txt", age_days=5)
        put(self.home + "/.claude/chat-results/old.txt", age_days=30)
        put(self.backup + "/old/f")
        age(self.backup + "/old", 30)

        def snap():
            out = []
            for r, ds, fs in os.walk(self.root):
                out += [os.path.join(r, n) for n in ds + fs]
            return sorted(out)
        before = snap()
        cl.run_all(self.cfg(dry_run=True))
        self.assertEqual(snap(), before)
        self.assertEqual(self.run_.calls and [a for a in self.run_.argvs() if a[0] in ("docker", "journalctl")], [])

    def test_exit_codes(self):
        with self.assertRaises(SystemExit) as e:
            cl.main(["--nonsense"])
        self.assertEqual(e.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
