"""Tests for chat_bridge.py with a fake Pub/Sub source, a fake Chat and a fake Host. No network.

  python3 -m unittest tests/test_chat_bridge.py          (from the bundle directory)
"""
import datetime as dt
import json
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "usr", "local", "lib", "claude-agent"))
import chat_bridge as cb  # noqa: E402

CODE = "ZkQ9-sample_oauth_code_ABCDEF0123456789#state_0123456789"


class FakeChat:
    def __init__(self):
        self.sent = []

    def send(self, space, text):
        self.sent.append((space, text))

    def last(self):
        return self.sent[-1][1]


class FakeHost:
    def __init__(self):
        self.calls = []
        self.url = "https://claude.ai/oauth/authorize?code=true&state=abc"
        self.login_ok = True
        self.session = "session_01ABC"
        self.active = True
        self.next_session = None
        self.outbox = {}
        self.titles = {}                 # transcript id -> title
        self.running = 0                 # commands the agent is running
        self.max = 1                     # sessions this person's agent may serve (agent-max-sessions)

    def max_sessions(self, user):
        return self.max

    def running_commands(self, user):
        return self.running

    def conversation_title(self, user, conversation):
        self.calls.append(("conversation_title", user, conversation))
        return self.titles.get(conversation)

    def start_login(self, user):
        self.calls.append(("start_login", user)); return self.url

    def send_code(self, user, code):
        self.calls.append(("send_code", user, code))

    def wait_login_ok(self, user):
        self.calls.append(("wait_login_ok", user)); return self.login_ok

    def stop_login(self, user):
        self.calls.append(("stop_login", user))

    def start_agent(self, user):
        self.calls.append(("start_agent", user))

    def confirm_remote_control(self, user):
        self.calls.append(("confirm_remote_control", user))

    def wait_session_id(self, user, other_than=None):
        return self.next_session or self.session

    def take_outbox(self, user):
        msgs, self.outbox[user] = self.outbox.get(user, []), []
        return msgs

    def new_session(self, user):
        self.calls.append(("new_session", user))

    def switch_session(self, user, sid):
        self.calls.append(("switch_session", user, sid))

    def restart_agent(self, user):
        self.calls.append(("restart", user))

    def agent_job(self, user, args):
        self.calls.append(("agent_job", user, tuple(args))); return 0, "no jobs"

    def service_active(self, user):
        return self.active

    def session_id(self, user):
        return self.session

    def names(self):
        return [c[0] for c in self.calls]


class FakeSource:
    def __init__(self, events):
        self.events, self.acked = events, []

    def pull(self):
        return list(self.events)

    def ack(self, ids):
        self.acked += ids


def classic(text, email="asha@co.test", space="spaces/DM1", dm=True, user="users/1", sender_type="HUMAN", etype="MESSAGE"):
    return {"type": etype, "space": {"name": space, "type": "DM" if dm else "ROOM", "singleUserBotDm": dm},
            "message": {"text": text, "argumentText": text, "sender": {"name": user, "email": email, "type": sender_type}},
            "user": {"name": user, "email": email}}


def addon(text, email="asha@co.test", space="spaces/DM1"):
    return {"chat": {"user": {"name": "users/1", "email": email},
                     "messagePayload": {"space": {"name": space, "spaceType": "DIRECT_MESSAGE"},
                                        "message": {"text": text, "sender": {"name": "users/1", "email": email}}}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.people = os.path.join(d, "people.json")
        with open(self.people, "w") as f:
            json.dump({"people": [
                {"name": "asha", "email": "asha@co.test", "linux_user": "asha", "status": "pending", "chat_space": None, "chat_user": None, "session_id": None},
                {"name": "ravi", "email": "ravi@co.test", "linux_user": "ravi", "status": "pending", "chat_space": None, "chat_user": None, "session_id": None},
            ]}, f)
        self.audit = os.path.join(d, "audit"); os.mkdir(self.audit)
        for u in ("asha", "ravi"):
            with open(os.path.join(self.audit, u + ".jsonl"), "w"):
                pass
        self.clock = [1_800_000_000.0]
        self.cfg = cb.Config({"PEOPLE_FILE": self.people, "AUDIT_DIR": self.audit, "NUDGE_MINUTES": "5", "LOGIN_TTL_SECONDS": "600"})
        self.store = cb.PeopleStore(self.people)
        self.chat, self.host = FakeChat(), FakeHost()
        self.b = cb.Bridge(self.cfg, self.store, self.chat, self.host, now=lambda: self.clock[0])

    def person(self, name="asha"):
        return self.store.get(name)

    def ts(self, offset_min=0):
        t = dt.datetime.fromtimestamp(self.clock[0] + offset_min * 60, dt.timezone.utc)
        return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def log(self, user, event, at_min=0, **kw):
        with open(os.path.join(self.audit, user + ".jsonl"), "a") as f:
            f.write(json.dumps({"ts": self.ts(at_min), "user": user, "event": event, **kw}) + "\n")

    def activate(self, name="asha"):
        self.store.update(name, status="active", chat_space="spaces/DM1", session_id="session_01ABC")


class TestParsing(unittest.TestCase):
    def test_classic_and_addon_shapes_normalise_the_same_way(self):
        a, b = cb.normalize(classic("hi")), cb.normalize(addon("hi"))
        for m in (a, b):
            self.assertEqual((m["kind"], m["space"], m["dm"], m["email"], m["text"]), ("message", "spaces/DM1", True, "asha@co.test", "hi"))

    def test_added_to_space_and_junk(self):
        self.assertEqual(cb.normalize(classic("", etype="ADDED_TO_SPACE"))["kind"], "added")
        for junk in (None, {}, {"type": "REMOVED_FROM_SPACE"}, {"type": "MESSAGE"}, "x", {"chat": {}}):
            self.assertIsNone(cb.normalize(junk))

    def test_room_is_not_a_dm_and_email_is_lowercased(self):
        m = cb.normalize(classic("hi", email="Asha@CO.test", dm=False))
        self.assertFalse(m["dm"]); self.assertEqual(m["email"], "asha@co.test")


class TestOnboarding(Base):
    def test_unknown_email_is_turned_away_and_nothing_runs(self):
        self.b.handle_event(classic("hi", email="stranger@co.test"))
        self.assertIn("registered", self.chat.last()); self.assertEqual(self.host.calls, [])

    def test_bot_senders_and_other_domains_and_missing_email_and_rooms(self):
        self.b.handle_event(classic("hi", sender_type="BOT")); self.assertEqual(self.chat.sent, [])
        self.b.handle_event(classic("hi", email="")); self.assertIn("can't see your email", self.chat.last())
        self.b.handle_event(classic("hi", dm=False)); self.assertIn("directly", self.chat.last())
        self.assertEqual(self.host.calls, [])
        b2 = cb.Bridge(cb.Config({"PEOPLE_FILE": self.people, "AUDIT_DIR": self.audit, "ALLOWED_EMAIL_DOMAIN": "other.test"}),
                       self.store, self.chat, self.host)
        b2.handle_event(classic("hi")); self.assertIn("company domain", self.chat.last()); self.assertEqual(self.host.calls, [])

    def test_hi_saves_chat_ids_starts_login_and_sends_the_url(self):
        self.b.handle_event(classic("hi"))
        p = self.person()
        self.assertEqual((p["status"], p["chat_space"], p["chat_user"]), ("login_started", "spaces/DM1", "users/1"))
        self.assertEqual(self.host.calls, [("start_login", "asha")])
        self.assertIn(self.host.url, self.chat.last()); self.assertIn("YOUR Claude account", self.chat.last())

    def test_hi_matching_is_by_email_not_by_chat_user_id(self):
        self.b.handle_event(classic("hi", email="ravi@co.test", space="spaces/DM2", user="users/2"))
        self.assertEqual(self.person("ravi")["chat_space"], "spaces/DM2"); self.assertIsNone(self.person("asha")["chat_space"])
        self.assertEqual(self.host.calls, [("start_login", "ravi")])

    def test_login_could_not_start(self):
        self.host.url = None
        self.b.handle_event(classic("hi"))
        self.assertEqual(self.person()["status"], "pending"); self.assertIn("couldn't start", self.chat.last())

    def test_full_flow_code_in_agent_started_link_sent(self):
        self.b.handle_event(classic("hi"))
        self.b.handle_event(classic(CODE))
        self.assertEqual(self.host.names(), ["start_login", "send_code", "wait_login_ok", "stop_login", "start_agent", "confirm_remote_control"])
        self.assertEqual(self.host.calls[1], ("send_code", "asha", CODE))
        p = self.person()
        self.assertEqual((p["status"], p["session_id"]), ("active", "session_01ABC"))
        self.assertIn("https://claude.ai/code/session_01ABC", self.chat.last()); self.assertIn("asha-agent", self.chat.last())

    def test_wrong_code_returns_to_pending_and_never_starts_the_agent(self):
        self.host.login_ok = False
        self.b.handle_event(classic("hi")); self.b.handle_event(classic(CODE))
        self.assertEqual(self.person()["status"], "pending"); self.assertNotIn("start_agent", self.host.names())
        self.assertIn("didn't work", self.chat.last())

    def test_expired_link_is_refused_without_typing_the_code(self):
        self.b.handle_event(classic("hi")); self.clock[0] += 601
        self.b.handle_event(classic(CODE))
        self.assertNotIn("send_code", self.host.names()); self.assertEqual(self.person()["status"], "pending")
        self.assertIn("expired", self.chat.last())

    def test_ordinary_text_during_login_is_not_typed_into_the_terminal(self):
        self.b.handle_event(classic("hi")); self.b.handle_event(classic("ok done, signed in"))
        self.assertNotIn("send_code", self.host.names()); self.assertIn("Paste the code", self.chat.last())

    def test_a_code_without_a_login_in_progress_is_ignored(self):
        self.b.handle_event(classic(CODE))
        self.assertEqual(self.host.calls, []); self.assertIn("don't know that one", self.chat.last())

    def test_hi_when_already_running_just_resends_the_link(self):
        self.activate(); self.b.handle_event(classic("hi"))
        self.assertEqual(self.host.calls, []); self.assertIn("claude.ai/code/session_01ABC", self.chat.last())

    def test_hi_when_active_but_service_down_signs_in_again(self):
        self.activate(); self.host.active = False; self.b.handle_event(classic("hi"))
        self.assertEqual(self.host.calls, [("start_login", "asha")])

    def test_session_id_not_available_yet(self):
        self.host.session = None
        self.b.handle_event(classic("hi")); self.b.handle_event(classic(CODE))
        self.assertEqual(self.person()["status"], "active"); self.assertIn("status", self.chat.last())

    def test_pasted_code_and_message_text_are_never_logged(self):
        records = []
        h = logging.Handler(); h.emit = records.append
        logging.getLogger("chat_bridge").addHandler(h); logging.getLogger("chat_bridge").setLevel(logging.DEBUG)
        self.addCleanup(logging.getLogger("chat_bridge").removeHandler, h)
        self.b.handle_event(classic("hi")); self.b.handle_event(classic(CODE)); self.b.handle_event(classic("hi", email="x@y.test"))
        self.assertFalse([r for r in records if CODE in r.getMessage()])


class TestStatusAndHelp(Base):
    def test_status_by_state(self):
        self.b.handle_event(classic("status")); self.assertIn("pending", self.chat.last()); self.assertIn("hi", self.chat.last())
        self.b.handle_event(classic("hi")); self.b.handle_event(classic("status")); self.assertIn("waiting for the code", self.chat.last())
        self.activate(); self.log("asha", "prompt", -3); self.b.handle_event(classic("status"))
        out = self.chat.last()
        self.assertIn("running", out); self.assertIn("session_01ABC", out); self.assertIn("last activity", out)

    def test_status_says_when_the_agent_is_down_and_when_it_is_waiting(self):
        self.activate(); self.host.active = False; self.log("asha", "notification", -20, ntype="permission_prompt")
        self.b.handle_event(classic("status"))
        self.assertIn("NOT running", self.chat.last()); self.assertIn("waiting for you", self.chat.last())

    def test_help_and_link(self):
        self.b.handle_event(classic("help")); self.assertIn("status", self.chat.last())
        self.b.handle_event(classic("link")); self.assertIn("No agent yet", self.chat.last())
        self.activate(); self.b.handle_event(classic("link")); self.assertIn("claude.ai/code/session_01ABC", self.chat.last())

    def test_added_to_space_greets(self):
        self.b.handle_event(classic("", etype="ADDED_TO_SPACE")); self.assertIn("Send \"hi\"", self.chat.last())


class TestNudges(Base):
    def setUp(self):
        super().setUp(); self.activate()

    def test_no_nudge_before_the_wait_and_one_after_and_not_repeated(self):
        self.log("asha", "notification", -4, ntype="idle_prompt")
        self.assertEqual(self.b.check_nudges(), 0)
        self.clock[0] += 120
        self.assertEqual(self.b.check_nudges(), 1)
        self.assertEqual(self.chat.sent[-1], ("spaces/DM1", "asha, your agent is waiting: https://claude.ai/code/session_01ABC"))
        self.assertEqual(self.b.check_nudges(), 0)
        self.assertEqual(len([s for s in self.chat.sent if "waiting" in s[1]]), 1)

    def test_schedule_is_5_then_5_later_then_every_30_and_never_denies(self):
        self.log("asha", "notification", 0, ntype="permission_prompt")
        sent = lambda: [s[1] for s in self.chat.sent if "waiting" in s[1]]
        for minutes, expect in [(4, 0), (5, 1), (9, 1), (10, 2), (39, 2), (40, 3), (70, 4), (71, 4)]:
            self.clock[0] = 1_800_000_000.0 + minutes * 60
            self.b.check_nudges()
            self.assertEqual(len(sent()), expect, "at %d min" % minutes)
        self.assertIn("still waiting (40 min)", sent()[2])
        self.assertTrue(sent()[0].endswith("session_01ABC"))

    def test_the_nudge_is_recorded_in_the_audit_log(self):
        self.log("asha", "notification", -20, ntype="permission_prompt"); self.b.check_nudges()
        rows = cb.read_tail(os.path.join(self.audit, "asha.jsonl"))
        self.assertEqual([r["event"] for r in rows], ["notification", "nudge"])

    def test_answered_waits_do_not_nudge(self):
        for answer in ("prompt", "tool", "stop"):
            self.log("asha", "notification", -30, ntype="idle_prompt"); self.log("asha", answer, -29)
        self.assertEqual(self.b.check_nudges(), 0)

    def test_a_new_wait_after_an_answered_one_nudges_again(self):
        self.log("asha", "notification", -40, ntype="idle_prompt"); self.b.check_nudges()
        self.log("asha", "prompt", -30); self.log("asha", "notification", -20, ntype="permission_prompt")
        self.assertEqual(self.b.check_nudges(), 1)

    def test_only_active_people_with_a_chat_space_are_nudged(self):
        self.log("ravi", "notification", -60, ntype="idle_prompt")           # ravi is pending
        self.store.update("asha", chat_space=None); self.log("asha", "notification", -60, ntype="idle_prompt")
        self.assertEqual(self.b.check_nudges(), 0); self.assertEqual(self.chat.sent, [])

    def test_a_missing_audit_log_is_fine(self):
        os.remove(os.path.join(self.audit, "asha.jsonl")); self.assertEqual(self.b.check_nudges(), 0)


class TestNudgesIgnoreJobs(Base):
    def test_a_scheduled_jobs_rows_neither_open_nor_clear_a_wait(self):
        self.activate()
        self.log("asha", "notification", -20, ntype="permission_prompt")
        self.log("asha", "tool", -15, job="my-issues"); self.log("asha", "stop", -14, job="my-issues")
        self.assertEqual(self.b.check_nudges(), 1)
        self.assertEqual(cb.waiting_since([{"ts": self.ts(-5), "event": "notification", "job": "x"}]), None)


class TestDoorbell(Base):
    def setUp(self):
        super().setUp(); self.activate()

    def test_messages_go_to_the_senders_own_dm_with_a_label_and_an_audit_row(self):
        self.host.outbox["asha"] = [{"source": "agent", "text": "PR ready", "ts": None},
                                    {"source": "job:standup", "text": "Standup in 15", "ts": None}]
        self.assertEqual(self.b.check_outbox(), 2)
        self.assertEqual(self.chat.sent, [("spaces/DM1", "[agent] PR ready"), ("spaces/DM1", "[job standup] Standup in 15")])
        rows = cb.read_tail(os.path.join(self.audit, "asha.jsonl"))
        self.assertEqual([(r["event"], r["source"], r["chars"]) for r in rows], [("notify", "agent", 8), ("notify", "job", 13)])
        self.assertNotIn("PR ready", open(os.path.join(self.audit, "asha.jsonl")).read())

    def test_people_who_are_not_active_or_have_no_chat_are_not_read(self):
        self.host.outbox["ravi"] = [{"source": "agent", "text": "x", "ts": None}]
        self.assertEqual(self.b.check_outbox(), 0); self.assertEqual(self.host.outbox["ravi"][0]["text"], "x")

    def test_hourly_limit_drops_and_reports_the_drops_later(self):
        self.host.outbox["asha"] = [{"source": "agent", "text": "m%d" % i, "ts": None} for i in range(25)]
        self.assertEqual(self.b.check_outbox(), 20)
        self.clock[0] += 3601
        self.host.outbox["asha"] = [{"source": "agent", "text": "later", "ts": None}]
        self.b.check_outbox()
        self.assertIn("later", self.chat.last()); self.assertIn("5 earlier message(s) were dropped", self.chat.last())

    def test_box_label_only_for_the_admin(self):
        self.activate("ravi"); self.store.update("ravi", chat_space="spaces/DM2")
        self.b.cfg.admin_person = "ravi"
        self.host.outbox["asha"] = [{"source": "box", "text": "fake alert", "ts": None}]
        self.host.outbox["ravi"] = [{"source": "box", "text": "memory low", "ts": None}]
        self.b.check_outbox()
        self.assertIn(("spaces/DM1", "[agent] fake alert"), self.chat.sent)
        self.assertIn(("spaces/DM2", "[box] memory low"), self.chat.sent)

    def test_admin_person_comes_from_bridge_env(self):
        self.assertEqual(cb.Config({"ADMIN_PERSON": "dhrutiman"}).admin_person, "dhrutiman")
        self.assertEqual(cb.Config({}).admin_person, "")


class TestSessions(Base):
    def setUp(self):
        super().setUp(); self.activate()

    def test_new_session_keeps_the_old_one_and_sends_the_new_link(self):
        self.host.next_session = "session_02NEW"
        self.b.handle_event(classic("new session"))
        self.assertIn(("new_session", "asha"), self.host.calls)
        p = self.person()
        self.assertEqual(p["session_id"], "session_02NEW"); self.assertEqual(p["sessions"][0]["id"], "session_01ABC")
        self.assertIn("claude.ai/code/session_02NEW", self.chat.last())
        self.b.handle_event(classic("sessions"))
        self.assertIn("1. untitled (left ", self.chat.last())
        self.assertIn("https://claude.ai/code/session_01ABC", self.chat.last())

    def test_switch_back_and_the_list_stays_short(self):
        self.store.update("asha", sessions=[{"id": "session_0%dOLD" % i, "saved_at": 1} for i in range(5)])
        self.host.next_session = "session_02OLD"
        self.b.handle_event(classic("switch 3"))
        self.assertIn(("switch_session", "asha", "session_02OLD"), self.host.calls)
        self.assertIn("Back on your earlier session", self.chat.last())
        ids = [s["id"] for s in self.person()["sessions"]]
        self.assertEqual(ids[0], "session_01ABC"); self.assertNotIn("session_02OLD", ids); self.assertLessEqual(len(ids), 5)

    def test_switch_that_cannot_reopen_says_so(self):
        self.store.update("asha", sessions=[{"id": "session_09OLD", "saved_at": 1}])
        self.host.next_session = "session_03OTHER"
        self.b.handle_event(classic("switch 1")); self.assertIn("could not be reopened", self.chat.last())

    def test_bad_numbers_and_inactive_people(self):
        self.b.handle_event(classic("switch 4")); self.assertIn("No session 4", self.chat.last())
        self.assertNotIn("switch_session", self.host.names())
        self.b.handle_event(classic("new session", email="ravi@co.test", space="spaces/DM2"))
        self.assertIn("No agent yet", self.chat.last()); self.assertNotIn("new_session", self.host.names())


CONV_A = "11111111-2222-4333-8444-555555555555"
CONV_B = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class TestConversationTitle(unittest.TestCase):
    def line(self, **e):
        return json.dumps(e) + "\n"

    def test_first_thing_the_person_typed_skipping_meta_and_command_tags(self):
        head = ("{cut line\n" + self.line(type="summary", summary="from another conversation")
                + self.line(type="user", isMeta=True, message={"role": "user", "content": "Caveat: meta"})
                + self.line(type="user", message={"role": "user", "content": "<command-name>/clear</command-name>"})
                + self.line(type="user", message={"role": "user", "content": [{"type": "text", "text": "Fix the\nlogin  bug"}]})
                + self.line(type="user", message={"role": "user", "content": "second question"}))
        self.assertEqual(cb.conversation_title(head, ""), "Fix the login bug")

    def test_a_given_title_wins_and_long_text_is_cut(self):
        head = self.line(type="user", message={"role": "user", "content": "x" * 200})
        self.assertEqual(len(cb.conversation_title(head, "")), 60)
        tail = self.line(type="custom-title", customTitle="Payments refactor") + "half a li"
        self.assertEqual(cb.conversation_title(head, tail), "Payments refactor")

    def test_a_compacted_conversations_summary_is_not_its_title(self):
        head = (json.dumps({"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": "This session is being continued from a previous conversation that ran out of context."}}) + "\n"
                + json.dumps({"type": "user", "message": {"role": "user", "content": "This session is being continued from a previous conversation. Summary: ..."}}) + "\n"
                + json.dumps({"type": "user", "message": {"role": "user", "content": "git issue 667"}}) + "\n")
        self.assertEqual(cb.conversation_title(head, ""), "git issue 667")

    def test_nothing_readable(self):
        self.assertIsNone(cb.conversation_title("", ""))
        self.assertIsNone(cb.conversation_title(self.line(type="user", message={"role": "user", "content": [{"type": "tool_result"}]}), ""))


class TestSessionNames(Base):
    def setUp(self):
        super().setUp(); self.activate()

    def test_titles_come_from_the_interactive_conversation_not_a_job(self):
        self.host.titles = {CONV_A: "Fix the login bug", CONV_B: "Daily Jira digest"}
        self.log("asha", "prompt", -10, session=CONV_A)
        self.log("asha", "prompt", -1, session=CONV_B, job="digest")      # a scheduled job's run: ignored
        self.b.handle_event(classic("sessions"))
        self.assertIn("now: Fix the login bug https://claude.ai/code/session_01ABC", self.chat.last())

    def test_new_session_saves_the_old_ones_title_and_takes_a_name(self):
        self.host.titles = {CONV_A: "Fix the login bug"}
        self.log("asha", "prompt", -10, session=CONV_A); self.log("asha", "stop", -9, session=CONV_A)
        self.host.next_session = "session_02NEW"
        self.b.handle_event(classic("new session Payments"))
        p = self.person()
        self.assertEqual((p["session_name"], p["sessions"][0]["title"]), ("Payments", "Fix the login bug"))
        self.assertIn('New session "Payments"', self.chat.last()); self.assertIn("(Fix the login bug)", self.chat.last())
        self.clock[0] += 60; self.host.session = "session_02NEW"
        self.b.handle_event(classic("sessions"))           # the old transcript's rows predate this session: not its title
        self.assertIn("now: Payments https://claude.ai/code/session_02NEW", self.chat.last())
        self.assertIn("1. Fix the login bug (left ", self.chat.last())

    def test_name_the_current_session_and_bad_names(self):
        self.b.handle_event(classic("name Login   bug"))
        self.assertEqual(self.person()["session_name"], "Login bug")
        for bad in ("name 12", "name all", "name " + "x" * 41, "new session 3"):
            self.b.handle_event(classic(bad)); self.assertIn("name", self.chat.last().lower())
        self.assertEqual(self.person()["session_name"], "Login bug")
        self.assertNotIn("new_session", self.host.names())

    def test_switch_by_name_or_word_and_ambiguity(self):
        self.store.update("asha", sessions=[
            {"id": "session_0AOLD", "saved_at": 1, "name": "Payments", "title": "Refactor checkout"},
            {"id": "session_0BOLD", "saved_at": 1, "name": None, "title": "Fix the login bug"},
            {"id": "session_0COLD", "saved_at": 1, "name": "Login page", "title": None}])
        self.b.handle_event(classic("switch login"))
        self.assertIn("2 sessions match that; use the number: 2, 3", self.chat.last())
        self.b.handle_event(classic("switch nothing-like-it")); self.assertIn("No saved session matches", self.chat.last())
        self.assertNotIn("switch_session", self.host.names())
        self.host.next_session = "session_0AOLD"
        self.b.handle_event(classic("switch payments"))
        self.assertIn(("switch_session", "asha", "session_0AOLD"), self.host.calls)
        p = self.person()
        self.assertEqual((p["session_name"], p["session_title"]), ("Payments", "Refactor checkout"))
        self.assertEqual([s["id"] for s in p["sessions"]], ["session_01ABC", "session_0BOLD", "session_0COLD"])
        self.host.next_session = "session_0BOLD"
        self.b.handle_event(classic("switch checkout"))     # a word from a title: none left matching
        self.assertIn("No saved session matches", self.chat.last())

    def test_switch_to_a_link_pasted_from_the_app(self):
        self.store.update("asha", sessions=[{"id": "session_0AOLD", "saved_at": 1, "name": "Payments", "title": None}])
        self.host.next_session = "session_01SyjXkJwyRk4N6LHcPCNK4t"
        self.b.handle_event(classic("switch <https://claude.ai/code/session_01SyjXkJwyRk4N6LHcPCNK4t>"))
        self.assertIn(("switch_session", "asha", "session_01SyjXkJwyRk4N6LHcPCNK4t"), self.host.calls)
        self.assertIn("Back on your earlier session", self.chat.last())
        self.assertEqual([s["id"] for s in self.person()["sessions"]], ["session_01ABC", "session_0AOLD"])
        self.host.calls.clear(); self.host.session = "session_01SyjXkJwyRk4N6LHcPCNK4t"
        self.b.handle_event(classic("switch session_01SyjXkJwyRk4N6LHcPCNK4t"))   # the one it is on: no restart
        self.assertIn("already on that session", self.chat.last()); self.assertNotIn("switch_session", self.host.names())
        for bad in ("switch https://evil.test/code/session_01ABCDEF", "switch session_01;rm -rf", "switch https://claude.ai/code/x"):
            self.b.handle_event(classic(bad))
        self.assertNotIn("switch_session", self.host.names())

    def test_a_prompt_typed_while_the_switch_is_still_waiting_counts_for_the_new_session(self):
        self.host.titles = {CONV_A: "Release notes for 2.3"}
        self.host.next_session = "session_01SyjXkJwyRk4N6LHcPCNK4t"
        def slow_wait(user, other_than=None):                 # the restart takes 3 minutes; the person types at minute 1
            self.log("asha", "prompt", 1, session=CONV_A)
            self.clock[0] += 180
            return self.host.next_session
        self.host.wait_session_id = slow_wait
        self.b.handle_event(classic("switch https://claude.ai/code/session_01SyjXkJwyRk4N6LHcPCNK4t"))
        self.host.session = "session_01SyjXkJwyRk4N6LHcPCNK4t"
        self.b.handle_event(classic("sessions"))
        self.assertIn("now: Release notes for 2.3 https://claude.ai/code/session_01SyjXkJwyRk4N6LHcPCNK4t", self.chat.last())

    def test_bare_switch_forget_and_name_explain_themselves(self):
        self.store.update("asha", sessions=[{"id": "session_0AOLD", "saved_at": 1, "name": "Payments", "title": None}])
        self.b.handle_event(classic("switch")); self.assertIn("1. Payments", self.chat.last())
        self.b.handle_event(classic("forget")); self.assertIn("forget all", self.chat.last())
        self.b.handle_event(classic("name")); self.assertIn("name payments refactor", self.chat.last())
        self.assertEqual(self.person()["sessions"][0]["id"], "session_0AOLD")
        self.assertNotIn("switch_session", self.host.names())

    def test_forget_one_or_all(self):
        self.store.update("asha", sessions=[{"id": "session_0%dOLD" % i, "saved_at": 1} for i in range(4)])
        self.b.handle_event(classic("forget 2"))
        self.assertEqual([s["id"] for s in self.person()["sessions"]], ["session_00OLD", "session_02OLD", "session_03OLD"])
        self.b.handle_event(classic("forget 9")); self.assertIn("forget N", self.chat.last())
        self.b.handle_event(classic("forget all"))
        self.assertEqual(self.person()["sessions"], [])
        self.assertEqual(self.person()["session_id"], "session_01ABC")
        self.assertIn("archive them there", self.chat.last())
        self.assertNotIn("switch_session", self.host.names()); self.assertNotIn("new_session", self.host.names())


class TestBusyCheck(Base):
    def setUp(self):
        super().setUp(); self.activate(); self.host.next_session = "session_02NEW"

    def restarts(self):
        return [c for c in self.host.calls if c[0] in ("new_session", "switch_session")]

    def test_an_idle_agent_restarts_at_once(self):
        self.log("asha", "prompt", -5, session=CONV_A); self.log("asha", "stop", -4, session=CONV_A)
        self.b.handle_event(classic("new session"))
        self.assertEqual(self.restarts(), [("new_session", "asha")])

    def test_a_request_in_progress_asks_first_and_yes_goes_ahead(self):
        self.log("asha", "prompt", -3, session=CONV_A); self.log("asha", "tool", -1, session=CONV_A)
        self.b.handle_event(classic("new session Payments"))
        self.assertEqual(self.restarts(), [])
        self.assertIn("working on a request (last step 1 min ago)", self.chat.last()); self.assertIn('Say "yes"', self.chat.last())
        self.b.handle_event(classic("yes"))
        self.assertEqual(self.restarts(), [("new_session", "asha")])
        self.assertEqual(self.person()["session_name"], "Payments")      # the name given with the first message is kept
        self.b.handle_event(classic("yes")); self.assertIn("Nothing is waiting", self.chat.last())

    def test_a_background_command_counts_after_the_turn_ended(self):
        self.log("asha", "stop", -1, session=CONV_A); self.host.running = 2
        self.b.handle_event(classic("new session"))
        self.assertIn("2 commands still running", self.chat.last()); self.assertEqual(self.restarts(), [])

    def test_an_unanswered_approval_counts_and_a_job_or_stale_turn_does_not(self):
        self.log("asha", "notification", -2, session=CONV_A, ntype="permission_prompt")
        self.b.handle_event(classic("new session")); self.assertIn("waiting for your approval", self.chat.last())
        self.log("asha", "stop", -1, session=CONV_A)
        self.log("asha", "tool", 0, session=CONV_B, job="digest")          # a scheduled job working is not the agent
        self.b.handle_event(classic("new session"))
        self.assertEqual(self.restarts(), [("new_session", "asha")])

    def test_a_turn_silent_for_an_hour_is_not_busy(self):
        self.log("asha", "prompt", -61, session=CONV_A)
        self.b.handle_event(classic("new session"))
        self.assertEqual(self.restarts(), [("new_session", "asha")])

    def test_the_question_expires_and_any_other_message_withdraws_it(self):
        self.host.running = 1
        self.b.handle_event(classic("new session")); self.clock[0] += 121
        self.b.handle_event(classic("yes")); self.assertIn("Nothing is waiting", self.chat.last())
        self.b.handle_event(classic("new session")); self.b.handle_event(classic("status"))
        self.b.handle_event(classic("yes")); self.assertIn("Nothing is waiting", self.chat.last())
        self.assertEqual(self.restarts(), [])

    def test_switch_asks_too(self):
        self.store.update("asha", sessions=[{"id": "session_0AOLD", "saved_at": 1, "name": "Payments", "title": None}])
        self.host.running = 1; self.host.next_session = "session_0AOLD"
        self.b.handle_event(classic("switch payments"))
        self.assertIn("Switching stops it", self.chat.last()); self.assertEqual(self.restarts(), [])
        self.b.handle_event(classic("y"))
        self.assertEqual(self.restarts(), [("switch_session", "asha", "session_0AOLD")])
        self.assertIn("Back on your earlier session", self.chat.last())

    def test_one_persons_question_is_not_answered_by_another(self):
        self.activate("ravi"); self.host.running = 1
        self.b.handle_event(classic("new session"))
        self.b.handle_event(classic("yes", email="ravi@co.test", space="spaces/DM2"))
        self.assertIn("Nothing is waiting", self.chat.last()); self.assertEqual(self.restarts(), [])


class TestPerSessionWaits(Base):
    """Several sessions per person: each session waits, is nudged and is busy on its own."""
    def setUp(self):
        super().setUp(); self.activate(); self.host.max = 5
        self.host.titles = {CONV_A: "Fix login", CONV_B: "Payments"}

    def nudges(self):
        return [s[1] for s in self.chat.sent if "waiting" in s[1]]

    def test_stop_in_other_session_does_not_clear(self):
        rows = [{"ts": self.ts(-10), "event": "notification", "session": CONV_A},
                {"ts": self.ts(-9), "event": "stop", "session": CONV_B}]
        self.assertEqual(set(cb.waits_by_session(rows)), {CONV_A})
        self.log("asha", "notification", -10, session=CONV_A, ntype="permission_prompt")
        self.log("asha", "stop", -9, session=CONV_B)
        self.assertEqual(self.b.check_nudges(), 1)
        self.assertIn("\u201cFix login\u201d", self.nudges()[0])

    def test_one_session_mode_keeps_any_later_row_clearing(self):
        rows = [{"ts": self.ts(-10), "event": "notification", "session": CONV_A},
                {"ts": self.ts(-9), "event": "stop", "session": CONV_B}]
        self.assertIsNone(cb.waiting_since(rows))
        self.host.max = 1
        self.log("asha", "notification", -10, session=CONV_A, ntype="permission_prompt")
        self.log("asha", "stop", -9, session=CONV_B)
        self.assertEqual(self.b.check_nudges(), 0)

    def test_each_waiting_session_nudged_once(self):
        self.log("asha", "notification", -6, session=CONV_A, ntype="idle_prompt")
        self.log("asha", "notification", -6, session=CONV_B, ntype="permission_prompt")
        self.assertEqual(self.b.check_nudges(), 2)
        self.assertEqual(self.b.check_nudges(), 0)
        texts = self.nudges()
        self.assertTrue(any("waiting in \u201cFix login\u201d" in t for t in texts), texts)
        self.assertTrue(any("waiting in \u201cPayments\u201d" in t for t in texts), texts)
        rows = cb.read_tail(os.path.join(self.audit, "asha.jsonl"))
        self.assertEqual([r["event"] for r in rows].count("nudge"), 2)

    def test_nudge_counts_are_per_session(self):
        self.log("asha", "notification", -6, session=CONV_A, ntype="idle_prompt")
        self.log("asha", "notification", -6, session=CONV_B, ntype="idle_prompt")
        self.b.check_nudges()
        self.clock[0] += 5 * 60                                              # 11 minutes: the second nudge is due
        self.assertEqual(self.b.check_nudges(), 2)
        self.assertEqual(self.b.check_nudges(), 0)
        self.assertEqual(len([t for t in self.nudges() if "still waiting in \u201c" in t and "(11 min)" in t]), 2)

    def test_nudge_links_the_agent_not_a_session(self):
        self.log("asha", "notification", -6, session=CONV_A, ntype="idle_prompt")
        self.b.check_nudges()
        self.assertTrue(self.nudges()[0].endswith("https://claude.ai/code"), self.nudges()[0])
        self.assertNotIn("session_01ABC", self.nudges()[0])

    def test_a_wait_left_for_8_hours_is_dropped(self):
        self.log("asha", "notification", -8 * 60 - 1, session=CONV_A, ntype="idle_prompt")
        self.assertEqual(self.b.check_nudges(), 0)

    def test_old_nudge_fields_are_honoured_once(self):
        self.host.max = 1
        self.log("asha", "notification", -6, ntype="idle_prompt")
        self.store.update("asha", nudged_for=self.ts(-6), nudge_count=1)     # nudged before this version shipped
        self.assertEqual(self.b.check_nudges(), 0)

    def test_busy_lists_every_session(self):
        self.log("asha", "notification", -2, session=CONV_B, ntype="permission_prompt")
        self.log("asha", "tool", -1, session=CONV_A)
        why = self.b._busy(self.person())
        self.assertIn("\u201cFix login\u201d is working on a request (last step 1 min ago)", why)
        self.assertIn("\u201cPayments\u201d is waiting for your approval in the Claude app", why)

    def test_busy_ignores_a_finished_session(self):
        self.log("asha", "tool", -3, session=CONV_A); self.log("asha", "stop", -2, session=CONV_A)
        self.log("asha", "tool", -1, session=CONV_B)
        self.assertEqual(self.b._busy(self.person()),
                         ["\u201cPayments\u201d is working on a request (last step 1 min ago)"])


class TestMultiSessionCommands(Base):
    """A person whose agent serves several sessions starts and picks them in the Claude app, not in Chat."""
    def setUp(self):
        super().setUp(); self.activate(); self.host.max = 5
        self.host.titles = {CONV_A: "Fix login", CONV_B: "Payments"}

    def restarts(self):
        return [c for c in self.host.calls if c[0] in ("new_session", "switch_session", "restart")]

    def test_retired_commands_point_to_the_app(self):
        self.store.update("asha", sessions=[{"id": "session_0AOLD", "saved_at": 1, "name": "x", "title": None}])
        for t in ("new session", "new session Payments", "switch 1", "switch", "name x", "forget 1", "forget all"):
            self.b.handle_event(classic(t))
            self.assertIn("Claude app", self.chat.last(), t)
            self.assertIn("several sessions", self.chat.last(), t)
        self.assertEqual(self.restarts(), [])
        self.assertIsNone(self.person().get("session_name"))

    def test_sessions_lists_recent_conversations_and_marks_active(self):
        self.log("asha", "prompt", -120, session=CONV_B); self.log("asha", "stop", -119, session=CONV_B)
        self.log("asha", "tool", -3, session=CONV_A)
        self.log("asha", "tool", -1, session=CONV_B, job="digest")          # a job is not a session
        self.b.handle_event(classic("sessions"))
        text = self.chat.last()
        lines = text.splitlines()
        a = next(l for l in lines if "Fix login" in l); b = next(l for l in lines if "Payments" in l)
        self.assertIn("active", a); self.assertNotIn("active", b)
        self.assertLess(lines.index(a), lines.index(b))                      # newest first
        self.assertIn("Claude app", text)

    def test_sessions_with_nothing_yet(self):
        self.b.handle_event(classic("sessions"))
        self.assertIn("No sessions yet", self.chat.last())

    def test_restart_when_idle(self):
        self.log("asha", "stop", -5, session=CONV_A)
        self.b.handle_event(classic("restart"))
        self.assertEqual(self.restarts(), [("restart", "asha")])
        self.assertIn("limit of 5", self.chat.last()); self.assertIn("restore", self.chat.last())

    def test_restart_when_busy_asks_and_yes_restarts(self):
        self.log("asha", "tool", -1, session=CONV_A)
        self.b.handle_event(classic("restart"))
        self.assertEqual(self.restarts(), [])
        self.assertIn("\u201cFix login\u201d is working on a request", self.chat.last())
        self.assertIn('Say "yes"', self.chat.last())
        self.b.handle_event(classic("yes"))
        self.assertEqual(self.restarts(), [("restart", "asha")])

    def test_help_lists_the_multi_session_commands(self):
        self.b.handle_event(classic("help"))
        h = self.chat.last()
        for w in ("sessions", "restart", "jobs"):
            self.assertIn(w, h)
        for w in ("new session", "switch"):
            self.assertNotIn(w, h)

    def test_one_session_people_keep_the_old_commands(self):
        self.host.max = 1
        self.b.handle_event(classic("help")); self.assertIn("new session", self.chat.last())
        self.b.handle_event(classic("new session")); self.assertEqual(self.restarts(), [("new_session", "asha")])


class TestJobCommands(Base):
    def setUp(self):
        super().setUp(); self.activate()

    def test_jobs_and_job_verbs_run_the_persons_own_agent_job(self):
        self.b.handle_event(classic("jobs")); self.b.handle_event(classic("job pause standup"))
        self.assertEqual([c for c in self.host.calls if c[0] == "agent_job"],
                         [("agent_job", "asha", ("list",)), ("agent_job", "asha", ("pause", "standup"))])

    def test_unknown_or_unsafe_job_commands_run_nothing(self):
        for t in ("job add x", "job pause ../../etc", "job remove", "job run A B"):
            self.b.handle_event(classic(t))
        self.assertFalse([c for c in self.host.calls if c[0] == "agent_job"])

    def test_help_lists_the_new_commands(self):
        self.b.handle_event(classic("help"))
        for w in ("new session", "switch N", "jobs"):
            self.assertIn(w, self.chat.last())


class TestLoop(Base):
    def test_every_event_is_acked_even_when_a_handler_fails_or_the_event_is_junk(self):
        def boom(space, text):
            raise RuntimeError("chat down")
        self.chat.send = boom
        src = FakeSource([("a1", classic("hi")), ("a2", {"nonsense": True}), ("a3", classic("hi", email="stranger@co.test"))])
        self.b.process(src)
        self.assertEqual(src.acked, ["a1", "a2", "a3"])

    def test_each_event_is_acked_before_its_handler_runs(self):
        # a slow command ("new session" waits up to 2 min) acked afterwards was redelivered and ran again, for ever
        order = []
        src = FakeSource([("a1", classic("help"))])
        src.ack = lambda ids: order.append(("ack", ids[0]))
        send = self.chat.send
        self.chat.send = lambda space, text: (order.append(("send", space)), send(space, text))
        self.b.process(src)
        self.assertEqual(order[0], ("ack", "a1"))
        self.assertIn(("send", "spaces/DM1"), order[1:])

    def test_a_redelivered_message_is_handled_once(self):
        ev = classic("help")
        ev["message"]["name"] = "spaces/DM1/messages/m1"
        sends = []
        self.chat.send = lambda space, text: sends.append(text)
        self.b.process(FakeSource([("a1", ev)]))
        n = len(sends)
        self.assertGreater(n, 0)
        self.b.process(FakeSource([("a2", ev)]))              # same Chat message, new ack id: a Pub/Sub redelivery
        self.assertEqual(len(sends), n)
        other = classic("help"); other["message"]["name"] = "spaces/DM1/messages/m2"
        self.b.process(FakeSource([("a3", other)]))           # a genuinely new message still runs
        self.assertGreater(len(sends), n)

    def test_a_failed_ack_still_handles_the_event(self):
        src = FakeSource([("a1", classic("help"))])
        def broken(ids):
            raise RuntimeError("pubsub down")
        src.ack = broken
        sends = []
        self.chat.send = lambda space, text: sends.append(text)
        self.b.process(src)
        self.assertTrue(sends)

    def test_config_defaults(self):
        c = cb.Config({})
        self.assertEqual((c.nudge_minutes, c.nudge_second_gap, c.nudge_repeat, c.login_ttl, c.link_base), (5.0, 5.0, 30.0, 600, "https://claude.ai/code/"))


class TestRestartWith(unittest.TestCase):
    """SystemHost._restart_with: the agent is started again even when stopping it overran (the 11 Nov outage)."""
    def run_restart(self, stop_raises):
        h, calls = cb.SystemHost(), []
        h._as = lambda user, args, timeout=30: calls.append(("as",) + tuple(args[:2]))
        def fake_run(cmd, **kw):
            calls.append(tuple(cmd[:2]))
            if cmd[:2] == ["systemctl", "stop"] and stop_raises:
                raise cb.subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        real, cb.subprocess.run = cb.subprocess.run, fake_run
        try:
            h.new_session("asha")
        finally:
            cb.subprocess.run = real
        return calls

    def test_a_slow_stop_still_ends_in_a_start(self):
        for slow in (False, True):
            calls = self.run_restart(slow)
            self.assertEqual(calls, [("systemctl", "stop"), ("as", "sh", "-c"), ("systemctl", "reset-failed"), ("systemctl", "start")])


class TestConfirmRemoteControl(unittest.TestCase):
    """SystemHost.confirm_remote_control against a scripted tmux pane (no tmux, no sleeping)."""
    def host(self, panes):
        h = cb.SystemHost(); h.sent = []
        it = iter(panes)
        def fake_as(user, args, timeout=30):
            class R: pass
            r = R(); r.returncode = 0; r.stderr = ""
            if args[:2] == ["tmux", "capture-pane"]:
                r.stdout = next(it, panes[-1])
            else:
                h.sent.append(args); r.stdout = ""
            return r
        h._as = fake_as
        return h

    def run_confirm(self, panes):
        h = self.host(panes)
        real = cb.time.sleep; cb.time.sleep = lambda s: None
        try:
            return h, h.confirm_remote_control("asha", timeout=30)
        finally:
            cb.time.sleep = real

    def test_answers_the_first_run_question_once(self):
        h, ok = self.run_confirm(["starting...", "Remote Control\nEnable Remote Control? (y/n)"])
        self.assertTrue(ok)
        self.assertEqual(h.sent, [["tmux", "send-keys", "-t", "agent", "y", "Enter"]])

    def test_does_nothing_when_the_agent_is_already_connected(self):
        h, ok = self.run_confirm(["Single session\nContinue coding at https://claude.ai/code/session_01XYZ"])
        self.assertFalse(ok); self.assertEqual(h.sent, [])


class TestStore(Base):
    def test_update_persists_and_keeps_other_people(self):
        self.store.update("asha", status="active"); fresh = cb.PeopleStore(self.people)
        self.assertEqual(fresh.get("asha")["status"], "active"); self.assertEqual(fresh.get("ravi")["status"], "pending")

    def test_missing_file_is_empty(self):
        self.assertEqual(cb.PeopleStore(os.path.join(self.tmp.name, "nope.json")).all(), [])


if __name__ == "__main__":
    unittest.main()
