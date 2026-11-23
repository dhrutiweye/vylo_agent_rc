"""Tests for agent_sessions.py and the agent-max-sessions CLI. No network.

  python3 -m unittest tests/test_sessions.py          (from the bundle directory)
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "..", "usr", "local", "lib", "claude-agent")
CLI = os.path.join(HERE, "..", "usr", "local", "bin", "agent-max-sessions")
sys.path.insert(0, LIB)
import agent_sessions as s  # noqa: E402

WHEELSEYE_POLICY = '{"permissions":{"disableBypassPermissionsMode":"disable"}}'


class TestConfiguredMax(unittest.TestCase):
    def test_default_is_one_when_file_empty(self):
        self.assertEqual(s.configured_max("a", ""), 1)

    def test_user_line_beats_default(self):
        self.assertEqual(s.configured_max("a", "default=1\na=5\n"), 5)
        self.assertEqual(s.configured_max("b", "default=1\na=5\n"), 1)

    def test_clamped_and_garbage_ignored(self):
        self.assertEqual(s.configured_max("a", "default=9"), 5)
        self.assertEqual(s.configured_max("a", "a=0"), 1)
        self.assertEqual(s.configured_max("a", "a=x\n# c\n"), 1)


class TestPolicy(unittest.TestCase):
    def test_policy_absent_allows(self):
        self.assertIs(s.policy_allows_multi(None), True)

    def test_policy_merge_allows(self):
        self.assertIs(s.policy_allows_multi('{"managedSourcesBehavior":"merge","permissions":{}}'), True)

    def test_policy_without_merge_blocks(self):
        self.assertIs(s.policy_allows_multi(WHEELSEYE_POLICY), False)

    def test_policy_broken_blocks(self):
        for text in ("", "{", "[]"):
            self.assertIs(s.policy_allows_multi(text), False, text)


class TestEffectiveMax(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.conf = os.path.join(self.tmp.name, "agent-sessions.conf")
        with open(self.conf, "w") as f:
            f.write("default=1\na=5\n")

    def test_effective_max_forced_to_one(self):
        with open(os.path.join(self.home, ".claude", "remote-settings.json"), "w") as f:
            f.write(WHEELSEYE_POLICY)
        self.assertEqual(s.effective_max("a", self.home, os.getuid(), self.conf), 1)

    def test_effective_max_personal_account(self):
        self.assertEqual(s.effective_max("a", self.home, os.getuid(), self.conf), 5)

    def test_cli_prints_number(self):
        with open(self.conf, "w") as f:
            f.write("default=5\n")
        env = dict(os.environ, AGENT_SESSIONS_CONF=self.conf, HOME=self.home)
        out = subprocess.run([sys.executable, CLI], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, "5\n")


GUARD_RULES = {"hooks": {"PreToolUse": [{"matcher": "Bash|mcp__.*|Edit|Write",
                                         "hooks": [{"type": "command", "command": "/usr/local/bin/claude-guard.sh"}]}]}}


class TestLockedRules(unittest.TestCase):
    """The box's rules copied into the person's own ~/.claude/settings.json, owned by root and immutable: the way to
    run the guard for someone whose org policy makes Claude skip the box's managed settings."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.path = os.path.join(self.home, ".claude", "settings.json")
        self.uid = os.getuid()

    def write(self, rules=GUARD_RULES, mode=0o644):
        with open(self.path, "w") as f:
            json.dump(rules, f)
        os.chmod(self.path, mode)

    def locked(self, root_uid=None, immutable=True):
        return s.box_rules_locked(self.home, self.uid, root_uid=self.uid if root_uid is None else root_uid,
                                  is_immutable=lambda fd: immutable)

    def test_locked_rules_found(self):
        self.write(); self.assertTrue(self.locked())

    def test_absent(self):
        self.assertFalse(self.locked())

    def test_not_immutable(self):
        self.write(); self.assertFalse(self.locked(immutable=False))

    def test_not_owned_by_root(self):
        self.write(); self.assertFalse(self.locked(root_uid=self.uid + 1))

    def test_writable_by_others(self):
        self.write(mode=0o666); self.assertFalse(self.locked())

    def test_no_guard_hook(self):
        self.write({"hooks": {}}); self.assertFalse(self.locked())
        self.write({"hooks": {"PreToolUse": [{"matcher": "Read", "hooks": [{"command": "/usr/local/bin/claude-guard.sh"}]}]}})
        self.assertFalse(self.locked())                                     # the guard must see Bash

    def test_symlink_rejected(self):
        real = os.path.join(self.tmp.name, "real.json")
        with open(real, "w") as f:
            json.dump(GUARD_RULES, f)
        os.symlink(real, self.path)
        self.assertFalse(self.locked())

    def test_org_policy_with_locked_rules_allows(self):
        conf = os.path.join(self.tmp.name, "c.conf")
        with open(conf, "w") as f:
            f.write("a=5\n")
        with open(os.path.join(self.home, ".claude", "remote-settings.json"), "w") as f:
            f.write(WHEELSEYE_POLICY)
        self.assertEqual(s.effective_max("a", self.home, self.uid, conf, locked=lambda h, u: True), 5)
        self.assertEqual(s.effective_max("a", self.home, self.uid, conf, locked=lambda h, u: False), 1)

    def test_unreadable_policy_means_one_even_with_locked_rules(self):
        conf = os.path.join(self.tmp.name, "c.conf")
        with open(conf, "w") as f:
            f.write("a=5\n")
        orig = s._read_policy
        s._read_policy = lambda home, uid: (True, None)                       # present, but unsafe or unreadable
        try:
            self.assertEqual(s.effective_max("a", self.home, self.uid, conf, locked=lambda h, u: True), 1)
            self.assertEqual(s.effective_max("a", self.home, self.uid, conf, locked=lambda h, u: False), 1)
        finally:
            s._read_policy = orig

    def test_policy_that_blocks_user_hooks_wins(self):
        conf = os.path.join(self.tmp.name, "c.conf")
        with open(conf, "w") as f:
            f.write("a=5\n")
        pol = os.path.join(self.home, ".claude", "remote-settings.json")
        for text in ('{"allowManagedHooksOnly": true}', '{"disableAllHooks": true}', "{"):
            with open(pol, "w") as f:
                f.write(text)
            self.assertEqual(s.effective_max("a", self.home, self.uid, conf, locked=lambda h, u: True), 1, text)


class TestOverrides(unittest.TestCase):
    def test_test_overrides_only_outside_the_installed_copy(self):
        env = {"AGENT_SESSIONS_CONF": "/tmp/x.conf", "HOME": "/tmp/h"}
        self.assertEqual(s.cli_inputs(env, "/usr/local/bin/agent-max-sessions", "/home/ann"), (s.CONF_PATH, "/home/ann"))
        self.assertEqual(s.cli_inputs(env, "/src/bundle/usr/local/bin/agent-max-sessions", "/home/ann"), ("/tmp/x.conf", "/tmp/h"))
        self.assertEqual(s.cli_inputs({}, "/src/bundle/usr/local/bin/agent-max-sessions", "/home/ann"), (s.CONF_PATH, "/home/ann"))


if __name__ == "__main__":
    unittest.main()
