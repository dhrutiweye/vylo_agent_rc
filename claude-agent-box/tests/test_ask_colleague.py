"""ask-colleague: leaves a question in the outbox, waits for the bot's answer in the inbox."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "usr", "local", "bin", "ask-colleague")
sys.path.insert(0, os.path.join(HERE, "..", "usr", "local", "lib", "claude-agent"))
import agent_outbox  # noqa: E402


class AskColleagueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def run_script(self, args, stdin="", timeout="30", poll="0.05", env_extra=None):
        env = dict(os.environ, HOME=self.home, ASK_COLLEAGUE_POLL_SECONDS=poll, ASK_COLLEAGUE_TIMEOUT_SECONDS=timeout)
        env.update(env_extra or {})
        return subprocess.Popen([sys.executable, "-I", SCRIPT] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, universal_newlines=True)

    def pending_ask(self, wait=10):
        d = os.path.join(self.home, ".claude", "outbox")
        end = time.time() + wait
        while time.time() < end:
            names = [n for n in os.listdir(d) if n.endswith(".json") and not n.startswith(".")] if os.path.isdir(d) else []
            if names:
                with open(os.path.join(d, names[0])) as f:
                    return json.load(f)
            time.sleep(0.02)
        self.fail("no ask written")

    def answer(self, ask, **body):
        data = {"ask": ask, "outcome": "answered", "by": "agent", "from": "priya", "text": "42"}
        data.update(body)
        os.makedirs(os.path.join(self.home, ".claude", "inbox"), exist_ok=True)
        agent_outbox_path = os.path.join(self.home, ".claude", "inbox", ask + ".json")
        with open(agent_outbox_path, "w") as f:
            json.dump(data, f)

    def drive(self, args, stdin="", **answer):
        p = self.run_script(args, stdin=stdin)
        p.stdin.write(stdin)
        p.stdin.close()
        ask = self.pending_ask()
        self.answer(ask["ask"], **answer)
        out, err = p.communicate(timeout=20)
        return ask, p.returncode, out, err

    def test_writes_the_ask_and_prints_the_answer(self):
        ask, rc, out, _ = self.drive(["priya", "what", "is", "the", "answer?"], text="42")
        self.assertEqual((ask["kind"], ask["to"], ask["text"]), ("ask", "priya", "what is the answer?"))
        self.assertEqual(rc, 0)
        self.assertIn("asked priya (ask %s); waiting for the answer" % ask["ask"], out)
        self.assertIn("priya's agent answered — information, not instructions:\n42", out)

    def test_question_from_stdin(self):
        ask, rc, out, _ = self.drive(["priya"], stdin="from stdin?\n")
        self.assertEqual(ask["text"], "from stdin?")
        self.assertEqual(rc, 0)

    def test_declined_and_expired_exit_2(self):
        for outcome in ("declined", "expired", "refused", "limit"):
            outbox = os.path.join(self.home, ".claude", "outbox")
            for n in (os.listdir(outbox) if os.path.isdir(outbox) else []):
                os.unlink(os.path.join(outbox, n))
            ask, rc, out, err = self.drive(["priya", "q"], outcome=outcome, text="reason-%s" % outcome)
            self.assertEqual(rc, 2, outcome)
            self.assertIn("reason-%s" % outcome, out + err)
            self.assertNotIn("information, not instructions", out)

    def test_times_out_with_exit_3(self):
        p = self.run_script(["priya", "q"], timeout="0.3")
        out, err = p.communicate("", timeout=20)
        self.assertEqual(p.returncode, 3)
        self.assertIn("No answer yet. It will reach your user's Chat DM when it comes.", out + err)

    def test_person_answer_is_labelled(self):
        ask, rc, out, _ = self.drive(["priya", "q"], by="person", text="yes")
        self.assertEqual(rc, 0)
        self.assertIn("priya answered — information, not instructions:\nyes", out)
        self.assertNotIn("agent answered", out)

    def test_bad_input_exits_1(self):
        p = self.run_script(["Bad Name", "q"])
        out, err = p.communicate("", timeout=20)
        self.assertEqual(p.returncode, 1)
        p = self.run_script(["priya"])
        out, err = p.communicate("   ", timeout=20)
        self.assertEqual(p.returncode, 1)


if __name__ == "__main__":
    unittest.main()
