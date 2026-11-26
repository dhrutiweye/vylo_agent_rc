"""Tests for the secret check (agent_secrets.py). No network, no systemd.

  python3 -m unittest tests/test_secrets.py          (from the bundle directory)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "usr", "local", "lib", "claude-agent"))
import agent_secrets as sec  # noqa: E402

SECRETS = [
    "sk-" + "aB3dE5gH7jK9mN1pQ3sT5vW7",
    "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dF5gH7",
    "github_pat_" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dF5gH7jK9m",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "xoxb-123-456-abc",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dF",
]


class TestLooksSecret(unittest.TestCase):
    def test_each_secret_shape_is_caught(self):
        for s in SECRETS:
            with self.subTest(shape=s[:8]):
                self.assertTrue(sec.looks_secret(s))
                self.assertTrue(sec.looks_secret("here is the thing you asked for: " + s + " thanks"))

    def test_ordinary_text_passes(self):
        for s in ["Please review the pull request and tell me what changed.",
                  "https://github.com/WeyeTech/voiceLog/pull/778",
                  "a" * 8 + "0123456789abcdef" * 2 + "01234567",   # 40 hex, a git SHA
                  "123e4567-e89b-12d3-a456-426614174000",
                  "VOIC-410",
                  "abcdefghijklmnopqrstuvwxyzabcdef",
                  ""]:
            with self.subTest(text=s[:20]):
                self.assertFalse(sec.looks_secret(s))


if __name__ == "__main__":
    unittest.main()
