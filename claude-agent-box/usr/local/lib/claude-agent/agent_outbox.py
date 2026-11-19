"""The doorbell: a colleague's agent (or one of their scheduled jobs) leaves a message for its OWN user, and the Chat
bridge posts it to that person's DM with the bot.

    writer  notify-me (runs as the colleague)   ->  ~/.claude/outbox/<time>-<random>.json   (mode 600)
    reader  chat_bridge (root)                  ->  take(...) reads, validates and deletes each file

Who sent a message is the Linux user whose home it was found in, never a field inside it, so an agent can only ring
its own user's doorbell. The reader runs as root inside a directory the colleague controls, so every path step is
opened without following symlinks and every file must be a regular file owned by that user with one link: a planted
symlink or hard link cannot make root read (and post) somebody else's file.

Message file: {"v": 1, "to": "self", "source": "agent" | "job:<name>", "text": "...", "ts": <epoch>}
"to" is "self" only for now; messaging a colleague is a later phase and is refused here.
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
SOURCE_RE = re.compile(r"^(agent|box|job:[a-z0-9][a-z0-9_-]{0,31})$")   # box: claude-agent-monitor, to the admin
NAME_RE = re.compile(r"^[0-9]{10,}-[0-9a-f]{8}\.json$")
MAX_FILE_BYTES = 16384
MAX_CHARS = 3000


# ------------------------------------------------------------------ writer side (runs as the colleague)
def write(home, text, source="agent", max_pending=50, max_chars=MAX_CHARS):
    """Drop one message in home's outbox. Returns the file name. Raises ValueError for an unusable message."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty message")
    if not SOURCE_RE.match(source):
        raise ValueError("bad source %r" % source)
    if len(text) > max_chars:
        text = text[: max_chars - 40].rstrip() + "\n... (cut: message over %d characters)" % max_chars
    d = os.path.join(home, OUTBOX)
    os.makedirs(d, mode=0o700, exist_ok=True)
    pending = [n for n in os.listdir(d) if NAME_RE.match(n)]
    if len(pending) >= max_pending:
        raise ValueError("%d messages are already waiting to be delivered; not adding more" % len(pending))
    name = "%d-%s.json" % (time.time_ns(), uuid.uuid4().hex[:8])
    tmp = os.path.join(d, ".tmp-" + name)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"v": 1, "to": "self", "source": source, "text": text, "ts": int(time.time())}, f)
    os.rename(tmp, os.path.join(d, name))
    return name


# ------------------------------------------------------------------ safe reading inside a colleague's home (runs as root)
def _open_dir(path_fd, name, uid):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=path_fd)
    st = os.fstat(fd)
    if st.st_uid != uid:
        os.close(fd)
        raise PermissionError("%s is not owned by uid %d" % (name, uid))
    return fd


def open_user_dir(home, rel, uid):
    """fd of home/rel, opened one step at a time with no symlinks, every step owned by uid. Raises OSError."""
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != uid:
            raise PermissionError("%s is not owned by uid %d" % (home, uid))
        for part in [p for p in rel.split("/") if p]:
            nfd = _open_dir(fd, part, uid)
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


def take(home, uid, limit=20):
    """Read, validate and delete up to `limit` waiting messages, oldest first. Returns [{"source", "text", "ts"}].
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
            if not isinstance(m, dict) or m.get("to", "self") != "self":
                continue
            text = str(m.get("text") or "").strip()[:MAX_CHARS]
            source = str(m.get("source") or "agent")
            if not text:
                continue
            out.append({"source": source if SOURCE_RE.match(source) else "agent", "text": text,
                        "ts": m.get("ts") if isinstance(m.get("ts"), int) else None})
    finally:
        os.close(dfd)
    return out


def label(source):
    """What a delivered message starts with: [agent], [box] or [job <name>]."""
    if source == "box":
        return "[box]"
    return "[job %s]" % source[4:] if source.startswith("job:") else "[agent]"
