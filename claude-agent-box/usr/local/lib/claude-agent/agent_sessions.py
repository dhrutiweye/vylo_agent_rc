"""How many Claude sessions one person's agent may serve at once.

The admin sets it in /etc/claude-code/agent-sessions.conf (`default=N`, `name=N`, 1..5). The answer is forced to 1
when the person's Claude account carries an org policy (~/.claude/remote-settings.json) that does not set
`"managedSourcesBehavior": "merge"`: Claude then skips the box's managed-settings.json, so the guard that keeps
sessions apart would not run. A policy file that cannot be read or parsed counts the same way (fail closed).

Unless the box's rules are locked into the person's own settings instead: ~/.claude/settings.json owned by root,
immutable, not writable by anyone else, with the guard on Bash (lock-box-rules writes it). Claude still runs user hooks
under an org policy, as long as that policy sets neither allowManagedHooksOnly nor disableAllHooks.
"""
import json
import os
import stat

CONF_PATH = "/etc/claude-code/agent-sessions.conf"
INSTALLED_CLI = "/usr/local/bin/agent-max-sessions"
POLICY_REL = ".claude/remote-settings.json"
USER_RULES = "settings.json"                # in ~/.claude
GUARD = "/usr/local/bin/claude-guard.sh"
FS_IOC_GETFLAGS, FS_IMMUTABLE_FL = 0x80086601, 0x10
LOWEST, HIGHEST = 1, 5


def _clamp(n):
    return max(LOWEST, min(HIGHEST, n))


def configured_max(user, conf_text):
    """The admin's number for `user`: their own line, else `default=`, else 1. Clamped to 1..5."""
    found = {}
    for line in (conf_text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        try:
            found[key.strip()] = int(value.strip())
        except ValueError:
            continue
    if user in found:
        return _clamp(found[user])
    if "default" in found:
        return _clamp(found["default"])
    return 1


def policy_allows_multi(policy_text):
    """True when there is no org policy (None) or it asks Claude to merge it with the box's own."""
    if policy_text is None:
        return True
    try:
        policy = json.loads(policy_text)
    except ValueError:
        return False
    return isinstance(policy, dict) and policy.get("managedSourcesBehavior") == "merge"


def policy_blocks_user_hooks(policy_text):
    """True unless the policy parses and leaves user hooks alone (no allowManagedHooksOnly, no disableAllHooks)."""
    try:
        policy = json.loads(policy_text or "")
    except ValueError:
        return True
    return not isinstance(policy, dict) or bool(policy.get("allowManagedHooksOnly")) or bool(policy.get("disableAllHooks"))


def _is_immutable(fd):
    import array
    import fcntl
    try:
        buf = array.array("l", [0])
        fcntl.ioctl(fd, FS_IOC_GETFLAGS, buf, True)
        return bool(buf[0] & FS_IMMUTABLE_FL)
    except (OSError, ImportError):
        return False


def _guards_bash(rules):
    for entry in ((rules.get("hooks") or {}).get("PreToolUse") or []) if isinstance(rules, dict) else []:
        matcher = entry.get("matcher", "") if isinstance(entry, dict) else ""
        if "Bash" in matcher.split("|") and any(isinstance(h, dict) and h.get("command") == GUARD
                                                for h in entry.get("hooks") or []):
            return True
    return False


def box_rules_locked(home, uid, root_uid=0, is_immutable=_is_immutable):
    """True when ~/.claude/settings.json is the box's rules, locked: a plain file owned by root (not a link, one hard
    link, not writable by group or others), immutable, and running the guard on Bash. Reached one directory at a time
    with no symlinks, each owned by the person, so it is safe as root and as the person."""
    import agent_outbox
    try:
        dfd = agent_outbox.open_user_dir(home, ".claude", uid)
    except OSError:
        return False
    try:
        fd = os.open(USER_RULES, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except OSError:
        os.close(dfd)
        return False
    os.close(dfd)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != root_uid or st.st_nlink != 1 or st.st_mode & 0o022
                or st.st_size > 262144 or not is_immutable(f.fileno())):
            return False
        try:
            return _guards_bash(json.loads(f.read().decode("utf-8", "replace")))
        except ValueError:
            return False


def _read_policy(home, uid):
    """(present, text). Absent → (False, None). Present but unreadable or unsafe → (True, None)."""
    path = os.path.join(home, POLICY_REL)
    if not os.path.lexists(path):
        return False, None
    if os.geteuid() == 0:
        import agent_outbox
        return True, agent_outbox.read_home_file(home, POLICY_REL, uid)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return True, f.read(262144)
    except OSError:
        return True, None


def cli_inputs(env, script_path, pw_home):
    """(conf path, home) for agent-max-sessions. AGENT_SESSIONS_CONF and HOME are honoured only by a copy that is not
    the installed one (the tests'): an agent can set both in its own environment, and must not choose its own limit."""
    if os.path.realpath(script_path) == INSTALLED_CLI:
        return CONF_PATH, pw_home
    return env.get("AGENT_SESSIONS_CONF") or CONF_PATH, env.get("HOME") or pw_home


def org_policy_ok(home, uid, locked=box_rules_locked):
    """True when the box's guard runs for this person: no org policy, an org policy that merges with the box's
    managed settings, or one that leaves user hooks alone while the box's rules are locked into their own settings.
    A policy file that is present but unreadable or unsafe is never OK."""
    present, text = _read_policy(home, uid)
    if not present:
        return True
    if text is None:
        return False
    if policy_allows_multi(text):
        return True
    return not policy_blocks_user_hooks(text) and locked(home, uid)


def effective_max(user, home, uid, conf_path=CONF_PATH, locked=box_rules_locked):
    try:
        with open(conf_path, encoding="utf-8", errors="replace") as f:
            conf_text = f.read(65536)
    except OSError:
        conf_text = ""
    n = configured_max(user, conf_text)
    if n == 1:
        return 1
    return n if org_policy_ok(home, uid, locked) else 1
