#!/usr/bin/env python3
"""Chat bridge: onboarding, status and nudges for the claude-agent box, over Google Chat.

A colleague has no shell on the box. They send "hi" to the Vylo Agent bot in a DM; the bridge matches
the sender's Google email to a person an admin registered with add-colleague.sh, runs the Claude
sign-in for that person's Linux user in a hidden tmux pty, sends the sign-in URL back, takes the pasted
code, types it in, starts claude-agent@<name> and sends the agent's session link. It also answers
`status` / `help`, and posts "<name>, your agent is waiting: <link>" when an agent has waited on its
user longer than NUDGE_MINUTES.

It is also each agent's doorbell: messages an agent or one of its scheduled jobs leaves with notify-me
(~/.claude/outbox, see agent_outbox.py) are posted to that person's own DM, at most OUTBOX_PER_HOUR an hour.

And it manages the one live session per person: `new session`, `sessions`, `switch N`; and jobs: `jobs`,
`job pause|resume|remove|run NAME` (run as that person's agent-job).

Tasks from Chat: any other text from an active person is a task for their agent. The bridge never runs Claude: it
posts "On it (task #N)" in a new thread, writes the request to ~/.claude/chat-requests/<ref>.json and starts
agent-chat-exec as that person in a capped systemd unit (Host.launch_run). The run's result comes back through the
outbox (kind "result", its ref) and is posted in the task's thread; a reply in that thread resumes the same headless
conversation. Task state lives on disk (chat_state.py), so a result still finds its thread after a bridge restart.

Asking a colleague: an agent's ask-colleague leaves {"kind": "ask", "to": NAME, ...} in its outbox. The bridge checks
NAME against people.json (not yourself, known, active), the daily limits and the secret check, then asks the asker's
person for a yes in a new thread of its own. A yes starts a read-only answer run in the colleague's account (ref = the
ask id); "answer" goes back at once, anything else opens a thread for the colleague, who answers there or says "do
it" (a normal run, ref "d" + id[1:]). The answer is written to the asker's ~/.claude/inbox/<ask>.json, where the
waiting ask-colleague picks it up; check_asks DMs it to the asker if nobody read it within ASK_DM_FALLBACK_SECONDS,
expires unapproved questions and prunes old ones. Every step is audited (metadata only) in both people's files.

Events arrive on a Cloud Pub/Sub pull subscription (no inbound port); replies go out through the Chat
REST API with the service-account key. All outside effects sit behind three small interfaces so the
logic is tested with fakes (tests/test_chat_bridge.py):

    EventSource  pull() -> [(ack_id, event_dict)] ; ack([ack_id])
    Chat         send(space, text, thread=None, new_thread=False) -> thread name
    Host         start_login / send_code / wait_login_ok / stop_login / start_agent /
                 wait_session_id / service_active / logged_in / session_id / take_outbox /
                 new_session / switch_session / agent_job / launch_run /
                 write_inbox / inbox_taken / remove_inbox

Config: environment (systemd EnvironmentFile=/etc/claude-agent/bridge.env). No secrets in code.
Pasted sign-in codes and message text are never logged.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque

from agent_outbox import REF_RE, label
from agent_secrets import looks_secret
from chat_state import ChatState

log = logging.getLogger("chat_bridge")

CODE_RE = re.compile(r"^[A-Za-z0-9_\-#.:~+/=]{16,}$")      # an OAuth code: one long token, no spaces
LITELLM_KEY_RE = re.compile(r"^sk-[A-Za-z0-9_-]{16,200}$")  # a LiteLLM key, pasted alone; checked before CODE_RE
SET_LITELLM_KEY = "/usr/local/bin/set-litellm-key.sh"
HI_WORDS = {"hi", "hello", "hey", "start", "login", "relogin"}
WAIT_CLEARS = {"prompt", "tool", "stop", "session_end", "session_start"}
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{6,100}$")
LINKED_SESSION_RE = re.compile(r"^<?(?:https://claude\.ai/code/)?(session_[A-Za-z0-9]{6,92})/?>?$")   # Chat may wrap a link in <>
CONVERSATION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")   # a local transcript id
JOB_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
SESSION_NAME_MAX = 40
YES_WORDS = {"yes", "y"}
CONFIRM_SECONDS = 120         # how long a "your agent is busy" question waits for a yes
BUSY_STALE_SECONDS = 3600     # a turn with no audit row for this long is a crashed one, not a busy one
WAIT_MAX_SECONDS = 8 * 3600   # several sessions: a wait this old is a session closed in the app, not one to nudge
HELP = ('hi — sign in / set up your agent\nstatus — is it running, last activity\nlink — your agent\'s link\n'
        'new session [NAME] — start a fresh conversation (the current one is kept)\n'
        'name NAME — name the conversation you are in\nsessions — your recent sessions\n'
        'switch N or switch NAME — go back to one from that list\n'
        'switch LINK — move your agent onto a session link copied from the Claude app\nforget N or forget all — drop from that list\n'
        'jobs — your scheduled jobs\njob pause|resume|remove|run NAME — manage one\nhelp — this list\n'
        'anything else — a task for your agent; it answers in a thread, reply there to add to it\n'
        '(Or chat with the agent itself in the Claude app. Ask it to set up reminders and jobs.)')
HELP_MULTI = ('hi — sign in / set up your agent\nstatus — is it running, last activity\n'
              'sessions — your recent conversations, and which are active\n'
              'restart — restart your agent (asks first if it is busy)\n'
              'jobs — your scheduled jobs\njob pause|resume|remove|run NAME — manage one\nhelp — this list\n'
              'anything else — a task for your agent; it answers in a thread, reply there to add to it\n'
              '(Your agent runs several sessions: start, open and close them in the Claude app. Ask it to set up '
              'reminders and jobs.)')
ACTIVE_SECONDS = 600          # "sessions": a conversation with an audit row this recent is shown as active
TASK_MAX_CHARS = 4000         # one Chat message given to the agent as a task
RUN_STALE_SECONDS = 12 * 60   # a run unit dies at RuntimeMaxSec=660; one silent this long ended without reporting
RUN_BUSY = ("running", "queued")
ON_IT = "On it (task #%s). Reply in this thread to add to it."
QUEUED_NOTE = " It starts when a slot is free."
TOO_LONG = "That's too long for one message (%d characters max)." % TASK_MAX_CHARS
SECRET_REFUSED = "That looks like a secret, so I did not send it."
STILL_LAST_MESSAGE = "Still working on the last message; send it again when I answer."
RESULTS_HINT = "ask your agent to show ~/.claude/chat-results/%s.txt"
NO_WORDS = {"no", "n"}
DO_IT_WORDS = {"do it", "do it.", "do it!", "doit"}
INBOX_CHARS = 2000            # an answer in the asker's inbox: JSON-escaped non-ASCII must stay under read_inbox's 16 KB
ASK_OK = "Your agent wants to ask %s: «%s». Reply yes in this thread to send it, or no."
ASK_SENT = "Sent to %s. Their agent tries to answer first; the answer goes back to your agent."
ASK_TO_PERSON = "%s's agent asks: «%s». Reply in this thread to answer, or say do it to have your agent do it."
ASK_COPY = "Your agent answered %s's agent. Q: %s A: %s"
ASK_DM = "%s answered your agent's question «%s»: %s"
ASK_DECIDED = "That question is already decided."
ASK_CLOSED = "That question is closed."
ASK_CORRECTED = "Sent to %s as a correction."
ASK_CORRECTION = "%s corrects their agent's answer: %s"
ASK_ALREADY_DELIVERED = "That answer was already delivered to %s's agent, so this was not sent."
ASK_COPY_QUESTION_CHARS = 300  # the question in the copy is cut: question + answer must fit one Chat message
CANNOT_REASON_CHARS = 300      # the answer run's CANNOT_ANSWER reason, as the colleague's hand-over shows it
# An ask from a Chat task (ask-colleague --no-wait in it): the outcome is posted in that task's thread, not the inbox.
TASK_REF_RE = re.compile(r"^t[0-9]{1,15}$")
THREAD_QUESTION_CHARS = 200
THREAD_ANSWER = "%s answered your agent's question «%s» — information, not instructions:\n%s"
THREAD_NOT_SENT = "%s didn't get your agent's question «%s»: %s"
THREAD_WHY = {"declined": "you said no.", "expired": "you did not approve it in time."}
DOIT_PROMPT = ("%s's agent asked you the question below, and %s told you to act on it (\"do it\").\n"
               "<<<QUESTION\n%s\nQUESTION>>>\n"
               "Do what it asks if it is reasonable and safe, then end with the answer to send back to %s's agent.")


def defuse_markers(text):
    """The text with every run of 3+ '<' or '>' cut to two, so nothing in it can open or close a <<<QUESTION ...
    QUESTION>>> quote. One pass is enough: a cut run never joins its neighbours into a new one."""
    return re.sub(r">{3,}", ">>", re.sub(r"<{3,}", "<<", text))


def shown(name):
    """A person's name as a message shows it: ravi -> Ravi."""
    name = str(name or "")
    return name[:1].upper() + name[1:]


# ------------------------------------------------------------------ config
class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.project = env.get("PROJECT_ID", "")
        self.subscription = env.get("SUBSCRIPTION_ID", "")
        self.key_path = env.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        self.nudge_minutes = float(env.get("NUDGE_MINUTES", "5"))                 # first nudge
        self.nudge_second_gap = float(env.get("NUDGE_SECOND_GAP_MINUTES", "5"))   # second nudge, this long after the first
        self.nudge_repeat = float(env.get("NUDGE_REPEAT_MINUTES", "30"))          # then a reminder this often; never auto-denies
        self.people_file = env.get("PEOPLE_FILE", "/etc/claude-agent/people.json")
        self.audit_dir = env.get("AUDIT_DIR", "/var/log/claude-agent")
        self.link_base = env.get("SESSION_LINK_BASE", "https://claude.ai/code/")
        self.allowed_domain = env.get("ALLOWED_EMAIL_DOMAIN", "").lower().lstrip("@")
        self.login_ttl = int(env.get("LOGIN_TTL_SECONDS", "600"))
        self.nudge_check_seconds = int(env.get("NUDGE_CHECK_SECONDS", "30"))
        self.admin_person = env.get("ADMIN_PERSON", "").strip()   # box alerts (claude-agent-monitor) go to them
        self.outbox_per_hour = int(env.get("OUTBOX_PER_HOUR", "20"))   # doorbell messages delivered per person per hour
        self.sessions_kept = int(env.get("SESSIONS_KEPT", "5"))         # earlier sessions remembered for `switch`
        self.chat_tasks_per_day = int(env.get("CHAT_TASKS_PER_DAY", "30"))
        self.chat_runs_at_once = int(env.get("CHAT_RUNS_AT_ONCE", "2"))
        self.asks_per_day = int(env.get("ASKS_PER_DAY", "10"))
        self.asks_received_per_day = int(env.get("ASKS_RECEIVED_PER_DAY", "20"))
        self.ask_approval_seconds = int(env.get("ASK_APPROVAL_SECONDS", "7200"))
        self.ask_dm_fallback_seconds = int(env.get("ASK_DM_FALLBACK_SECONDS", "120"))
        self.chat_result_chars = int(env.get("CHAT_RESULT_CHARS", "1500"))
        self.chat_state_file = env.get("CHAT_STATE_FILE", "/var/lib/claude-agent-chat/state.json")


# ------------------------------------------------------------------ people store (shared with add-colleague.sh)
class PeopleStore:
    """people.json under the same lock file add-colleague.sh / remove-colleague.sh use."""

    def __init__(self, path):
        self.path = path
        self.lock_path = os.path.join(os.path.dirname(path) or ".", "people.lock")

    def _locked(self):
        f = open(self.lock_path, "a+")
        fcntl.flock(f, fcntl.LOCK_EX)
        return f

    def _read(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {"people": []}

    def all(self):
        return self._read()["people"]

    def find_email(self, email):
        email = (email or "").lower()
        return next((p for p in self.all() if p["email"].lower() == email), None)

    def get(self, name):
        return next((p for p in self.all() if p["name"] == name), None)

    def update(self, name, **fields):
        lock = self._locked()
        try:
            d = self._read()
            for p in d["people"]:
                if p["name"] == name:
                    p.update(fields)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f, indent=2)
            os.chmod(tmp, 0o640)
            os.replace(tmp, self.path)
        finally:
            lock.close()


# ------------------------------------------------------------------ event parsing
def normalize(event):
    """Chat sends two shapes: the classic app event and the Workspace-add-on event. Return one dict
    {kind, space, dm, user, email, bot, text} or None. kind is 'message' or 'added'."""
    if not isinstance(event, dict):
        return None
    if "chat" in event:                                    # add-on shape
        chat = event["chat"]
        payload = chat.get("messagePayload") or {}
        if payload:
            kind, msg, space = "message", payload.get("message") or {}, payload.get("space") or {}
        elif "addedToSpacePayload" in chat:
            kind, msg, space = "added", {}, (chat["addedToSpacePayload"] or {}).get("space") or {}
        else:
            return None
        user = chat.get("user") or msg.get("sender") or {}
    else:                                                  # classic shape
        etype = event.get("type")
        msg = event.get("message") or {}
        space = event.get("space") or msg.get("space") or {}
        user = msg.get("sender") or event.get("user") or {}
        if etype == "MESSAGE":
            kind = "message"
        elif etype == "ADDED_TO_SPACE":
            kind = "added"
        else:
            return None
    if not space.get("name"):
        return None
    text = (msg.get("argumentText") or msg.get("text") or "").strip()
    return {
        "kind": kind,
        "space": space["name"],
        "dm": bool(space.get("singleUserBotDm")) or space.get("type") in ("DM", "DIRECT_MESSAGE")
              or space.get("spaceType") == "DIRECT_MESSAGE",
        "user": user.get("name", ""),
        "email": (user.get("email") or "").lower(),
        "bot": user.get("type") == "BOT",
        "text": text,
        "id": msg.get("name") or "",                       # spaces/X/messages/Y: the same on a Pub/Sub redelivery
        "thread": (msg.get("thread") or {}).get("name") or "",   # spaces/X/threads/Z, "" when absent
    }


# ------------------------------------------------------------------ audit log helpers
def _parse_ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc).timestamp()


# Lines Claude Code writes as the user's turn that the person never typed: command tags, reminders, and the summary a
# compacted conversation is continued from.
NOT_TYPED = ("<", "This session is being continued from a previous conversation")


def _ago(seconds):
    return "under a minute" if seconds < 60 else "%d min" % (seconds // 60)


def short_text(s, width=60):
    s = " ".join(CONTROL_RE.sub(" ", s).split())
    return s if len(s) <= width else s[: width - 1].rstrip() + "…"


def conversation_title(head, tail, width=60):
    """A readable label for a Claude Code transcript (.jsonl), given its first and last bytes: a title it was given
    (/rename) if any, else the first thing the person typed. None when there is nothing to show."""
    def rows(text):
        for line in text.splitlines():           # the cut line at either end does not parse and is skipped
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                yield e
    for e in reversed(list(rows(tail))):
        for kind, key in (("custom-title", "customTitle"), ("ai-title", "aiTitle")):
            if e.get("type") == kind and isinstance(e.get(key), str) and e[key].strip():
                return short_text(e[key], width)
    for e in rows(head):
        if e.get("type") != "user" or e.get("isMeta") or e.get("isSidechain") or e.get("isCompactSummary"):
            continue
        content = (e.get("message") or {}).get("content")
        if isinstance(content, list):            # tool results have no "text" parts, so they come out empty
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
        if isinstance(content, str) and content.strip() and not content.lstrip().startswith(NOT_TYPED):
            return short_text(content, width)    # "<command-name>", "<system-reminder>": not the person's words
    return None


def read_tail(path, max_bytes=65536):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    rows = []
    for line in data.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def waits_by_session(rows, per_session=True):
    """{session: (iso, epoch)} of each session's oldest unanswered 'agent is waiting' notification. A session is the
    transcript id on the row ("" for a row without one). per_session=False is the one-session agent: only one
    conversation is ever live, so any later row from any conversation answers the wait, and everything is ""."""
    open_at = {}
    for e in rows:
        if e.get("job"):                  # a scheduled job's own headless run: not the interactive agent
            continue
        key = e.get("session") if per_session and isinstance(e.get("session"), str) else ""
        ev = e.get("event")
        if ev == "notification" and key not in open_at:
            try:
                open_at[key] = (e["ts"], _parse_ts(e["ts"]))
            except (KeyError, ValueError):
                continue
        elif ev in WAIT_CLEARS:
            open_at.pop(key, None)
    return open_at


def waiting_since(rows):
    """(iso, epoch) of the oldest unanswered 'agent is waiting' notification for a one-session agent, or None."""
    return waits_by_session(rows, per_session=False).get("")


# ------------------------------------------------------------------ the bridge
class Bridge:
    def __init__(self, cfg, store, chat, host, now=time.time, state=None):
        self.cfg, self.store, self.chat, self.host, self.now = cfg, store, chat, host, now
        self.state = state if state is not None else ChatState(cfg.chat_state_file, now)
        self.run_lock = threading.Lock()   # handle_event and check_outbox (nudger thread) both start runs
        self.sent = {}                   # user -> deque of delivery times (the hourly doorbell limit)
        self.dropped = {}                # user -> messages dropped over the limit, reported with the next one
        self.seen = deque(maxlen=500)    # Chat message names already handled: a redelivery is ignored
        self.pending = {}                # user -> (handler, arg, expires): a restart waiting for "yes" while busy

    # ---- helpers
    def link(self, sid):
        return self.cfg.link_base + sid if sid else None

    def _audit(self, user, event, **extra):
        path = os.path.join(self.cfg.audit_dir, user + ".jsonl")
        if not os.path.exists(path):
            return
        row = {"ts": dt.datetime.fromtimestamp(self.now(), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
               "user": user, "event": event, **extra}
        try:
            with open(path, "a") as f:
                f.write(json.dumps(row) + "\n")
        except OSError:
            log.warning("could not append audit event for %s", user)

    # ---- one pull cycle. Each event is acked BEFORE it is handled: "new session" takes up to two minutes, far past
    # the subscription's ack deadline, and acking afterwards had Pub/Sub redeliver it, which ran it again, for ever.
    # A command is therefore run at most once; one lost to a crash mid-handler is simply sent again by the person.
    def process(self, source):
        for ack_id, event in source.pull():
            try:
                source.ack([ack_id])
            except Exception:                              # noqa: BLE001 - a redelivery is caught by self.seen
                log.exception("ack failed")
            try:
                self.handle_event(event)
            except Exception:                              # noqa: BLE001 - keep the loop alive
                log.exception("handler failed")

    # ---- events
    def handle_event(self, event):
        m = normalize(event)
        if m is None or m["bot"]:
            return
        if m["id"]:
            if m["id"] in self.seen:
                log.info("ignoring a redelivered Chat message")
                return
            self.seen.append(m["id"])
        if m["kind"] == "added":
            self.chat.send(m["space"], 'Hi! I\'m the Vylo Agent bot. Send "hi" here to set up your coding agent, or "help".')
            return
        if not m["dm"]:
            self.chat.send(m["space"], "Please message me directly (a DM) — sign-in codes must not be pasted in a room.")
            return
        if not m["email"]:
            log.warning("event without a sender email: cannot identify the user")
            self.chat.send(m["space"], "I can't see your email in Google Chat, so I can't tell who you are. Ask your admin.")
            return
        domain = m["email"].rsplit("@", 1)[-1]
        if self.cfg.allowed_domain and domain != self.cfg.allowed_domain:
            self.chat.send(m["space"], "Sorry, this bot is only for people on the company domain.")
            return
        person = self.store.find_email(m["email"])
        if person is None:
            log.info("unknown sender %s", m["email"])
            self.chat.send(m["space"], "I don't have you registered yet. Ask your admin to add you, then say hi again.")
            return
        self.store.update(person["name"], chat_space=m["space"], chat_user=m["user"])
        person = self.store.get(person["name"])
        text, low = m["text"], m["text"].strip().lower()
        user = person["linux_user"]
        # Routing order (design §3): the key (and, below, the sign-in code: only a login_started person, who never
        # reaches the thread lookup) first; then the thread the message is in; then the commands; then a new task.
        if LITELLM_KEY_RE.match(text.strip()):              # before everything: a key is never an OAuth code or a task
            self.pending.pop(user, None)
            self.on_litellm_key(person, m["space"], text.strip())
            return
        if person["status"] == "active" and m["thread"]:
            try:                                            # every DM has a thread: a state error must not stop the commands
                found = self.state.item_by_thread(user, m["thread"])
                closed = None if found else self._closed_ask_thread(user, m["thread"])
            except Exception as e:                          # noqa: BLE001 - logged without the text, then the commands
                log.warning("thread lookup failed for %s: %s", user, e.__class__.__name__)
                found = closed = None
            if found and self._on_thread(person, m["space"], found, text):
                self.pending.pop(user, None)
                return
            if closed:
                self.pending.pop(user, None)                # a late yes/answer/"do it"/correction: never a command or a task
                if text.strip():
                    self._reply(m["space"], closed, m["thread"])
                return
        if low in YES_WORDS:
            self.on_yes(person, m["space"])
            return
        self.pending.pop(user, None)                        # anything else withdraws an unanswered "are you sure"
        if low in HI_WORDS:
            self.on_hi(person, m["space"])
        elif low == "status":
            self.on_status(person, m["space"])
        elif low in ("help", "?"):
            self.on_help(person, m["space"])
        elif low == "link":
            self.on_link(person, m["space"])
        elif self._max_sessions(person["linux_user"]) > 1 and self._multi_command(person, m["space"], low):
            pass
        elif low == "new session" or low.startswith("new session "):
            self.on_new_session(person, m["space"], text.strip()[len("new session"):].strip())
        elif low in ("sessions", "switch"):                # a bare "switch" wants the list to pick from
            self.on_sessions(person, m["space"])
        elif low == "forget":
            self.chat.send(m["space"], 'Say "forget N" with a number from "sessions", or "forget all".')
        elif low == "name":
            self.chat.send(m["space"], "Say name, then the name: name payments refactor")
        elif low.startswith("name "):
            self.on_name(person, m["space"], text.strip()[len("name"):].strip())
        elif low.startswith("switch "):
            self.on_switch(person, m["space"], text.strip()[len("switch"):].strip())
        elif low.startswith("forget "):
            self.on_forget(person, m["space"], low[len("forget"):].strip())
        elif low == "jobs" or low.startswith("job "):
            self.on_jobs(person, m["space"], low.split())
        elif person["status"] == "login_started" and CODE_RE.match(text):
            self.on_code(person, m["space"], text)
        elif person["status"] == "login_started":
            self.chat.send(m["space"], 'Paste the code from the sign-in page here, or say "hi" to get a fresh link.')
        elif person["status"] == "active":
            self.on_task(person, m["space"], text)
        else:
            self.chat.send(m["space"], 'I don\'t know that one. Say "help" for what I can do; chat with your agent in the Claude app.')

    def on_hi(self, person, space):
        name, user = person["name"], person["linux_user"]
        if person["status"] == "active" and self.host.service_active(user) and self.host.logged_in(user):
            self.chat.send(space, "You're all set. Your agent: %s" % (self.link(person.get("session_id")) or "starting up, say status in a minute"))
            return
        url = self.host.start_login(user)
        if not url:
            log.error("could not start sign-in for %s", name)
            self.chat.send(space, "I couldn't start the sign-in just now. Try again in a minute; if it keeps failing, tell your admin.")
            return
        fields = {"status": "login_started", "login_started_at": self.now()}
        if person["status"] == "active":       # an expired login (or a stopped agent): on_code restarts a running one
            fields["relogin"] = True
        self.store.update(name, **fields)
        self.chat.send(space, "Hi %s! 1) Open this link and sign in with YOUR Claude account. 2) Paste the code it shows you back here "
                              "(as a message to me).\nSigning in also turns on Remote Control for your Claude account, so you can use "
                              "your agent from the Claude app.\n%s\nThe link works for about %d minutes." % (name, url, self.cfg.login_ttl // 60))

    def on_code(self, person, space, code):
        name, user = person["name"], person["linux_user"]
        if self.now() - float(person.get("login_started_at") or 0) > self.cfg.login_ttl:
            self.host.stop_login(user)
            self.store.update(name, status="pending")
            self.chat.send(space, 'That sign-in link expired. Say "hi" and I\'ll send a new one.')
            return
        self.host.send_code(user, code)
        ok = self.host.wait_login_ok(user)
        self.host.stop_login(user)
        if not ok:
            self.store.update(name, status="pending")
            self.chat.send(space, 'That code didn\'t work. Say "hi" to try again with a fresh link.')
            return
        if person.get("relogin") and self.host.service_active(user):
            self.host.restart_agent(user)            # still running on the expired login: Remote Control needs the new one
        else:
            self.host.start_agent(user)
        self.host.confirm_remote_control(user)       # the first run asks "Enable Remote Control? (y/n)"
        sid = self.host.wait_session_id(user)
        self.store.update(name, status="active", session_id=sid, activated_at=self.now(), relogin=False)
        if sid:
            self.chat.send(space, "You're signed in. Your agent \"%s-agent\" is running. Open it in the Claude app: %s\n"
                                  'Say "status" any time.' % (name, self.link(sid)))
        else:
            self.chat.send(space, "You're signed in and your agent is starting. Say \"status\" in a minute for its link.")

    def on_litellm_key(self, person, space, key):
        """Their own LiteLLM key, pasted alone in the DM. The key goes to set-litellm-key.sh on stdin and is never
        logged, audited or repeated; only the outcome is."""
        user = person["linux_user"]
        result = self.host.set_litellm_key(user, key)
        log.info("litellm key from %s: %s", user, result)
        self._audit(user, "litellm_key", result=result)
        if result == "saved":
            self.chat.send(space, "Saved your LiteLLM key; your agent now uses it for Jira. Start a new session in the "
                                  "Claude app (or /mcp, then reconnect, in one already open). You can delete your "
                                  "message with the key from this chat.")
        elif result == "rejected":
            self.chat.send(space, "The gateway did not accept that key, so nothing changed. Check it is your staging "
                                  "LiteLLM key from Sarkaar and paste it again, on its own.")
        else:
            self.chat.send(space, "I couldn't save your key just now; nothing changed. Try again in a few minutes, "
                                  "or tell your admin.")

    def on_status(self, person, space):
        name, user = person["name"], person["linux_user"]
        lines = ["%s: %s" % (name, person["status"])]
        if person["status"] == "active":
            running = self.host.service_active(user)
            sid = self.host.session_id(user) or person.get("session_id")
            lines.append("agent: %s" % ("running" if running else "NOT running (your admin can check it)"))
            if sid:
                lines.append("link: %s" % self.link(sid))
            rows = read_tail(os.path.join(self.cfg.audit_dir, user + ".jsonl"))
            if rows:
                lines.append("last activity: %s UTC" % rows[-1]["ts"][:16].replace("T", " "))
            w = waiting_since(rows)
            if w:
                lines.append("your agent has been waiting for you since %s UTC" % w[0][:16].replace("T", " "))
        elif person["status"] == "login_started":
            lines.append("waiting for the code from the sign-in page")
        else:
            lines.append('say "hi" to sign in')
        self.chat.send(space, "\n".join(lines))

    def on_link(self, person, space):
        sid = self.host.session_id(person["linux_user"]) or person.get("session_id")
        self.chat.send(space, self.link(sid) if sid and person["status"] == "active" else 'No agent yet: say "hi".')

    def on_help(self, person, space):
        self.chat.send(space, HELP_MULTI if self._max_sessions(person["linux_user"]) > 1 else HELP)

    # ---- several sessions per person: the Claude app opens, picks and closes them; Chat only lists and restarts
    def _multi_command(self, person, space, low):
        """True when the message was a session command for a several-sessions person, and was answered."""
        words = low.split()
        if low in ("sessions",):
            self.on_sessions_multi(person, space)
        elif low == "restart":
            self.on_restart(person, space)
        elif low.startswith("new session") or (words and words[0] in ("switch", "name", "forget")):
            self.chat.send(space, "Your agent runs several sessions now. Start or pick one from %s-agent in the Claude "
                                  "app: %s" % (person["name"], self.cfg.link_base.rstrip("/")))
        else:
            return False
        return True

    def _recent_conversations(self, user):
        """[(transcript id, epoch of its newest row)] of the interactive agent, newest first."""
        newest = {}
        for e in read_tail(os.path.join(self.cfg.audit_dir, user + ".jsonl")):
            conv = e.get("session")
            if e.get("job") or not isinstance(conv, str) or not CONVERSATION_RE.match(conv):
                continue
            try:
                newest[conv] = _parse_ts(e["ts"])
            except (KeyError, ValueError):
                continue
        return sorted(newest.items(), key=lambda kv: -kv[1])

    def on_sessions_multi(self, person, space):
        if not self._need_active(person, space):
            return
        user, app = person["linux_user"], self.cfg.link_base.rstrip("/")
        recent = self._recent_conversations(user)[: self.cfg.sessions_kept]
        if not recent:
            self.chat.send(space, "No sessions yet. Start one from %s-agent in the Claude app: %s" % (person["name"], app))
            return
        lines = ["Your recent sessions (open them in the Claude app: %s):" % app]
        for i, (conv, at) in enumerate(recent, 1):
            title = self._title(user, conv) or "untitled"
            if self.now() - at < ACTIVE_SECONDS:
                when = "active"
            else:
                when = "last used %s" % dt.datetime.fromtimestamp(at, dt.timezone.utc).strftime("%d %b %H:%M UTC")
            lines.append("%d. %s (%s)" % (i, title, when))
        self.chat.send(space, "\n".join(lines))

    def on_restart(self, person, space, arg=None, confirmed=False):
        if not self._need_active(person, space):
            return
        user = person["linux_user"]
        if not confirmed and self._ask_if_busy(person, space, self.on_restart, None, "Restarting"):
            return
        self.chat.send(space, "Restarting your agent; this takes a minute...")
        self.host.restart_agent(user)
        self._audit(user, "session_switch", how="restart")
        self.chat.send(space, "Your agent restarted. Sessions that were open come back only if Claude can restore "
                              "them, within your limit of %d; any others have ended. Open them in the Claude app: %s"
                       % (self._max_sessions(user), self.cfg.link_base.rstrip("/")))

    # ---- one live session per person: start a fresh one, name it, list, go back, forget
    # A session is shown by its name (given in Chat) or else its title: the first thing the person typed in it, read
    # from the transcript the audit log points at. people.json keeps, per person: session_id, session_name,
    # session_title, session_started_at, and sessions = [{id, saved_at, name, title}], newest first.
    def _need_active(self, person, space):
        if person["status"] != "active":
            self.chat.send(space, 'No agent yet: say "hi".')
            return False
        return True

    @staticmethod
    def _clean_name(raw):
        """(name, None) or (None, why not)."""
        name = " ".join((raw or "").split())
        if not name:
            return None, None
        if CONTROL_RE.search(name) or len(name) > SESSION_NAME_MAX:
            return None, "A session name is one line of at most %d characters." % SESSION_NAME_MAX
        if name.isdigit() or name.lower() == "all":
            return None, 'Pick a name that is not a number or "all": those mean something to switch and forget.'
        return name, None

    def _live_title(self, person):
        """Title of the conversation the agent is in now, or None. The newest audit row from the interactive agent
        (not a scheduled job) names its transcript; a row older than the current session belongs to the last one."""
        since = float(person.get("session_started_at") or 0)
        for e in reversed(read_tail(os.path.join(self.cfg.audit_dir, person["linux_user"] + ".jsonl"))):
            conv = e.get("session")
            if e.get("job") or not isinstance(conv, str) or not CONVERSATION_RE.match(conv):
                continue
            try:
                if since and _parse_ts(e["ts"]) < since:
                    return None
            except (KeyError, ValueError):
                continue
            try:
                return self.host.conversation_title(person["linux_user"], conv)
            except Exception:                              # noqa: BLE001 - a label is never worth failing a command
                log.exception("could not read a session title")
                return None
        return None

    @staticmethod
    def _label(name, title):
        if name and title:
            return "%s: %s" % (name, title)
        return name or title or "untitled"

    def _leaving(self, person, sid):
        """The list entry for the session the agent is about to leave."""
        return {"id": sid, "saved_at": int(self.now()), "name": person.get("session_name"),
                "title": self._live_title(person) or person.get("session_title")}

    def _remember(self, sessions, entry):
        if entry and entry.get("id"):
            sessions = [s for s in sessions if s["id"] != entry["id"]]
            sessions.insert(0, entry)
        return sessions[: self.cfg.sessions_kept]

    def _max_sessions(self, user):
        try:
            return int(self.host.max_sessions(user))
        except Exception:                                  # noqa: BLE001 - unknown means today's one session
            log.exception("could not read the session limit")
            return 1

    def _title(self, user, conv):
        """The conversation's title, or None. Never fails a command."""
        if not isinstance(conv, str) or not CONVERSATION_RE.match(conv):
            return None
        try:
            return self.host.conversation_title(user, conv)
        except Exception:                                  # noqa: BLE001
            log.exception("could not read a session title")
            return None

    def _busy(self, person):
        """Why restarting the agent now would cut work off, as short phrases; [] when it is idle. Two signals: the
        newest audit row of the interactive agent (a request still running, or an approval prompt nobody answered),
        and the commands it still has running, which a background command keeps doing after its turn has ended.
        With several sessions the newest row of EACH session counts, and each phrase names its session."""
        user, why = person["linux_user"], []
        multi = self._max_sessions(user) > 1
        seen = set()
        for e in reversed(read_tail(os.path.join(self.cfg.audit_dir, user + ".jsonl"))):
            ev = e.get("event")
            if e.get("job") or ev not in ("prompt", "tool", "notification", "stop", "session_start", "session_end"):
                continue
            key = e.get("session") if multi and isinstance(e.get("session"), str) else ""
            if key in seen:
                continue
            seen.add(key)
            try:
                age = self.now() - _parse_ts(e["ts"])
            except (KeyError, ValueError):
                if multi:
                    continue
                break
            if age >= BUSY_STALE_SECONDS:
                break                                      # rows are oldest first: everything further back is stale too
            title = self._title(user, key) if multi else None
            who = "\u201c%s\u201d" % title if title else ("a session" if multi else "it")
            if ev in ("prompt", "tool"):
                why.append("%s is working on a request (last step %s ago)" % (who, _ago(age)))
            elif ev == "notification" and e.get("ntype") == "permission_prompt":
                why.append("%s is waiting for your approval in the Claude app" % who)
            if not multi:
                break
        try:
            n = self.host.running_commands(user)
        except Exception:                                  # noqa: BLE001 - never block a restart on this
            log.exception("could not count running commands")
            n = 0
        if n:
            why.append("%d command%s still running" % (n, "" if n == 1 else "s"))
        return why

    def _ask_if_busy(self, person, space, handler, arg, what):
        """True (and asks) when the agent is busy; the restart then waits for a "yes" in this chat."""
        why = self._busy(person)
        if not why:
            return False
        self.pending[person["linux_user"]] = (handler, arg, self.now() + CONFIRM_SECONDS)
        self.chat.send(space, "Your agent is busy: %s. %s stops it, and that work is lost.\n"
                              'Say "yes" within %d minutes to go ahead, or wait until it finishes.'
                       % ("; ".join(why), what, CONFIRM_SECONDS // 60))
        return True

    def on_yes(self, person, space):
        waiting = self.pending.pop(person["linux_user"], None)
        if not waiting or waiting[2] < self.now():
            self.chat.send(space, "Nothing is waiting for a yes. Say \"help\" for what I can do.")
            return
        handler, arg, _ = waiting
        handler(person, space, arg, confirmed=True)

    def on_new_session(self, person, space, raw_name="", confirmed=False):
        if not self._need_active(person, space):
            return
        new_name, why = self._clean_name(raw_name)
        if why:
            self.chat.send(space, why)
            return
        name, user = person["name"], person["linux_user"]
        if not confirmed and self._ask_if_busy(person, space, self.on_new_session, raw_name, "A new session"):
            return
        old = self.host.session_id(user) or person.get("session_id")
        leaving = self._leaving(person, old)               # read before the restart: it names the old transcript
        self.chat.send(space, "Starting a fresh session; this takes a minute or two...")
        started = self.now()                               # before the restart: the person may type while we wait
        self.host.new_session(user)
        sid = self.host.wait_session_id(user, other_than=old)
        sessions = self._remember(person.get("sessions") or [], leaving)
        self.store.update(name, session_id=sid or None, sessions=sessions, session_name=new_name,
                          session_title=None, session_started_at=started)
        self._audit(user, "session_switch", how="new")
        if sid:
            self.chat.send(space, "New session%s: %s\nYour previous one (%s) is saved: say \"sessions\" to go back."
                           % (' "%s"' % new_name if new_name else "", self.link(sid), self._label(leaving["name"], leaving["title"])))
        else:
            self.chat.send(space, "Your agent is restarting with a fresh session. Say \"link\" in a minute.")

    def on_name(self, person, space, raw):
        if not self._need_active(person, space):
            return
        new_name, why = self._clean_name(raw)
        if why or not new_name:
            self.chat.send(space, why or "Say name, then the name: name payments refactor")
            return
        self.store.update(person["name"], session_name=new_name)
        self.chat.send(space, 'This session is now called "%s".' % new_name)

    def on_sessions(self, person, space):
        if not self._need_active(person, space):
            return
        cur = self.host.session_id(person["linux_user"]) or person.get("session_id")
        now_label = self._label(person.get("session_name"), self._live_title(person) or person.get("session_title"))
        lines = ["now: %s %s" % (now_label, self.link(cur) or "(starting)")]
        for i, s in enumerate(person.get("sessions") or [], 1):
            when = dt.datetime.fromtimestamp(s["saved_at"], dt.timezone.utc).strftime("%d %b %H:%M UTC")
            lines.append("%d. %s (left %s) %s" % (i, self._label(s.get("name"), s.get("title")), when, self.link(s["id"])))
        if len(lines) == 1:
            lines.append('no earlier sessions. "new session" starts a fresh one and keeps this one.')
        else:
            lines.append('"switch 1" or "switch NAME" to go back; "forget N" or "forget all" to tidy the list.')
        self.chat.send(space, "\n".join(lines))

    @staticmethod
    def _find(sessions, arg):
        """(index, None) or (None, why not). arg is a list number, a session name, or a word from a name or title."""
        if arg.isdigit():
            n = int(arg)
            return (n - 1, None) if 1 <= n <= len(sessions) else (None, 'No session %d. Say "sessions" for the list.' % n)
        want = " ".join(arg.split()).lower()
        exact = [i for i, s in enumerate(sessions) if (s.get("name") or "").lower() == want]
        if len(exact) == 1:
            return exact[0], None
        hits = [i for i, s in enumerate(sessions) if want and want in ("%s %s" % (s.get("name") or "", s.get("title") or "")).lower()]
        if len(hits) == 1:
            return hits[0], None
        if not hits:
            return None, 'No saved session matches "%s". Say "sessions" for the list.' % short_text(arg, 40)
        return None, "%d sessions match that; use the number: %s" % (len(hits), ", ".join(str(i + 1) for i in hits))

    def on_switch(self, person, space, arg, confirmed=False):
        if not self._need_active(person, space):
            return
        name, user = person["name"], person["linux_user"]
        sessions = person.get("sessions") or []
        pasted = LINKED_SESSION_RE.match(arg)
        if pasted:                                         # a link or id copied from the Claude app: any of theirs
            sid = pasted.group(1)
            entry = next((s for s in sessions if s["id"] == sid), {"id": sid, "name": None, "title": None})
        else:
            i, why = self._find(sessions, arg)
            if why or not SESSION_RE.match(sessions[i]["id"]):
                self.chat.send(space, why or "That saved session is damaged; say \"forget %d\"." % (i + 1))
                return
            entry = sessions[i]
        old = self.host.session_id(user) or person.get("session_id")
        if entry["id"] == old:
            self.chat.send(space, "Your agent is already on that session: %s" % self.link(old))
            return
        if not confirmed and self._ask_if_busy(person, space, self.on_switch, arg, "Switching"):
            return
        target, leaving = entry["id"], self._leaving(person, old)
        self.chat.send(space, "Switching to %s; this takes a minute or two..." % self._label(entry.get("name"), entry.get("title")))
        started = self.now()
        self.host.switch_session(user, target)
        sid = self.host.wait_session_id(user, other_than=old)
        rest = self._remember([s for s in sessions if s["id"] != target], leaving)
        self.store.update(name, session_id=sid or None, sessions=rest, session_name=entry.get("name"),
                          session_title=entry.get("title"), session_started_at=started)
        self._audit(user, "session_switch", how="switch")
        if sid == target:
            self.chat.send(space, "Back on your earlier session: %s" % self.link(sid))
        elif sid:
            self.chat.send(space, "That session could not be reopened, so your agent started this one: %s" % self.link(sid))
        else:
            self.chat.send(space, 'Your agent is restarting. Say "link" in a minute.')

    def on_forget(self, person, space, arg):
        if not self._need_active(person, space):
            return
        sessions = person.get("sessions") or []
        if arg == "all":
            keep = []
        elif arg.isdigit() and 1 <= int(arg) <= len(sessions):
            keep = sessions[: int(arg) - 1] + sessions[int(arg):]
        else:
            self.chat.send(space, 'Say "forget N" with a number from "sessions", or "forget all".')
            return
        self.store.update(person["name"], sessions=keep)
        self.chat.send(space, "Removed %d from your list; the one you are in now is untouched. They still show in the "
                              "Claude app: archive them there to hide them." % (len(sessions) - len(keep)))

    # ---- scheduled jobs (definitions live with the person; this runs their own agent-job)
    def on_jobs(self, person, space, words):
        if not self._need_active(person, space):
            return
        if words == ["jobs"]:
            args = ["list"]
        elif len(words) == 3 and words[1] in ("pause", "resume", "remove", "run") and JOB_NAME_RE.match(words[2]):
            args = [words[1], words[2]]
        else:
            self.chat.send(space, 'Say "jobs", or "job pause|resume|remove|run NAME". Ask your agent in the Claude app to add one.')
            return
        rc, out = self.host.agent_job(person["linux_user"], args)
        self.chat.send(space, (out or ("done" if rc == 0 else "that did not work")).strip()[:3000])

    # ---- tasks from Chat: each task is its own headless conversation of the person's agent, in its own thread
    def _reply(self, space, text, thread=None):
        if thread:
            return self.chat.send(space, text, thread=thread)
        return self.chat.send(space, text)

    def _on_thread(self, person, space, found, text):
        """True when the message belonged to a known thread and was handled there."""
        role, item = found
        if role == "task":
            self.on_task_reply(person, space, item, text)
            return True
        return self._on_ask_thread(person, space, role, item, text)

    def _on_ask_thread(self, person, space, role, item, text):
        """A yes/no in an ask's approval thread (the asker's person), or the colleague's answer / "do it" in the thread
        of an ask they received. Always True: a message in an ask's thread is never a command or a task."""
        user, ask_id = person["linux_user"], item["ask"]
        thread = item.get({"approval": "approval_thread", "copy": "copy_thread"}.get(role, "received_thread")) or ""
        if not thread.startswith(space + "/"):              # only the person's own DM space counts
            log.warning("ignored a message for ask %s from %s: not in their own DM", ask_id, user)
            return True
        if role == "approval":
            self._on_approval(space, ask_id, thread, text)
        elif role == "copy":
            self._on_copy(person, space, ask_id, thread, text)
        else:
            self._on_received(person, space, ask_id, thread, text)
        return True

    def _usable_text(self, user, space, text, thread=None):
        """The task text, or None when it must not be run (blank: ignored; too long or a secret: refused)."""
        body = (text or "").strip()
        if not body:
            return None
        if len(body) > TASK_MAX_CHARS:
            self._reply(space, TOO_LONG, thread)
            return None
        if looks_secret(body):
            log.info("refused a message from %s: it looks like a secret (where=task)", user)
            self._audit(user, "secret_blocked", where="task")
            self._reply(space, SECRET_REFUSED, thread)
            return None
        return body

    def _busy_task(self, user):
        return next((t for t in self.state.tasks(user) if t.get("run_status") in RUN_BUSY), None)

    @staticmethod
    def _request(prompt, session_uuid, resume):
        return {"mode": "task", "prompt": prompt, "session_uuid": session_uuid, "resume": resume, "ask": None,
                "asker": None}

    def _launch(self, user, space, thread, ref, request, count=False):
        """Start the run (never waits for it). On failure the task goes idle and the person is told in its thread.
        count: a reply in the task's thread, counted against the daily limit only once it has started."""
        ok = False
        try:
            ok = bool(self.host.launch_run(user, ref, request))
        except Exception:                                  # noqa: BLE001 - a failed start is reported, never fatal
            log.exception("could not start chat run %s for %s", ref, user)
        chars = len(request.get("prompt") or "")
        if ok:
            self.state.update(ref, status="running", run_status="running", request=None, last_at=self.now(),
                              launched=True, count_on_launch=False)   # its session id now exists: replies --resume it
            if count:
                self.state.log_task_run(user)
            self._audit(user, "chat_task", task=ref, chars=chars, phase="started")
            log.info("chat run %s started for %s", ref, user)
        else:
            self.state.update(ref, status="failed", run_status="idle", request=None, last_at=self.now(),
                              count_on_launch=False)
            self._audit(user, "chat_task", task=ref, chars=chars, phase="failed")
            self._reply(space, "I couldn't start your agent just now. Try again in a minute.", thread)
        return ok

    def on_task(self, person, space, text):
        user = person["linux_user"]
        body = self._usable_text(user, space, text)
        if body is None:
            return
        if self.state.count_today("task", user) >= self.cfg.chat_tasks_per_day:
            self.chat.send(space, "You've used today's %d Chat tasks." % self.cfg.chat_tasks_per_day)
            return
        with self.run_lock:
            busy = self._busy_task(user)
            if busy:
                self.chat.send(space, "Still working on your last task (#%s)." % busy["ref"][1:])
                return
            slot = self.state.running() < self.cfg.chat_runs_at_once     # read first: new_task counts as running
            session_uuid = str(uuid.uuid4())
            ref = self.state.new_task(user, None, session_uuid)["ref"]
            request = self._request(body, session_uuid, resume=False)
            if not slot:                                   # held (root-only state) until check_outbox finds a slot
                self.state.update(ref, status="queued", run_status="queued", request=request)
            try:
                thread = self.chat.send(space, ON_IT % ref[1:] + ("" if slot else QUEUED_NOTE), new_thread=True)
            except Exception:
                self.state.update(ref, status="failed", run_status="idle", request=None)
                raise
            self.state.update(ref, thread=thread)
            if slot:
                self._launch(user, space, thread, ref, request)
            else:
                log.info("chat task %s for %s waits for a free slot", ref, user)

    def on_task_reply(self, person, space, task, text):
        """A message in a task's thread: the next turn of that same conversation."""
        user, ref, thread = person["linux_user"], task["ref"], task.get("thread")
        body = self._usable_text(user, space, text, thread)
        if body is None:
            return
        with self.run_lock:
            task = self.state.get(ref) or task
            if task.get("run_status") in RUN_BUSY:
                self._reply(space, STILL_LAST_MESSAGE, thread)
                return
            other = self._busy_task(user)
            if other:
                self._reply(space, "Still working on your last task (#%s)." % other["ref"][1:], thread)
                return
            if self.state.count_today("task", user) >= self.cfg.chat_tasks_per_day:   # a reply is a run too
                self._reply(space, "You've used today's %d Chat tasks." % self.cfg.chat_tasks_per_day, thread)
                return
            request = self._request(body, task["session_uuid"], resume=bool(task.get("launched")))
            if self.state.running() < self.cfg.chat_runs_at_once:   # counted for today once it has started
                if self._launch(user, space, thread, ref, request, count=True):
                    self._reply(space, "On it.", thread)
            else:
                self.state.update(ref, status="queued", run_status="queued", request=request, last_at=self.now(),
                                  count_on_launch=True)
                self._reply(space, "Got it." + QUEUED_NOTE, thread)

    def _person_of(self, user):
        return next((p for p in self.store.all() if p.get("linux_user") == user), None)

    def _on_result(self, p, msg):
        """A run's result from p's own outbox. Only a run of p's own task is acted on; anything else is dropped and
        logged by ref, never with its text. Returns True when something was posted."""
        user, ref = p["linux_user"], msg["ref"]
        if not ref.startswith("t"):
            return self._on_ask_result(p, msg)
        task = self.state.get(ref)
        if not task or task.get("user") != user:
            log.warning("dropped a result for %s from %s: not one of their tasks", ref, user)
            return False
        if task.get("run_status") != "running":
            log.warning("dropped a result for %s from %s: no run of it is going", ref, user)
            return False
        status, text = msg.get("status"), msg["text"]
        if status not in ("ok", "needs_you", "failed"):
            status = "failed"
        self.state.update(ref, status=status, run_status="idle", last_at=self.now())   # before posting: a Chat error
        n, limit = ref[1:], self.cfg.chat_result_chars                                 # must not hold the slot
        if looks_secret(text):
            log.info("did not post the result of %s for %s: it looks like a secret (where=result)", ref, user)
            self._audit(user, "secret_blocked", where="result")
            post = ("Task #%s finished, but its answer looks like it holds a secret, so I did not post it. To read it, %s."
                    % (n, RESULTS_HINT % ref))
        else:
            body = text if len(text) <= limit else text[:limit].rstrip() + "\n… full result: " + RESULTS_HINT % ref
            post = {"needs_you": "Task #%s needs you:\n%s", "failed": "Task #%s failed:\n%s"}.get(status, "%s%s")
            post = post % (("", body) if status == "ok" else (n, body))
        self._audit(user, "chat_task", task=ref, chars=len(text), phase="done" if status == "ok" else status)
        try:
            self._reply(p["chat_space"], post, task.get("thread"))
        except Exception:                                  # noqa: BLE001
            log.exception("could not post the result of %s for %s", ref, user)
            return False
        return True

    # ---- asking a colleague (design §2). An ask goes awaiting_ok -> asking_agent -> with_person -> answered |
    # declined | expired; "done" is closed_at, set once the answer was read or DMed. The state keeps the linux users
    # in from/to and the people names in from_name/to_name. Message text is never logged or audited.
    def _audit_ask(self, ask, phase, chars, **extra):
        """One metadata row in BOTH people's audit files."""
        names = {"from": ask.get("from_name") or ask["from"], "to": ask.get("to_name") or ask["to"]}
        for user in (ask["from"], ask["to"]):
            self._audit(user, "ask", ask=ask["ask"], chars=chars, phase=phase, **names, **extra)

    @staticmethod
    def _inbox_body(ask_id, outcome, colleague, text, by=None):
        """What ask-colleague reads from ~/.claude/inbox/<ask>.json."""
        text = text if len(text) <= INBOX_CHARS else text[:INBOX_CHARS].rstrip() + "\n… (cut)"
        return {"ask": ask_id, "outcome": outcome, "by": by, "from": colleague, "text": text}

    def _reply_task_of(self, user, ask_id, ref):
        """The asker's own Chat task `ref` (named by ask-colleague --no-wait), else None (logged, never the text)."""
        if ref is None:
            return None
        task = self.state.get(ref) if isinstance(ref, str) and TASK_REF_RE.match(ref) else None
        if not task or task.get("user") != user:
            log.warning("ask %s from %s names a task that is not theirs; its outcome goes the usual way", ask_id, user)
            return None
        return ref

    def _post_to_task(self, ask, outcome, text, by=None):
        """Post an ask's outcome in the asker's Chat task thread, or else their DM. "thread", "dm", or None when
        neither could be posted (the caller then takes the inbox path, whose DM fallback retries)."""
        user, ref = ask["from"], ask.get("reply_task")
        p = self._person_of(user)
        if not p or not p.get("chat_space"):
            return None
        q = short_text(ask.get("text") or "", THREAD_QUESTION_CHARS)
        name = shown(ask.get("to_name") or ask.get("to") or "")
        if outcome == "answered":
            who = name + ("'s agent" if by == "agent" else "")
            post, dm = THREAD_ANSWER % (who, q, text), ASK_DM % (who, q, text)
        else:
            post = dm = THREAD_NOT_SENT % (name, q, THREAD_WHY.get(outcome, text))
        task = self.state.get(ref) if ref else None
        if task and task.get("user") == user and task.get("thread"):
            try:
                self._reply(p["chat_space"], post, task["thread"])
                return "thread"
            except Exception as e:                         # noqa: BLE001 - the DM is next
                log.warning("could not post ask %s in task %s for %s: %s", ask["ask"], ref, user, e.__class__.__name__)
        try:
            self.chat.send(p["chat_space"], dm)
            return "dm"
        except Exception as e:                             # noqa: BLE001
            log.warning("could not DM ask %s to %s: %s", ask["ask"], user, e.__class__.__name__)
            return None

    def _refuse_ask(self, user, ask_id, name, outcome, why, reply_task=None, text=""):
        """Straight back to the waiter (refused / limit): no state row, nothing posted to anyone. For an ask from a
        Chat task the reason is posted in that task's thread instead."""
        log.info("ask %s from %s: %s", ask_id, user, outcome)
        if reply_task and self._post_to_task({"ask": ask_id, "from": user, "to": name, "to_name": name,
                                              "text": text, "reply_task": reply_task}, outcome, why):
            return
        try:
            self.host.write_inbox(user, ask_id, self._inbox_body(ask_id, outcome, name, why))
        except Exception as e:                             # noqa: BLE001 - the waiter then times out; nothing leaks
            log.warning("could not answer ask %s for %s: %s", ask_id, user, e.__class__.__name__)

    def _on_outbox_ask(self, p, msg):
        """A question p's agent wants to ask colleague msg["to"] (a people name). The sender is p, whose home it was."""
        user, ask_id, name, text = p["linux_user"], msg["ask"], msg["to"], msg["text"]
        if self.state.get(ask_id) is not None:
            log.warning("dropped ask %s from %s: that id is already taken", ask_id, user)
            return
        task = self._reply_task_of(user, ask_id, msg.get("task"))

        def refuse(outcome, why, quoted=text):
            return self._refuse_ask(user, ask_id, name, outcome, why, task, quoted)
        target = self.store.get(name)
        if target is None:
            return refuse("refused", "There is no colleague called %s on the box." % name)
        if target.get("linux_user") == user:
            return refuse("refused", "You cannot ask yourself; ask a colleague by name.")
        if target.get("status") != "active" or not target.get("chat_space"):
            return refuse("refused", "%s has not set up their agent yet, so they cannot be asked." % shown(name))
        if self.state.count_today("ask_sent", user) >= self.cfg.asks_per_day:
            return refuse("limit", "Limit reached: your agent has asked %d questions today." % self.cfg.asks_per_day)
        if self.state.count_today("ask_received", target["linux_user"]) >= self.cfg.asks_received_per_day:
            return refuse("limit", "Limit reached: %s has had %d questions today."
                          % (shown(name), self.cfg.asks_received_per_day))
        if looks_secret(text):
            log.info("refused ask %s from %s: it looks like a secret (where=ask)", ask_id, user)
            self._audit(user, "secret_blocked", where="ask")
            return refuse("refused", SECRET_REFUSED, quoted="(not shown)")
        self.state.new_ask(user, target["linux_user"], text, ask_id)
        extra = {"reply_task": task} if task else {}
        ask = self.state.update(ask_id, from_name=p["name"], to_name=target["name"], **extra)
        self._audit_ask(ask, "requested", len(text))
        try:
            thread = self.chat.send(p["chat_space"], ASK_OK % (shown(target["name"]), text), new_thread=True)
        except Exception as e:                             # noqa: BLE001
            log.warning("could not ask %s to approve ask %s: %s", user, ask_id, e.__class__.__name__)
            self.state.close(ask_id)                       # stays awaiting_ok: never counted as the colleague's
            self._refuse_ask(user, ask_id, target["name"], "refused", "I could not reach your user in Chat just now.")
            return
        self.state.update(ask_id, approval_thread=thread)
        log.info("ask %s from %s to %s waits for approval", ask_id, user, target["linux_user"])

    def _open_ask(self, ask_id, status):
        """The ask, read fresh, when it is open and in `status`; else None."""
        ask = self.state.get(ask_id)
        if not ask or ask.get("closed_at") is not None or ask.get("status") != status:
            return None
        return ask

    def _on_approval(self, space, ask_id, thread, text):
        low = (text or "").strip().lower()
        if not low:
            return
        with self.run_lock:
            ask = self._open_ask(ask_id, "awaiting_ok")
            if ask is None:                                # decided already (or a redelivery after a restart)
                self._reply(space, ASK_DECIDED, thread)
                return
            if low in NO_WORDS:
                self.state.update(ask_id, status="declined")
                self._audit_ask(ask, "declined", len(ask["text"]))
                self._deliver(ask, "declined", "%s did not approve this question." % shown(ask["from_name"]))
                self._reply(space, "OK, not sent.", thread)
                return
            if low not in YES_WORDS:
                self._reply(space, "Reply yes or no here.", thread)
                return
            self.state.update(ask_id, status="asking_agent")
            self._audit_ask(ask, "approved", len(ask["text"]))
            self._reply(space, ASK_SENT % shown(ask["to_name"]), thread)
            request = {"mode": "ask", "prompt": ask["text"], "session_uuid": str(uuid.uuid4()), "resume": False,
                       "ask": ask_id, "asker": ask["from_name"]}
            self._start_ask_run(ask_id, ask_id, request)

    def _on_received(self, person, space, ask_id, thread, text):
        """The colleague, in the thread of a question they were handed: their answer, or "do it"."""
        user, body = person["linux_user"], (text or "").strip()
        if not body:
            return
        with self.run_lock:
            ask = self._open_ask(ask_id, "with_person")
            if ask is None:
                self._reply(space, "That question is already answered.", thread)
                return
            if len(body) > TASK_MAX_CHARS:
                self._reply(space, TOO_LONG, thread)
                return
            if body.lower() in DO_IT_WORDS:
                if ask.get("run_status") in RUN_BUSY:
                    self._reply(space, "Your agent is already on it.", thread)
                    return
                request = {"mode": "doit", "session_uuid": str(uuid.uuid4()), "resume": False, "ask": ask_id,
                           "asker": ask["from_name"], "prompt": DOIT_PROMPT % (
                               shown(ask["from_name"]), shown(ask["to_name"]), defuse_markers(ask["text"]),
                               shown(ask["from_name"]))}
                started = self._start_ask_run(ask_id, "d" + ask_id[1:], request)
                if started:
                    self._audit_ask(ask, "do_it", len(ask["text"]))
                    self._reply(space, "On it. I'll post the result here." if started is True
                                else "Got it." + QUEUED_NOTE, thread)
                return
            if looks_secret(body):
                log.info("refused an answer to ask %s from %s: it looks like a secret (where=answer)", ask_id, user)
                self._audit(user, "secret_blocked", where="answer")
                self._reply(space, SECRET_REFUSED, thread)
                return
            self._audit_ask(ask, "answered", len(body))
            self._deliver(ask, "answered", body, by="person")
            self._reply(space, "Sent to %s." % shown(ask["from_name"]), thread)

    def _on_copy(self, person, space, ask_id, thread, text):
        """The colleague, in the thread of the copy of their agent's answer: their own answer, sent to the asker as a
        correction while the ask is open (an asker who already read the first answer gets it in their DM)."""
        user, body = person["linux_user"], (text or "").strip()
        if not body:
            return
        with self.run_lock:
            ask = self.state.get(ask_id)
            if not ask or ask.get("closed_at") is not None:
                self._reply(space, ASK_ALREADY_DELIVERED % shown((ask or {}).get("from_name") or "your colleague"),
                            thread)
                return
            if len(body) > TASK_MAX_CHARS:
                self._reply(space, TOO_LONG, thread)
                return
            if looks_secret(body):
                log.info("refused a correction to ask %s from %s: it looks like a secret (where=answer)", ask_id, user)
                self._audit(user, "secret_blocked", where="answer")
                self._reply(space, SECRET_REFUSED, thread)
                return
            self._audit_ask(ask, "corrected", len(body))
            self._deliver(ask, "answered", ASK_CORRECTION % (shown(ask["to_name"]), body), by="person")
            self._reply(space, ASK_CORRECTED % shown(ask["from_name"]), thread)

    def _start_ask_run(self, ask_id, ref, request):
        """An ask's answer run (ref a…) or do-it run (d…) in the colleague's account. True started, "queued" waiting for
        a box slot, False could not start (already handled). Called under run_lock."""
        if self.state.running() >= self.cfg.chat_runs_at_once:
            self.state.update(ask_id, run_status="queued", run_ref=ref, request=request, last_at=self.now())
            log.info("ask run %s waits for a free slot", ref)
            return "queued"
        return self._launch_ask(ask_id, ref, request)

    def _launch_ask(self, ask_id, ref, request):
        ask = self.state.get(ask_id)
        ok = False
        try:
            ok = bool(self.host.launch_run(ask["to"], ref, request))
        except Exception:                                  # noqa: BLE001 - a failed start is handled, never fatal
            log.exception("could not start ask run %s for %s", ref, ask["to"])
        if ok:
            self.state.update(ask_id, run_status="running", run_ref=ref, request=None, last_at=self.now())
            log.info("ask run %s started for %s", ref, ask["to"])
            return True
        self.state.update(ask_id, run_status="idle", run_ref=None, request=None, last_at=self.now())
        if ref.startswith("a"):                            # the agent cannot try: the person decides
            self._hand_to_person(ask_id)
        else:
            self._post_received(ask, "I couldn't start your agent just now. Say do it again in a minute, or answer "
                                     "here yourself.")
        return False

    def _post_received(self, ask, text):
        """Post in the thread of the ask the colleague received."""
        colleague = self._person_of(ask["to"])
        if not colleague or not colleague.get("chat_space"):
            return False
        try:
            self._reply(colleague["chat_space"], text, ask.get("received_thread"))
            return True
        except Exception as e:                             # noqa: BLE001
            log.warning("could not post to %s about ask %s: %s", ask["to"], ask["ask"], e.__class__.__name__)
            return False

    def _hand_to_person(self, ask_id, note=""):
        """The colleague's agent could not answer: open a thread for the colleague. Retried by check_asks when the
        post fails. Called under run_lock (or from check_asks)."""
        ask = self.state.update(ask_id, status="with_person", run_status="idle")
        colleague = self._person_of(ask["to"])
        if not colleague or not colleague.get("chat_space"):
            log.warning("ask %s: %s cannot be reached in Chat", ask_id, ask["to"])
            return
        try:
            thread = self.chat.send(colleague["chat_space"], ASK_TO_PERSON % (shown(ask["from_name"]), ask["text"])
                                    + note, new_thread=True)
        except Exception as e:                             # noqa: BLE001
            log.warning("could not hand ask %s to %s: %s", ask_id, ask["to"], e.__class__.__name__)
            return
        self.state.update(ask_id, received_thread=thread, handed_at=self.now())
        self._audit_ask(ask, "to_person", len(ask["text"]))

    def _deliver(self, ask, outcome, text, by=None):
        """Write the outcome to the asker's inbox, where the waiting ask-colleague picks it up. check_asks DMs an
        unread answer (and cleans up any unread outcome) after ASK_DM_FALLBACK_SECONDS, then closes the ask."""
        ask_id = ask["ask"]
        body = self._inbox_body(ask_id, outcome, ask["to_name"], text, by)
        fields = {"status": outcome, "outcome": outcome, "by": by, "delivered_at": self.now(),
                  "answer": body["text"] if outcome == "answered" else None}   # for the DM; dropped on close
        if self.state.get(ask_id).get("run_status") == "queued":   # a waiting run for a decided question never starts
            fields.update(run_status="idle", run_ref=None, request=None)
        if ask.get("reply_task"):                          # from a Chat task: posted in its thread (or DM), no inbox
            via = self._post_to_task(ask, outcome, body["text"], by)
            if via:
                fields.update(delivered_via=via, answer=None)
                ask = self.state.update(ask_id, **fields)
                self._audit_ask(ask, "delivered", len(body["text"]), via=via)
                return                                     # check_asks closes it later (corrections still land)
        try:
            self.host.write_inbox(ask["from"], ask_id, body)
            written = True
        except Exception as e:                             # noqa: BLE001
            log.warning("could not write the answer to ask %s for %s: %s", ask_id, ask["from"], e.__class__.__name__)
            written = False
        ask = self.state.update(ask_id, **fields)
        if written:
            self._audit_ask(ask, "delivered", len(body["text"]))
        else:
            self._settle(ask, unread=True)                 # no inbox file: straight to the DM

    def _settle(self, ask, unread=None):
        """End of delivery: an answer nobody picked up goes to the asker's DM; any unread file is removed; the ask is
        closed (text and answer dropped). False when the DM could not be posted (tried again next time)."""
        ask_id, user = ask["ask"], ask["from"]
        if ask.get("delivered_via"):                       # already posted in a Chat task thread or the DM
            unread = False
        if unread is None:
            try:
                unread = bool(ask.get("inbox_removed")) or not self.host.inbox_taken(user, ask_id)
            except Exception:                              # noqa: BLE001 - cannot tell: the DM is the safe side
                unread = True
        if unread:
            try:
                self.host.remove_inbox(user, ask_id)
                self.state.update(ask_id, inbox_removed=True)
            except Exception as e:                         # noqa: BLE001
                log.warning("could not remove the unread answer to ask %s for %s: %s", ask_id, user, e.__class__.__name__)
            if ask.get("outcome") == "answered":
                p = self._person_of(user)
                who = shown(ask["to_name"]) + ("'s agent" if ask.get("by") == "agent" else "")
                answer = ask.get("answer") or ""
                if not p or not p.get("chat_space"):
                    log.warning("ask %s: %s cannot be reached in Chat; the answer is dropped", ask_id, user)
                else:
                    try:
                        self.chat.send(p["chat_space"], ASK_DM % (who, short_text(ask.get("text") or "", 100), answer))
                    except Exception as e:                 # noqa: BLE001
                        log.warning("could not DM the answer to ask %s to %s: %s", ask_id, user, e.__class__.__name__)
                        return False
                    self._audit_ask(ask, "dm_fallback", len(answer))
        self.state.update(ask_id, answer=None)
        self.state.close(ask_id)
        log.info("ask %s closed", ask_id)
        return True

    def _on_ask_result(self, p, msg):
        """The result of an ask's answer run (a…) or do-it run (d…, ruling R2: "d" + ask id[1:]). Accepted only from
        the outbox of the colleague the ask went TO, while that run is going; anything else is dropped by ref."""
        user, ref = p["linux_user"], msg["ref"]
        ask_id = "a" + ref[1:]
        with self.run_lock:
            ask = self.state.get(ask_id)
            if not ask or ask.get("to") != user:
                log.warning("dropped a result for %s from %s: not an ask they were sent", ref, user)
                return False
            if ask.get("run_status") != "running" or ask.get("run_ref") != ref:
                log.warning("dropped a result for %s from %s: no run of it is going", ref, user)
                return False
            ask = self.state.update(ask_id, run_status="idle", run_ref=None, last_at=self.now())
            if ref.startswith("a"):
                return self._on_answer_run(ask, msg["status"], msg["text"])
            return self._on_doit_run(ask, ref, msg["status"], msg["text"])

    def _on_answer_run(self, ask, status, text):
        if ask.get("status") != "asking_agent" or ask.get("closed_at") is not None:
            return False
        if status != "answer":                             # cannot, failed or anything off-mode: the person decides
            note = ""
            if status == "cannot" and (text or "").strip() and not looks_secret(text):
                note = " (Your agent could not answer: %s)" % short_text(text, CANNOT_REASON_CHARS)
            self._hand_to_person(ask["ask"], note)
            return True
        if looks_secret(text):
            log.info("did not send the answer to ask %s: it looks like a secret (where=answer)", ask["ask"])
            self._audit(ask["to"], "secret_blocked", where="answer")
            self._hand_to_person(ask["ask"], " (Your agent's answer looked like it held a secret, so I did not send it.)")
            return True
        self._audit_ask(ask, "auto_answered", len(text))
        self._deliver(ask, "answered", text, by="agent")
        limit = self.cfg.chat_result_chars
        copy = text if len(text) <= limit else text[:limit].rstrip() + "…"
        colleague = self._person_of(ask["to"])
        if colleague and colleague.get("chat_space"):
            try:
                thread = self.chat.send(colleague["chat_space"], ASK_COPY % (
                    shown(ask["from_name"]), short_text(ask["text"], ASK_COPY_QUESTION_CHARS), copy), new_thread=True)
                if thread:                                 # a reply there is the colleague's correction
                    self.state.update(ask["ask"], copy_thread=thread)
            except Exception as e:                         # noqa: BLE001
                log.warning("could not copy the answer to ask %s to %s: %s", ask["ask"], ask["to"], e.__class__.__name__)
        return True

    def _on_doit_run(self, ask, ref, status, text):
        """Posted in the colleague's thread; only a clean result of a still-open ask goes to the asker."""
        if status not in ("ok", "needs_you", "failed"):
            status = "failed"
        open_ = ask.get("status") == "with_person" and ask.get("closed_at") is None
        again = " Nothing was sent to %s. Reply here to answer, or say do it again." % shown(ask["from_name"])
        limit = self.cfg.chat_result_chars
        if looks_secret(text):
            log.info("did not post or send the result of %s: it looks like a secret (where=answer)", ref)
            self._audit(ask["to"], "secret_blocked", where="answer")
            post = "Your agent finished, but its answer looks like it holds a secret, so I did not post it. To read " \
                   "it, %s." % (RESULTS_HINT % ref) + (again if open_ else "")
            send = False
        else:
            body = text if len(text) <= limit else text[:limit].rstrip() + "\n… full result: " + RESULTS_HINT % ref
            send = status == "ok" and open_
            if status == "ok":
                post = body + ("\n(Sent to %s.)" % shown(ask["from_name"]) if send else "")
            else:
                post = ("Your agent needs you:\n%s" if status == "needs_you" else "Your agent's run failed:\n%s") % body
                post += again if open_ else ""
        if send:
            self._audit_ask(ask, "answered", len(text))
            self._deliver(ask, "answered", text, by="agent")
        return self._post_received(ask, post)

    def _closed_ask_thread(self, user, thread):
        """The reply for a message in a thread of a closed ask (the approval thread when user asked, the received or
        copy thread when user was asked); None when the thread is no closed ask's."""
        for a in self.state.asks():
            if a.get("closed_at") is None:
                continue
            if (a["from"] == user and a.get("approval_thread") == thread) or \
                    (a["to"] == user and a.get("received_thread") == thread):
                return ASK_CLOSED
            if a["to"] == user and a.get("copy_thread") == thread:
                return ASK_ALREADY_DELIVERED % shown(a.get("from_name") or a["from"])
        return None

    def check_asks(self):
        """Run with the outbox (nudger thread): questions nobody approved in time expire, a hand-over whose post failed
        is tried again, an answer not picked up in time goes to the asker's DM, old asks are pruned. Returns how many
        asks expired or settled."""
        done = 0
        for a in self.state.asks():
            if a.get("closed_at") is not None:
                continue
            age = self.now() - float(a.get("created_at") or 0)
            if a.get("status") == "awaiting_ok" and age >= self.cfg.ask_approval_seconds:
                done += 1 if self._expire(a["ask"]) else 0
            elif a.get("status") == "with_person" and not a.get("handed_at"):
                with self.run_lock:
                    if self._open_ask(a["ask"], "with_person") and not self.state.get(a["ask"]).get("handed_at"):
                        self._hand_to_person(a["ask"])
            elif a.get("delivered_at") is not None and \
                    self.now() - float(a["delivered_at"]) >= self.cfg.ask_dm_fallback_seconds:
                done += 1 if self._settle(a) else 0
        self.state.prune()
        return done

    def _expire(self, ask_id):
        with self.run_lock:
            ask = self._open_ask(ask_id, "awaiting_ok")
            if ask is None:
                return False
            self.state.update(ask_id, status="expired")
            self._audit_ask(ask, "expired", len(ask["text"]))
            hours = self.cfg.ask_approval_seconds / 3600.0
            self._deliver(ask, "expired", "%s did not approve it in time." % shown(ask["from_name"]))
        p = self._person_of(ask["from"])
        if p and p.get("chat_space") and ask.get("approval_thread"):
            try:
                self._reply(p["chat_space"], "Not sent: no yes within %g hours." % hours, ask["approval_thread"])
            except Exception as e:                         # noqa: BLE001
                log.warning("could not tell %s ask %s expired: %s", ask["from"], ask_id, e.__class__.__name__)
        return True

    def _sweep_stale_runs(self):
        """A run that never reported (killed for time or memory) must not hold the person or a box slot for ever."""
        with self.run_lock:
            for a in self.state.asks():
                if a.get("run_status") != "running" or self.now() - float(a.get("last_at") or 0) <= RUN_STALE_SECONDS:
                    continue
                ref = a.get("run_ref") or ""
                a = self.state.update(a["ask"], run_status="idle", run_ref=None, last_at=self.now())
                log.warning("ask run %s for %s never reported; marked failed", ref, a["to"])
                if ref.startswith("a") and a.get("status") == "asking_agent" and a.get("closed_at") is None:
                    self._hand_to_person(a["ask"])
                elif ref.startswith("d"):
                    self._post_received(a, "Your agent stopped without an answer (it ran out of time or memory). "
                                           "Reply here to answer, or say do it again.")
            for t in self.state.tasks():
                if t.get("run_status") != "running" or self.now() - float(t.get("last_at") or 0) <= RUN_STALE_SECONDS:
                    continue
                ref, user = t["ref"], t["user"]
                self.state.update(ref, status="failed", run_status="idle", last_at=self.now())
                self._audit(user, "chat_task", task=ref, chars=0, phase="failed")
                log.warning("chat run %s for %s never reported; marked failed", ref, user)
                p = self._person_of(user)
                if p and p.get("chat_space"):
                    try:
                        self._reply(p["chat_space"], "Task #%s stopped without an answer (it ran out of time or memory). "
                                                     "Reply here to try again." % ref[1:], t.get("thread"))
                    except Exception:                      # noqa: BLE001
                        log.exception("could not tell %s about %s", user, ref)

    def _start_queued(self):
        """Start waiting runs, longest-waiting first, while the box has a free slot."""
        with self.run_lock:
            waiting = sorted([t for t in self.state.tasks() if t.get("run_status") == "queued"] +
                             [a for a in self.state.asks() if a.get("run_status") == "queued"],
                             key=lambda t: float(t.get("last_at") or 0))
            for t in waiting:
                if self.state.running() >= self.cfg.chat_runs_at_once:
                    return
                if "ask" in t and "ref" not in t:          # an ask's answer or do-it run
                    self._start_queued_ask(t)
                    continue
                ref, user = t["ref"], t["user"]
                p = self._person_of(user)
                if not p or p.get("status") != "active" or not p.get("chat_space") or not isinstance(t.get("request"), dict):
                    self.state.update(ref, status="failed", run_status="idle", request=None, count_on_launch=False)
                    log.warning("dropped queued chat run %s for %s: person no longer active", ref, user)
                    continue
                try:
                    self._launch(user, p["chat_space"], t.get("thread"), ref, t["request"],
                                 count=bool(t.get("count_on_launch")))
                except Exception:                          # noqa: BLE001 - one person's Chat error must not stop the rest
                    log.exception("could not start queued chat run %s for %s", ref, user)

    def _start_queued_ask(self, a):
        """Called under run_lock by _start_queued."""
        ask_id, ref = a["ask"], a.get("run_ref") or ""
        colleague = self._person_of(a["to"])
        wanted = {"a": "asking_agent", "d": "with_person"}.get(ref[:1])
        if a.get("closed_at") is not None or a.get("status") != wanted or not colleague or \
                colleague.get("status") != "active" or not isinstance(a.get("request"), dict) or not REF_RE.match(ref):
            self.state.update(ask_id, run_status="idle", run_ref=None, request=None)
            log.warning("dropped queued ask run %s for %s", ref, a["to"])
            return
        try:
            self._launch_ask(ask_id, ref, a["request"])
        except Exception:                                  # noqa: BLE001
            log.exception("could not start queued ask run %s for %s", ref, a["to"])

    # ---- the doorbell: messages agents and jobs leave for their own user; results of Chat runs come the same way
    def check_outbox(self):
        delivered = 0
        for p in self.store.all():
            if p.get("status") != "active" or not p.get("chat_space"):
                continue
            user = p["linux_user"]
            msgs = self.host.take_outbox(user)
            if not msgs:
                continue
            window = self.sent.setdefault(user, deque())
            for msg in msgs:
                kind = msg.get("kind", "self")
                if kind == "result":                       # before the hourly limit: a result is never dropped
                    delivered += 1 if self._on_result(p, msg) else 0
                    continue
                if kind == "ask":
                    self._on_outbox_ask(p, msg)
                    continue
                while window and self.now() - window[0] > 3600:
                    window.popleft()
                if len(window) >= self.cfg.outbox_per_hour:
                    self.dropped[user] = self.dropped.get(user, 0) + 1
                    continue
                source = msg["source"]
                if source == "box" and user != self.cfg.admin_person:
                    source = "agent"                       # only the monitor's alerts to the admin read as [box]
                text = "%s %s" % (label(source), msg["text"])
                if self.dropped.get(user):
                    text += "\n(%d earlier message(s) were dropped: over %d an hour)" % (self.dropped.pop(user), self.cfg.outbox_per_hour)
                self.chat.send(p["chat_space"], text)
                window.append(self.now())
                self._audit(user, "notify", source=msg["source"].split(":")[0], chars=len(msg["text"]))
                delivered += 1
        self._sweep_stale_runs()
        self._start_queued()
        return delivered

    # ---- nudges
    def nudges_due(self, waited_seconds):
        """How many nudges should have been sent after this long: first at nudge_minutes, second nudge_second_gap
        later, then one every nudge_repeat minutes. After a long outage only the latest one is sent."""
        first = self.cfg.nudge_minutes * 60
        second = first + self.cfg.nudge_second_gap * 60
        if waited_seconds < first:
            return 0
        if waited_seconds < second:
            return 1
        return 2 + int((waited_seconds - second) // (self.cfg.nudge_repeat * 60))

    def check_nudges(self):
        """One nudge schedule per waiting session. people.json keeps nudges = {session: {"for": iso, "count": n}};
        the older nudged_for/nudge_count pair is read as session "" until the first write replaces it."""
        nudged = 0
        for p in self.store.all():
            if p.get("status") != "active" or not p.get("chat_space"):
                continue
            user = p["linux_user"]
            multi = self._max_sessions(user) > 1
            waits = waits_by_session(read_tail(os.path.join(self.cfg.audit_dir, user + ".jsonl")), per_session=multi)
            if multi:
                waits = {k: w for k, w in waits.items() if self.now() - w[1] < WAIT_MAX_SECONDS}
            kept = p.get("nudges")
            if not isinstance(kept, dict):
                kept = {"": {"for": p.get("nudged_for"), "count": p.get("nudge_count", 0)}} if p.get("nudged_for") else {}
            fresh, sent = {}, 0
            for key, (iso, epoch) in sorted(waits.items(), key=lambda kv: kv[1][1]):
                due = self.nudges_due(self.now() - epoch)
                old = kept.get(key) or {}
                done = old.get("count", 0) if old.get("for") == iso else 0
                fresh[key] = {"for": iso, "count": max(done, due)}
                if due <= done:
                    continue
                if multi:                                  # the session id file names one session, not this one
                    link = self.cfg.link_base.rstrip("/")
                else:
                    link = self.link(self.host.session_id(user) or p.get("session_id")) or "open the Claude app"
                title = self._title(user, key) if multi else None
                where = " in \u201c%s\u201d" % title if title else ""
                if due == 1:
                    text = "%s, your agent is waiting%s: %s" % (p["name"], where, link)
                else:
                    mins = int((self.now() - epoch) // 60)
                    text = "%s, your agent is still waiting%s (%d min): %s" % (p["name"], where, mins, link)
                self.chat.send(p["chat_space"], text)
                self._audit(user, "nudge")
                sent += 1
            if sent or fresh != kept:
                self.store.update(p["name"], nudges=fresh)
            nudged += sent
        return nudged


# ------------------------------------------------------------------ real implementations
class PubSubSource:
    def __init__(self, cfg):
        from google.cloud import pubsub_v1                        # imported lazily: tests need no Google libs
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(cfg.key_path)
        self.client = pubsub_v1.SubscriberClient(credentials=creds)
        self.path = self.client.subscription_path(cfg.project, cfg.subscription)

    def pull(self):
        resp = self.client.pull(request={"subscription": self.path, "max_messages": 10}, timeout=30)
        out = []
        for rm in resp.received_messages:
            try:
                out.append((rm.ack_id, json.loads(rm.message.data.decode("utf-8"))))
            except ValueError:
                out.append((rm.ack_id, {}))
        return out

    def ack(self, ack_ids):
        self.client.acknowledge(request={"subscription": self.path, "ack_ids": list(ack_ids)})


class GoogleChat:
    def __init__(self, cfg):
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(
            cfg.key_path, scopes=["https://www.googleapis.com/auth/chat.bot"])
        self.session = AuthorizedSession(creds)

    def send(self, space, text, thread=None, new_thread=False):
        """Post text; return the thread name of the posted message. thread= replies in that thread, new_thread=True
        opens a fresh one, neither behaves as before (a plain message; Chat starts its own thread)."""
        body = {"text": text}
        if thread:
            body["thread"] = {"name": thread}
        elif new_thread:
            body["thread"] = {"threadKey": "vylo-%s" % uuid.uuid4().hex}
        url = "https://chat.googleapis.com/v1/%s/messages" % space
        if "thread" in body:                       # the reply option only with a thread: alone, Google answers 400
            r = self.session.post(url, json=body, timeout=20,
                                  params={"messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"})
        else:
            r = self.session.post(url, json=body, timeout=20)
        r.raise_for_status()
        try:
            return (r.json().get("thread") or {}).get("name") or None
        except ValueError:
            return None


ASK_COLLEAGUE = b"ask-colleague"   # also matches /usr/local/bin/ask-colleague


def counts_as_command(cmdline, marker):
    """Does this process (its raw /proc cmdline) count as a command the agent is running? A Claude shell, except one
    that is only waiting in ask-colleague for a colleague's answer: that is not work to protect from a restart."""
    return marker in cmdline and ASK_COLLEAGUE not in cmdline


class SystemHost:
    """tmux / systemd / claude on this box. The sign-in runs in a tmux session named 'login' owned by the
    colleague's Linux user, so the code is typed into a real terminal and nothing is written to disk."""

    def __init__(self):
        self.url_re = re.compile(r"https://\S+")

    def _as(self, user, args, timeout=30):
        home = "/home/%s" % user
        cmd = ["runuser", "-u", user, "--", "env", "HOME=" + home, "TERM=xterm-256color",
               "PATH=%s/.local/bin:/usr/local/bin:/usr/bin:/bin" % home] + list(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def start_login(self, user):
        self.stop_login(user)
        self._as(user, ["tmux", "new-session", "-d", "-s", "login", "-x", "220", "-y", "50", "claude auth login"])
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(1)
            pane = self._as(user, ["tmux", "capture-pane", "-p", "-J", "-t", "login"]).stdout
            m = self.url_re.search(pane)
            if m:
                return m.group(0)
        self.stop_login(user)
        return None

    def send_code(self, user, code):
        self._as(user, ["tmux", "send-keys", "-t", "login", "-l", "--", code])
        self._as(user, ["tmux", "send-keys", "-t", "login", "Enter"])

    def wait_login_ok(self, user, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            if self._as(user, ["claude", "auth", "status"]).returncode == 0:
                return True
        return False

    def stop_login(self, user):
        self._as(user, ["tmux", "kill-session", "-t", "login"])

    def start_agent(self, user):
        subprocess.run(["systemctl", "enable", "--now", "claude-agent@%s.service" % user], capture_output=True, timeout=60)

    def confirm_remote_control(self, user, timeout=90):
        """The first `claude remote-control` on an account waits at 'Enable Remote Control? (y/n)'. The colleague has
        just been told that signing in turns it on, so answer it once, in their agent's tmux session."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            pane = self._as(user, ["tmux", "capture-pane", "-p", "-J", "-t", "agent"]).stdout
            if "Enable Remote Control?" in pane:
                self._as(user, ["tmux", "send-keys", "-t", "agent", "y", "Enter"])
                return True
            if re.search(r"https://claude\.ai/code/\S+", pane):      # already running: nothing to confirm
                return False
        return False

    def service_active(self, user):
        return subprocess.run(["systemctl", "is-active", "--quiet", "claude-agent@%s.service" % user]).returncode == 0

    def logged_in(self, user):
        """Is Claude signed in for this user (claude auth status -> loggedIn)? True when the check itself fails, so a
        flaky check never forces a needless re-login."""
        try:
            out = self._as(user, ["claude", "auth", "status"], timeout=20).stdout
            return bool(json.loads(out).get("loggedIn"))
        except (subprocess.SubprocessError, OSError, ValueError, AttributeError):
            return True

    def session_id(self, user):
        for path in ("/home/%s/.claude/agent-session-id" % user,):
            try:
                with open(path) as f:
                    sid = f.read().strip()
                if sid:
                    return sid
            except OSError:
                pass
        return None

    def running_commands(self, user):
        """Commands the interactive agent is running now, in the foreground or background. Every shell Claude Code
        starts for a Bash call runs `source ~/.claude/shell-snapshots/...`, and the agent's own sit in its unit's
        cgroup (a scheduled job's run is in claude-job-*, and the bridge's own runuser calls source nothing)."""
        import pwd
        try:
            pw = pwd.getpwnam(user)
        except KeyError:
            return 0
        marker = (pw.pw_dir + "/.claude/shell-snapshots/").encode()
        unit = ("/claude-agent@%s.service" % user).encode()
        n = 0
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                if os.stat("/proc/" + pid).st_uid != pw.pw_uid:
                    continue
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    if not counts_as_command(f.read(8192), marker):
                        continue
                with open("/proc/%s/cgroup" % pid, "rb") as f:
                    if unit in f.read():
                        n += 1
            except OSError:                                # the process ended while we looked
                continue
        return n

    def conversation_title(self, user, conversation):
        """Title of one transcript in the agent's folder, read as root without following anything the user planted."""
        import pwd
        from agent_outbox import read_home_file_ends
        if not CONVERSATION_RE.match(conversation):
            return None
        try:
            pw = pwd.getpwnam(user)
        except KeyError:
            return None
        wd = os.path.join(pw.pw_dir, "Documents", "code", "vylo")
        rel = os.path.join(".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", wd), conversation + ".jsonl")
        ends = read_home_file_ends(pw.pw_dir, rel, pw.pw_uid)
        return conversation_title(*ends) if ends else None

    def _pointer(self, user):
        wd = "/home/%s/Documents/code/vylo" % user
        return "/home/%s/.claude/projects/%s/bridge-pointer.json" % (user, re.sub(r"[^A-Za-z0-9]", "-", wd))

    # 240 s: after a stop, claude refuses the folder ("already served") for about a minute, and agent-run retries
    # every 10 s; at 120 s a switch that worked was reported as "restarting".
    def wait_session_id(self, user, timeout=240, other_than=None):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(self._pointer(user)) as f:
                    sid = json.load(f).get("sessionId")
                if sid and sid != other_than:
                    return sid
            except (OSError, ValueError):
                pass
            time.sleep(3)
        return None

    def _restart_with(self, user, script, *args):
        """Stop the agent, run a small shell step as the user (session files), start it again. The start always runs:
        a stop that overran this wait used to raise here and leave the agent down until someone noticed."""
        unit = "claude-agent@%s.service" % user
        try:
            subprocess.run(["systemctl", "stop", unit], capture_output=True, timeout=90)   # the unit kills at 20 s
        except subprocess.TimeoutExpired:
            log.warning("stopping %s overran; starting it again anyway", unit)
        try:
            if script:
                self._as(user, ["sh", "-c", script, "_", self._pointer(user)] + list(args))
        finally:
            subprocess.run(["systemctl", "reset-failed", unit], capture_output=True, timeout=30)
            subprocess.run(["systemctl", "start", unit], capture_output=True, timeout=60)

    def restart_agent(self, user):
        """Stop and start the agent with nothing changed: agent-run restores what Claude can, within the limit."""
        self._restart_with(user, None)

    def max_sessions(self, user):
        """How many sessions this person's agent may serve (agent-max-sessions, read as root). 1 on any error."""
        import pwd
        import agent_sessions
        try:
            pw = pwd.getpwnam(user)
            return agent_sessions.effective_max(user, pw.pw_dir, pw.pw_uid)
        except Exception:                                  # noqa: BLE001
            log.exception("could not read the session limit for %s", user)
            return 1

    def new_session(self, user):
        # agent-run sees ~/.claude/agent-fresh and starts without --session-id / --continue
        self._restart_with(user, 'rm -f "$HOME/.claude/agent-session-id" "$1"; : > "$HOME/.claude/agent-fresh"')

    def switch_session(self, user, sid):
        if not SESSION_RE.match(sid):
            raise ValueError("bad session id")
        self._restart_with(user, 'rm -f "$HOME/.claude/agent-fresh" "$1"; printf "%s\\n" "$2" > "$HOME/.claude/agent-session-id"', sid)

    def set_litellm_key(self, user, key):
        """'saved', 'rejected' (the gateway said 401/403) or 'error'. The key travels on stdin only."""
        try:
            r = subprocess.run([SET_LITELLM_KEY, "--user", user, "--yes"], input=key + "\n",
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return "error"
        if r.returncode == 0:
            return "saved"
        return "rejected" if "rejected the key" in r.stderr else "error"

    def take_outbox(self, user):
        import pwd
        from agent_outbox import take
        try:
            pw = pwd.getpwnam(user)
        except KeyError:
            return []
        return take(pw.pw_dir, pw.pw_uid)

    @staticmethod
    def _inbox_target(user, ask_id):
        import pwd
        from agent_outbox import ASK_ID_RE
        if not isinstance(ask_id, str) or not ASK_ID_RE.match(ask_id):
            raise ValueError("bad ask id")
        return pwd.getpwnam(user), ask_id + ".json"

    def write_inbox(self, user, ask_id, body):
        """The answer to an ask, as ~/.claude/inbox/<ask>.json in the asker's home (owner-checked, no symlinks, 600),
        where their waiting ask-colleague reads and deletes it. Raises on anything unsafe."""
        from agent_outbox import INBOX, write_user_file
        pw, name = self._inbox_target(user, ask_id)
        write_user_file(pw.pw_dir, pw.pw_uid, pw.pw_gid, INBOX, name, body)

    def inbox_taken(self, user, ask_id):
        """True once the asker's agent has read (and so deleted) the answer file."""
        from agent_outbox import INBOX, user_file_exists
        try:
            pw, name = self._inbox_target(user, ask_id)
        except (KeyError, ValueError):
            return False
        return not user_file_exists(pw.pw_dir, pw.pw_uid, INBOX, name)

    def remove_inbox(self, user, ask_id):
        from agent_outbox import INBOX, remove_user_file
        pw, name = self._inbox_target(user, ask_id)
        remove_user_file(pw.pw_dir, pw.pw_uid, INBOX, name)

    def agent_job(self, user, args):
        r = self._as(user, ["/usr/local/bin/agent-job"] + list(args))
        return r.returncode, (r.stdout + r.stderr)

    def launch_run(self, user, ref, request):
        """Write the request to ~/.claude/chat-requests/<ref>.json (owner-checked, no symlinks) and start
        agent-chat-exec as the person in a capped unit, without waiting. The text is never on a command line.
        True when systemd took the unit."""
        import pwd
        from agent_outbox import REQUESTS, remove_user_file, write_user_file
        if not isinstance(ref, str) or not REF_RE.match(ref):
            return False
        try:
            pw = pwd.getpwnam(user)
        except KeyError:
            return False
        home, name = pw.pw_dir, ref + ".json"
        try:
            write_user_file(home, pw.pw_uid, pw.pw_gid, REQUESTS, name, request)
        except OSError as e:
            log.warning("could not write the run request %s for %s: %s", ref, user, e.__class__.__name__)
            return False
        cmd = ["systemd-run", "--no-block", "--collect", "--quiet", "--unit", "claude-chat-%s.%s" % (user, ref),
               "--uid", user, "--gid", user, "--slice", "claudeagents.slice", "--working-directory", home,
               "-p", "MemoryMax=1536M", "-p", "CPUQuota=100%", "-p", "RuntimeMaxSec=660", "-p", "Nice=10",
               "--setenv", "HOME=" + home, "--setenv", "USER=" + user, "--setenv", "TERM=dumb",
               "--setenv", "PATH=%s/.local/bin:/usr/local/bin:/usr/bin:/bin" % home,
               "/usr/local/bin/agent-chat-exec", ref]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd="/")
            ok = r.returncode == 0
            if not ok:
                log.warning("systemd-run refused chat run %s for %s: %s", ref, user, (r.stderr or "").strip()[:200])
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("could not start chat run %s for %s: %s", ref, user, e.__class__.__name__)
            ok = False
        if not ok:
            try:
                remove_user_file(home, pw.pw_uid, REQUESTS, name)
            except OSError:
                pass
        return ok


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    if not (cfg.project and cfg.subscription and cfg.key_path):
        log.error("PROJECT_ID, SUBSCRIPTION_ID and GOOGLE_APPLICATION_CREDENTIALS must be set (bridge.env)")
        return 2
    bridge = Bridge(cfg, PeopleStore(cfg.people_file), GoogleChat(cfg), SystemHost())
    source = PubSubSource(cfg)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    def nudger():
        while not stop.wait(cfg.nudge_check_seconds):
            for check in (bridge.check_nudges, bridge.check_outbox, bridge.check_asks):
                try:
                    check()
                except Exception:                              # noqa: BLE001
                    log.exception("%s failed", check.__name__)
    threading.Thread(target=nudger, daemon=True).start()
    log.info("bridge started: %s/%s, nudges at %s, +%s min, then every %s min", cfg.project, cfg.subscription, cfg.nudge_minutes, cfg.nudge_second_gap, cfg.nudge_repeat)
    while not stop.is_set():
        try:
            bridge.process(source)
        except Exception:                                      # noqa: BLE001
            log.exception("pull failed")
            stop.wait(5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
