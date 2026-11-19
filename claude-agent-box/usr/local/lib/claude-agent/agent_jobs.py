"""Scheduled jobs for colleagues' agents: reminders (fixed text to Chat) and tasks (a fresh headless `claude -p`).

    agent-job          (runs as the colleague)  adds/lists/pauses/removes jobs in ~/.claude/jobs.json
    agent-job-exec     (runs as the colleague, in its own capped systemd unit)  runs ONE task and reports to Chat
    claude-agent-jobs  (root service)  decides what is due and starts it, enforcing every limit itself

The colleague owns only the definitions. When a job last ran, how many tasks ran today, failures and pauses live in
root's state file (/var/lib/claude-agent-jobs/<user>.json, readable by that user), so editing jobs.json by hand
cannot make anything run more often than the limits allow.

Limits come from /etc/claude-code/agent-jobs.env (KEY=value, no secrets, readable by everyone); defaults are below.
Standard library only, Python 3.9 compatible.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import time

CONF_FILE = "/etc/claude-code/agent-jobs.env"   # world-readable: agent-job shows the same limits the runner enforces
STATE_DIR = "/var/lib/claude-agent-jobs"
JOBS_FILE = os.path.join(".claude", "jobs.json")
RESULTS_DIR = os.path.join(".claude", "job-results")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DEFAULTS = {
    "JOB_MAX_PER_USER": "10",            # job definitions per person (extra ones are ignored)
    "JOB_MIN_REMIND_MINUTES": "15",      # shortest repeat for a reminder
    "JOB_MIN_TASK_MINUTES": "60",        # shortest repeat for a task
    "JOB_TASK_DAILY_CAP": "24",          # task runs per person per day (run-now included)
    "JOB_TASK_TIMEOUT_SECONDS": "600",   # one task run, then it is stopped
    "JOB_MAX_TURNS": "25",               # claude -p --max-turns
    "JOB_CONCURRENT_BOX": "2",           # tasks running at once on the whole box (one per person at most)
    "JOB_MEMORY_MAX": "1536M",           # per task run
    "JOB_CPU_QUOTA": "100%",             # per task run
    "JOB_FAILURES_TO_PAUSE": "3",        # failures in a row before a job pauses itself
    "JOB_TZ": "Asia/Kolkata",            # times people type and see
}
MAX_REMIND_CHARS = 1000
MAX_PROMPT_CHARS = 4000


# ------------------------------------------------------------------ config and time
class Limits:
    def __init__(self, env=None):
        v = dict(DEFAULTS)
        v.update(env or {})
        self.max_jobs = int(v["JOB_MAX_PER_USER"])
        self.min_remind = int(v["JOB_MIN_REMIND_MINUTES"]) * 60
        self.min_task = int(v["JOB_MIN_TASK_MINUTES"]) * 60
        self.daily_cap = int(v["JOB_TASK_DAILY_CAP"])
        self.timeout = int(v["JOB_TASK_TIMEOUT_SECONDS"])
        self.max_turns = int(v["JOB_MAX_TURNS"])
        self.concurrent = int(v["JOB_CONCURRENT_BOX"])
        self.memory_max = v["JOB_MEMORY_MAX"]
        self.cpu_quota = v["JOB_CPU_QUOTA"]
        self.failures_to_pause = int(v["JOB_FAILURES_TO_PAUSE"])
        self.tz = get_tz(v["JOB_TZ"])


def load_limits(path=CONF_FILE):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, val = line.split("=", 1)
                    if k.strip() in DEFAULTS:
                        env[k.strip()] = val.strip()
    except OSError:
        pass
    return Limits(env)


def get_tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:                                      # noqa: BLE001 - no tz database: fall back
        if name == "Asia/Kolkata":
            return dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
        return dt.timezone.utc


def fmt_time(epoch, tz):
    return dt.datetime.fromtimestamp(epoch, tz).strftime("%a %d %b %H:%M") if epoch else "-"


# ------------------------------------------------------------------ schedules
DUR_RE = re.compile(r"^(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)$")
HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _duration(s):
    m = DUR_RE.match(s.strip().lower())
    if not m:
        raise ValueError("cannot read %r as a duration (examples: 30m, 2h, 1d)" % s)
    n, unit = int(m.group(1)), m.group(2)[0]
    return n * {"m": 60, "h": 3600, "d": 86400}[unit]


def parse_when(text, now, tz):
    """'at 2026-11-07 16:00' | 'at 16:00' | 'in 30m' | 'every 2h' | 'daily 09:30' | 'weekdays 10:00' -> schedule dict."""
    words = (text or "").strip().split(None, 1)
    if len(words) != 2:
        raise ValueError("say when: at <date time>, in <30m>, every <2h>, daily <HH:MM> or weekdays <HH:MM>")
    kind, rest = words[0].lower(), words[1].strip()
    if kind == "in":
        return {"type": "at", "ts": int(now + _duration(rest))}
    if kind == "every":
        return {"type": "every", "seconds": _duration(rest)}
    if kind in ("daily", "weekdays"):
        if not HHMM_RE.match(rest):
            raise ValueError("use a 24-hour time like 09:30")
        h, m = rest.split(":")
        return {"type": kind, "time": "%02d:%02d" % (int(h), int(m))}
    if kind == "at":
        local_now = dt.datetime.fromtimestamp(now, tz)
        if HHMM_RE.match(rest):
            h, m = (int(x) for x in rest.split(":"))
            t = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
            if t.timestamp() <= now:
                t += dt.timedelta(days=1)
            return {"type": "at", "ts": int(t.timestamp())}
        try:
            t = dt.datetime.strptime(rest, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        except ValueError:
            raise ValueError("use 'at YYYY-MM-DD HH:MM' or 'at HH:MM'")
        if t.timestamp() <= now:
            raise ValueError("that time has already passed")
        return {"type": "at", "ts": int(t.timestamp())}
    raise ValueError("unknown schedule %r: use at, in, every, daily or weekdays" % kind)


def describe(sched, tz):
    t = sched.get("type")
    if t == "at":
        return "once at " + fmt_time(sched["ts"], tz)
    if t == "every":
        s = sched["seconds"]
        return "every %s" % ("%dd" % (s // 86400) if s % 86400 == 0 else "%dh" % (s // 3600) if s % 3600 == 0 else "%dm" % (s // 60))
    return "%s at %s" % (t, sched.get("time"))


def _last_occurrence(sched, now, tz):
    """Most recent daily/weekdays time at or before now (epoch)."""
    h, m = (int(x) for x in sched["time"].split(":"))
    day = dt.datetime.fromtimestamp(now, tz).replace(hour=h, minute=m, second=0, microsecond=0)
    if day.timestamp() > now:
        day -= dt.timedelta(days=1)
    while sched["type"] == "weekdays" and day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day.timestamp()


def interval_floor(job, limits):
    return limits.min_task if job.get("kind") == "task" else limits.min_remind


def next_run(job, st, now, tz, limits):
    """When the job is next due (epoch), or None if it never will be again."""
    sched, last = job["schedule"], st.get("last_run")
    if sched["type"] == "at":
        return None if st.get("done") else sched["ts"]
    if sched["type"] == "every":
        return (last or job["created"]) + max(sched["seconds"], interval_floor(job, limits))
    occ = _last_occurrence(sched, now, tz)
    if occ >= job["created"] and (last is None or last < occ):
        return occ                                          # due now (or missed while the box was down)
    nxt = dt.datetime.fromtimestamp(occ, tz) + dt.timedelta(days=1)
    while sched["type"] == "weekdays" and nxt.weekday() >= 5:
        nxt += dt.timedelta(days=1)
    return nxt.timestamp()


def is_paused(job, st):
    if job.get("paused"):
        return True
    return bool(st.get("paused_at")) and not (job.get("resumed_at", 0) > st["paused_at"])


def due(job, st, now, tz, limits):
    """(True, why) when the job should run at `now`. Missed runs (box down) run once, not once per miss."""
    if is_paused(job, st):
        return False, "paused"
    if job.get("run_now", 0) > st.get("run_now_done", 0):
        return True, "run now"
    n = next_run(job, st, now, tz, limits)
    if n is None:
        return False, "done"
    return (now >= n), "scheduled"


def valid_job(j, limits):
    """Shape check of one definition read from a colleague's file. Returns the job or None."""
    try:
        if not NAME_RE.match(j["name"]) or j["kind"] not in ("remind", "task"):
            return None
        s = j["schedule"]
        if s["type"] == "at":
            int(s["ts"])
        elif s["type"] == "every":
            if int(s["seconds"]) <= 0:
                return None
        elif s["type"] in ("daily", "weekdays"):
            if not HHMM_RE.match(s["time"]):
                return None
        else:
            return None
        body = j["text"] if j["kind"] == "remind" else j["prompt"]
        if not isinstance(body, str) or not body.strip():
            return None
        int(j["created"])
        return j
    except (KeyError, TypeError, ValueError):
        return None


def parse_jobs(raw, limits):
    """Definitions from the colleague's jobs.json text: only valid ones, first limits.max_jobs, unique names."""
    try:
        d = json.loads(raw) if raw else {}
    except ValueError:
        return []
    out, seen = [], set()
    for j in (d.get("jobs") or []) if isinstance(d, dict) else []:
        j = valid_job(j, limits) if isinstance(j, dict) else None
        if j and j["name"] not in seen:
            seen.add(j["name"])
            out.append(j)
        if len(out) >= limits.max_jobs:
            break
    return out


# ------------------------------------------------------------------ the colleague's own store (agent-job)
class JobStore:
    def __init__(self, home):
        self.path = os.path.join(home, JOBS_FILE)
        self.lock_path = self.path + ".lock"

    def read(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            return d if isinstance(d, dict) and isinstance(d.get("jobs"), list) else {"jobs": []}
        except (OSError, ValueError):
            return {"jobs": []}

    def change(self, fn):
        """Run fn(data) under a lock and save. fn may raise ValueError to refuse."""
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        with open(self.lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            d = self.read()
            result = fn(d)
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(d, f, indent=2)
            os.replace(tmp, self.path)
            return result


def new_job(name, kind, body, when, now, limits, existing):
    if not NAME_RE.match(name or ""):
        raise ValueError("job name: lower-case letters, digits, - and _, up to 32 characters")
    if any(j.get("name") == name for j in existing):
        raise ValueError("there is already a job called %s" % name)
    live = [j for j in existing if not j.get("finished")]
    if len(live) >= limits.max_jobs:
        raise ValueError("you already have %d jobs, the limit; remove one first" % limits.max_jobs)
    body = (body or "").strip()
    if not body:
        raise ValueError("nothing to send or do")
    cap = MAX_REMIND_CHARS if kind == "remind" else MAX_PROMPT_CHARS
    if len(body) > cap:
        raise ValueError("too long: at most %d characters" % cap)
    sched = parse_when(when, now, limits.tz)
    job = {"name": name, "kind": kind, "schedule": sched, "created": int(now)}
    job["text" if kind == "remind" else "prompt"] = body
    if sched["type"] == "every" and sched["seconds"] < interval_floor(job, limits):
        raise ValueError("a %s can repeat at most every %d minutes" % ("task" if kind == "task" else "reminder",
                                                                       interval_floor(job, limits) // 60))
    return job


# ------------------------------------------------------------------ root state
def state_path(user, base=STATE_DIR):
    return os.path.join(base, user + ".json")


def load_state(user, base=STATE_DIR):
    try:
        with open(state_path(user, base)) as f:
            d = json.load(f)
        if isinstance(d, dict):
            d.setdefault("jobs", {})
            return d
    except (OSError, ValueError):
        pass
    return {"jobs": {}}


def save_state(user, d, gid=None, base=STATE_DIR):
    os.makedirs(base, mode=0o711, exist_ok=True)
    tmp = state_path(user, base) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.chmod(tmp, 0o640)
    if gid is not None:
        os.chown(tmp, 0, gid)
    os.replace(tmp, state_path(user, base))


# ------------------------------------------------------------------ the runner (root)
class Runner:
    """One tick() a minute. host: people() / agent_active(user) / read_jobs(user) -> text / notify(user, text, source) /
    launch(user, job, limits) -> handle with .poll() -> None | exit code / running_units() -> [(user, name)] / gid(user)."""

    def __init__(self, host, limits, now=time.time, state_dir=STATE_DIR):
        self.host, self.limits, self.now, self.state_dir = host, limits, now, state_dir
        self.running = {}                                   # (user, name) -> handle

    def _today(self):
        return dt.datetime.fromtimestamp(self.now(), self.limits.tz).strftime("%Y-%m-%d")

    def reap(self):
        """Record finished task runs: failures in a row pause a job."""
        for (user, name), h in list(self.running.items()):
            rc = h.poll()
            if rc is None:
                continue
            del self.running[(user, name)]
            st = load_state(user, self.state_dir)
            js = st["jobs"].setdefault(name, {})
            js["last_finished"] = int(self.now())
            if rc == 0:
                js["failures"], js["last_status"] = 0, "ok"
            else:
                js["failures"] = js.get("failures", 0) + 1
                js["last_status"] = "failed"
                if js["failures"] >= self.limits.failures_to_pause:
                    js["paused_at"] = int(self.now())
                    self.host.notify(user, "Job %s failed %d times in a row and is paused. Fix it, then ask your agent to "
                                           "run: agent-job resume %s" % (name, js["failures"], name), "job:" + name)
            save_state(user, st, self.host.gid(user), self.state_dir)

    def tick(self):
        self.reap()
        now = self.now()
        busy = set(self.running) | set(self.host.running_units())
        started = 0
        for p in self.host.people():
            if p.get("status") != "active":
                continue
            user = p["linux_user"]
            if not self.host.agent_active(user):
                continue                                    # agent stopped: its jobs wait
            jobs = parse_jobs(self.host.read_jobs(user), self.limits)
            st = load_state(user, self.state_dir)
            if st.get("day") != self._today():
                st["day"], st["task_runs_today"] = self._today(), 0
            changed = False
            names = {j["name"] for j in jobs}
            for gone in [n for n in st["jobs"] if n not in names]:
                del st["jobs"][gone]
                changed = True
            for job in jobs:
                name = job["name"]
                js = st["jobs"].setdefault(name, {})
                ok, why = due(job, js, now, self.limits.tz, self.limits)
                if not ok or (user, name) in busy:
                    continue
                if job["kind"] == "task":
                    if st.get("task_runs_today", 0) >= self.limits.daily_cap:
                        if st.get("cap_notified") != st["day"]:
                            st["cap_notified"] = st["day"]
                            self.host.notify(user, "Your scheduled tasks reached today's limit of %d runs; the rest wait until "
                                                   "tomorrow." % self.limits.daily_cap, "agent")
                            changed = True
                        continue
                    if any(u == user for u, _ in busy):
                        continue                            # one task at a time per person
                    if len(busy) >= self.limits.concurrent:
                        continue                            # box-wide cap
                    self.running[(user, name)] = self.host.launch(user, job, self.limits)
                    busy.add((user, name))
                    started += 1
                    st["task_runs_today"] = st.get("task_runs_today", 0) + 1
                    js["last_status"] = "running"
                else:
                    self.host.notify(user, job["text"], "job:" + name)
                    js["last_status"] = "ok"
                js["last_run"] = int(now)
                if why == "run now":
                    js["run_now_done"] = job.get("run_now", 0)
                if job["schedule"]["type"] == "at" and why != "run now":
                    js["done"] = True
                changed = True
            if changed:
                save_state(user, st, self.host.gid(user), self.state_dir)
        return started
