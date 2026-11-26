"""Secret check: does a piece of text look like a key, token or private key?

The Chat bridge never forwards, posts or stores text that does (a colleague pasting a key into a DM, or an agent
putting one in a reply). It is deliberately a blunt, cheap, pure function: a false positive costs one re-typed
message, a false negative leaks a credential into Chat and the audit trail.

    looks_secret(text) -> True when any known secret shape appears anywhere in the text

Shapes: OpenAI-style sk-, GitHub ghp_ and github_pat_, AWS AKIA, Slack xox?-, three-part JWT, PEM private key header,
and any long token (32+ characters of [A-Za-z0-9_-+/=]) holding an upper, a lower and a digit that is not pure hex
(so git SHAs and UUIDs pass). The text itself is never logged here.
Standard library only, Python 3.9 compatible.
"""
from __future__ import annotations

import re

_SHAPES = [
    re.compile(r"sk-[A-Za-z0-9_-]{24,}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"AKIA[A-Z0-9]{16}"),
    re.compile(r"xox[a-z]-[A-Za-z0-9-]+"),
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")
_HEX = re.compile(r"[0-9a-fA-F]+")


def looks_secret(text: str) -> bool:
    if not text:
        return False
    if any(p.search(text) for p in _SHAPES):
        return True
    for tok in _LONG_TOKEN.findall(text):
        if (re.search(r"[A-Z]", tok) and re.search(r"[a-z]", tok) and re.search(r"[0-9]", tok)
                and not _HEX.fullmatch(tok)):
            return True
    return False
