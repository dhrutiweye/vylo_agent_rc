"""How many Claude sessions one person's agent may serve at once.

The admin sets it in /etc/claude-code/agent-sessions.conf (`default=N`, `name=N`, 1..5). The answer is forced to 1
when the person's Claude account carries an org policy (~/.claude/remote-settings.json) that does not set
`"managedSourcesBehavior": "merge"`: Claude then skips the box's managed-settings.json, so the guard that keeps
sessions apart would not run. A policy file that cannot be read or parsed counts the same way (fail closed).
"""
import json
import os

CONF_PATH = "/etc/claude-code/agent-sessions.conf"
INSTALLED_CLI = "/usr/local/bin/agent-max-sessions"
POLICY_REL = ".claude/remote-settings.json"
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


def effective_max(user, home, uid, conf_path=CONF_PATH):
    try:
        with open(conf_path, encoding="utf-8", errors="replace") as f:
            conf_text = f.read(65536)
    except OSError:
        conf_text = ""
    n = configured_max(user, conf_text)
    if n == 1:
        return 1
    present, text = _read_policy(home, uid)
    if present and (text is None or not policy_allows_multi(text)):
        return 1
    return n
