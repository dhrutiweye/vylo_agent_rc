"""The doorbell: a colleague's agent (or one of their scheduled jobs) leaves a message for its OWN user, and the Chat
bridge posts it to that person's DM with the bot.

    writer  notify-me (runs as the colleague)   ->  ~/.claude/outbox/<time>-<random>.json   (mode 600)
    reader  chat_bridge (root)                  ->  take(...) reads, validates and deletes each file

Who sent a message is the Linux user whose home it was found in, never a field inside it, so an agent can only ring
its own user's doorbell. The reader runs as root inside a directory the colleague controls, so every path step is
opened without following symlinks and every file must be a regular file owned by that user with one link: a planted
symlink or hard link cannot make root read (and post) somebody else's file.

Message files (all in ~/.claude/outbox, written by the colleague, read by root):
  {"v": 1, "to": "self", "source": "agent" | "job:<name>", "text": "...", "ts": <epoch>}     note to its own user
  {"v": 1, "kind": "ask", "to": "<colleague>", "ask": "a<10 hex>", "text": "...", "ts": ...}  question for a colleague
  {"v": 1, "kind": "result", "ref": "t|a|d<id>", "status": ok|needs_you|failed|answer|cannot, "text", "ts"}
A plain message addressed to anyone but "self" is dropped; asks go through the bridge's approval flow.
Files root writes into a colleague's home, with the same no-symlink, owner-checked steps (write_user_file):
  ~/.claude/inbox/<ask id>.json         the answer to an ask, read once and deleted by the asking agent (read_inbox)
  ~/.claude/chat-requests/<name>.json   a run request for the colleague's own runner
Standard library only, Python 3.9 compatible (the colleague side runs on the system python3).
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
import uuid

OUTBOX = os.path.join(".claude", "outbox")
INBOX = ".claude/inbox"             # root drops answers here for the colleague's agent: <ask id>.json
REQUESTS = ".claude/chat-requests"   # root drops run requests here for the colleague's own runner
TO_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
REF_RE = re.compile(r"^(t|a|d)[0-9a-z]{1,16}$")
ASK_ID_RE = re.compile(r"^a[0-9a-f]{10}$")
STATUSES = ("ok", "needs_you", "failed", "answer", "cannot")
PLAIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SOURCE_RE = re.compile(r"^(agent|box|job:[a-z0-9][a-z0-9_-]{0,31})$")   # box: claude-agent-monitor, to the admin
NAME_RE = re.compile(r"^[0-9]{10,}-[0-9a-f]{8}\.json$")
MAX_FILE_BYTES = 16384
MAX_CHARS = 3000


# ------------------------------------------------------------------ writer side (runs as the colleague)
def _drop(home, body, max_pending):
    d = os.path.join(home, OUTBOX)
    os.makedirs(d, mode=0o700, exist_ok=True)
    pending = [n for n in os.listdir(d) if NAME_RE.match(n)]
    if len(pending) >= max_pending:
        raise ValueError("%d messages are already waiting to be delivered; not adding more" % len(pending))
    name = "%d-%s.json" % (time.time_ns(), uuid.uuid4().hex[:8])
    tmp = os.path.join(d, ".tmp-" + name)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(body, f)
    os.rename(tmp, os.path.join(d, name))
    return name


def _clean_text(text, max_chars):
    text = (text or "").strip()
    if not text:
        raise ValueError("empty message")
    if len(text) > max_chars:
        text = text[: max_chars - 40].rstrip() + "\n... (cut: message over %d characters)" % max_chars
    return text


def write(home, text, source="agent", max_pending=50, max_chars=MAX_CHARS):
    """Drop one message in home's outbox. Returns the file name. Raises ValueError for an unusable message."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty message")
    if not SOURCE_RE.match(source):
        raise ValueError("bad source %r" % source)
    text = _clean_text(text, max_chars)
    return _drop(home, {"v": 1, "to": "self", "source": source, "text": text, "ts": int(time.time())}, max_pending)


def write_ask(home, to, text, max_pending=50, max_chars=MAX_CHARS):
    """Leave a question for colleague `to`. Returns the ask id ("a" + 10 hex). Raises ValueError."""
    if not isinstance(to, str) or not TO_RE.match(to):
        raise ValueError("bad colleague name %r" % (to,))
    text = _clean_text(text, max_chars)
    ask = "a" + uuid.uuid4().hex[:10]
    _drop(home, {"v": 1, "kind": "ask", "to": to, "ask": ask, "text": text, "ts": int(time.time())}, max_pending)
    return ask


def write_result(home, ref, status, text, max_pending=50, max_chars=MAX_CHARS):
    """Report how the run `ref` ended. Returns the file name. Raises ValueError."""
    if not isinstance(ref, str) or not REF_RE.match(ref):
        raise ValueError("bad ref %r" % (ref,))
    if status not in STATUSES:
        raise ValueError("bad status %r" % (status,))
    text = _clean_text(text, max_chars)
    return _drop(home, {"v": 1, "kind": "result", "ref": ref, "status": status, "text": text,
                        "ts": int(time.time())}, max_pending)


def read_inbox(home, ask_id):
    """Read and delete ~/.claude/inbox/<ask_id>.json (runs as the colleague). The dict, or None if absent/unusable."""
    if not isinstance(ask_id, str) or not ASK_ID_RE.match(ask_id):
        return None
    uid = os.getuid()
    try:
        dfd = open_user_dir(home, INBOX, uid)
    except OSError:
        return None
    try:
        name = ask_id + ".json"
        raw = read_user_file(dfd, name, uid)
        if raw is None:
            return None
        try:
            os.unlink(name, dir_fd=dfd)
        except OSError:
            pass
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    finally:
        os.close(dfd)


# ------------------------------------------------------------------ safe reading inside a colleague's home (runs as root)
def _open_dir(path_fd, name, uid, create=False, gid=None):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=path_fd)
    except FileNotFoundError:
        if not create:
            raise
        os.mkdir(name, 0o700, dir_fd=path_fd)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=path_fd)
        if os.geteuid() == 0:
            os.chown(fd, uid, gid if gid is not None else -1)
    st = os.fstat(fd)
    if st.st_uid != uid:
        os.close(fd)
        raise PermissionError("%s is not owned by uid %d" % (name, uid))
    return fd


def _rel_parts(rel):
    parts = [p for p in rel.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise PermissionError("bad path %r" % rel)
    return parts


def open_user_dir(home, rel, uid, create=False, gid=None):
    """fd of home/rel, opened one step at a time with no symlinks, every step owned by uid. Raises OSError.
    create=True makes a missing step (mode 700, owned by uid:gid when running as root)."""
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != uid:
            raise PermissionError("%s is not owned by uid %d" % (home, uid))
        for part in _rel_parts(rel):
            nfd = _open_dir(fd, part, uid, create, gid)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_user_file(dir_fd, name, uid, max_bytes=MAX_FILE_BYTES):
    """Content of one file in an already-opened user directory, or None if it is not a plain file of that user."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_nlink != 1 or st.st_size > max_bytes:
            return None
        return f.read(max_bytes + 1)[:max_bytes].decode("utf-8", "replace")


def read_home_file_ends(home, rel_path, uid, head=262144, tail=65536):
    """(first `head` bytes, last `tail` bytes) of a file under a colleague's home, however large; same safety rules as
    read_user_file. None if absent or unsafe. For a conversation log: its first question is near the top, a title
    set later is near the end."""
    d, name = os.path.split(rel_path)
    try:
        dfd = open_user_dir(home, d, uid)
    except OSError:
        return None
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        except OSError:
            return None
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_nlink != 1:
                return None
            first = f.read(head)
            f.seek(max(0, st.st_size - tail))
            last = f.read(tail)
        return first.decode("utf-8", "replace"), last.decode("utf-8", "replace")
    finally:
        os.close(dfd)


def read_home_file(home, rel_path, uid, max_bytes=262144):
    """A file under a colleague's home (for example .claude/jobs.json), read safely as root. None if absent or unsafe."""
    d, name = os.path.split(rel_path)
    try:
        fd = open_user_dir(home, d, uid)
    except OSError:
        return None
    try:
        return read_user_file(fd, name, uid, max_bytes)
    finally:
        os.close(fd)


def _plain_name(name):
    if not isinstance(name, str) or not PLAIN_NAME_RE.match(name):
        raise PermissionError("bad file name %r" % (name,))
    return name


def write_user_file(home, uid, gid, rel_dir, name, data):
    """Write `data` as JSON to home/rel_dir/name for the colleague (root side): every step opened without following
    symlinks, created directories mode 700, file via temp + rename (replaces whatever is there, never follows it),
    mode 600, owned by uid:gid. Raises OSError on anything unsafe."""
    _plain_name(name)
    dfd = open_user_dir(home, rel_dir, uid, create=True, gid=gid)
    tmp = ".tmp-%s-%s" % (uuid.uuid4().hex[:8], name)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        try:
            with os.fdopen(fd, "w") as f:
                if os.geteuid() == 0:
                    os.fchown(f.fileno(), uid, gid)
                json.dump(data, f)
            os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
    finally:
        os.close(dfd)


def user_file_exists(home, uid, rel_dir, name):
    """True if home/rel_dir/name is a plain file of the user (no symlink, one link). False for anything else."""
    try:
        _plain_name(name)
        dfd = open_user_dir(home, rel_dir, uid)
    except OSError:
        return False
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        return stat.S_ISREG(st.st_mode) and st.st_uid == uid and st.st_nlink == 1
    except OSError:
        return False
    finally:
        os.close(dfd)


def remove_user_file(home, uid, rel_dir, name):
    """Delete home/rel_dir/name (the entry itself, never a symlink's target). Missing is fine; unsafe path raises."""
    _plain_name(name)
    try:
        dfd = open_user_dir(home, rel_dir, uid)
    except FileNotFoundError:
        return
    try:
        try:
            os.unlink(name, dir_fd=dfd)
        except FileNotFoundError:
            pass
    finally:
        os.close(dfd)


def take(home, uid, limit=20):
    """Read, validate and delete up to `limit` waiting messages, oldest first. Returns [{"kind": "self"|"ask"|"result", ...}]: self has source/text/ts, ask has to/ask/text/ts,
    result has ref/status/text/ts.
    A file that is unsafe or malformed is deleted and skipped; a message addressed to anyone but the sender is dropped."""
    try:
        dfd = open_user_dir(home, OUTBOX, uid)
    except OSError:
        return []
    out = []
    try:
        for name in sorted(n for n in os.listdir(dfd) if NAME_RE.match(n))[:limit]:
            raw = read_user_file(dfd, name, uid)
            try:
                os.unlink(name, dir_fd=dfd)
            except OSError:
                pass
            if raw is None:
                continue
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            text = str(m.get("text") or "").strip()[:MAX_CHARS]
            ts = m.get("ts") if isinstance(m.get("ts"), int) else None
            kind = m.get("kind", "self")
            if not text:
                continue
            if kind == "ask":
                to, ask = m.get("to"), m.get("ask")
                if (isinstance(to, str) and TO_RE.match(to)) and (isinstance(ask, str) and ASK_ID_RE.match(ask)):
                    out.append({"kind": "ask", "to": to, "ask": ask, "text": text, "ts": ts})
                continue
            if kind == "result":
                ref, status = m.get("ref"), m.get("status")
                if (isinstance(ref, str) and REF_RE.match(ref)) and status in STATUSES:
                    out.append({"kind": "result", "ref": ref, "status": status, "text": text, "ts": ts})
                continue
            if kind != "self" or m.get("to", "self") != "self":
                continue
            source = str(m.get("source") or "agent")
            out.append({"kind": "self", "source": source if SOURCE_RE.match(source) else "agent", "text": text,
                        "ts": ts})
    finally:
        os.close(dfd)
    return out


def label(source):
    """What a delivered message starts with: [agent], [box] or [job <name>]."""
    if source == "box":
        return "[box]"
    return "[job %s]" % source[4:] if source.startswith("job:") else "[agent]"
