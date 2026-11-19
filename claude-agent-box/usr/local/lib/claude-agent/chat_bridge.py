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

Events arrive on a Cloud Pub/Sub pull subscription (no inbound port); replies go out through the Chat
REST API with the service-account key. All outside effects sit behind three small interfaces so the
logic is tested with fakes (tests/test_chat_bridge.py):

    EventSource  pull() -> [(ack_id, event_dict)] ; ack([ack_id])
    Chat         send(space, text)
    Host         start_login / send_code / wait_login_ok / stop_login / start_agent /
                 wait_session_id / service_active / session_id / take_outbox /
                 new_session / switch_session / agent_job

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
from collections import deque

from agent_outbox import label

log = logging.getLogger("chat_bridge")

CODE_RE = re.compile(r"^[A-Za-z0-9_\-#.:~+/=]{16,}$")      # an OAuth code: one long token, no spaces
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
        '(Chat with the agent itself in the Claude app. Ask it to set up reminders and jobs.)')
HELP_MULTI = ('hi — sign in / set up your agent\nstatus — is it running, last activity\n'
              'sessions — your recent conversations, and which are active\n'
              'restart — restart your agent (asks first if it is busy)\n'
              'jobs — your scheduled jobs\njob pause|resume|remove|run NAME — manage one\nhelp — this list\n'
              '(Your agent runs several sessions: start, open and close them in the Claude app. Ask it to set up '
              'reminders and jobs.)')
ACTIVE_SECONDS = 600          # "sessions": a conversation with an audit row this recent is shown as active


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
    def __init__(self, cfg, store, chat, host, now=time.time):
        self.cfg, self.store, self.chat, self.host, self.now = cfg, store, chat, host, now
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
        if low in YES_WORDS:
            self.on_yes(person, m["space"])
            return
        self.pending.pop(person["linux_user"], None)       # anything else withdraws an unanswered "are you sure"
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
        else:
            self.chat.send(m["space"], 'I don\'t know that one. Say "help" for what I can do; chat with your agent in the Claude app.')

    def on_hi(self, person, space):
        name, user = person["name"], person["linux_user"]
        if person["status"] == "active" and self.host.service_active(user):
            self.chat.send(space, "You're all set. Your agent: %s" % (self.link(person.get("session_id")) or "starting up, say status in a minute"))
            return
        url = self.host.start_login(user)
        if not url:
            log.error("could not start sign-in for %s", name)
            self.chat.send(space, "I couldn't start the sign-in just now. Try again in a minute; if it keeps failing, tell your admin.")
            return
        self.store.update(name, status="login_started", login_started_at=self.now())
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
        self.host.start_agent(user)
        self.host.confirm_remote_control(user)       # the first run asks "Enable Remote Control? (y/n)"
        sid = self.host.wait_session_id(user)
        self.store.update(name, status="active", session_id=sid, activated_at=self.now())
        if sid:
            self.chat.send(space, "You're signed in. Your agent \"%s-agent\" is running. Open it in the Claude app: %s\n"
                                  'Say "status" any time.' % (name, self.link(sid)))
        else:
            self.chat.send(space, "You're signed in and your agent is starting. Say \"status\" in a minute for its link.")

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

    # ---- the doorbell: messages agents and jobs leave for their own user
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

    def send(self, space, text):
        r = self.session.post("https://chat.googleapis.com/v1/%s/messages" % space, json={"text": text}, timeout=20)
        r.raise_for_status()


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
                    if marker not in f.read(8192):
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

    def take_outbox(self, user):
        import pwd
        from agent_outbox import take
        try:
            pw = pwd.getpwnam(user)
        except KeyError:
            return []
        return take(pw.pw_dir, pw.pw_uid)

    def agent_job(self, user, args):
        r = self._as(user, ["/usr/local/bin/agent-job"] + list(args))
        return r.returncode, (r.stdout + r.stderr)


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
            for check in (bridge.check_nudges, bridge.check_outbox):
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
