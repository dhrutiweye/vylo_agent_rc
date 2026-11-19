"""Tests for agent_sessions.py and the agent-max-sessions CLI. No network.

  python3 -m unittest tests/test_sessions.py          (from the bundle directory)
"""
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


class TestOverrides(unittest.TestCase):
    def test_test_overrides_only_outside_the_installed_copy(self):
        env = {"AGENT_SESSIONS_CONF": "/tmp/x.conf", "HOME": "/tmp/h"}
        self.assertEqual(s.cli_inputs(env, "/usr/local/bin/agent-max-sessions", "/home/ann"), (s.CONF_PATH, "/home/ann"))
        self.assertEqual(s.cli_inputs(env, "/src/bundle/usr/local/bin/agent-max-sessions", "/home/ann"), ("/tmp/x.conf", "/tmp/h"))
        self.assertEqual(s.cli_inputs({}, "/src/bundle/usr/local/bin/agent-max-sessions", "/home/ann"), (s.CONF_PATH, "/home/ann"))


if __name__ == "__main__":
    unittest.main()
