"""Token names/symbols are untrusted text: clean them before publishing anywhere."""
import re

_CTRL = re.compile(r"[\u0000-\u001f\u007f​-‏‪-‮⁠-⁤﻿]")
_URL = re.compile(r"(https?://\S+|www\.\S+|t\.me/\S+|\b[\w-]+\.(?:com|io|xyz|net|org|fun|app|gg|me|co|ai|so)\b\S*)",
                  re.IGNORECASE)
_HANDLE = re.compile(r"@\w+")


def clean_text(text, max_len):
    """Strip control/zero-width chars, URLs and @handles; collapse spaces; cap length."""
    s = _CTRL.sub("", text or "")
    s = _HANDLE.sub("", _URL.sub("", s))
    s = " ".join(s.split())
    return s if len(s) <= max_len else s[:max_len - 1].rstrip() + "…"


def word_hits(text, words):
    """Case-insensitive substring hits (strict on purpose: catches 'SafeMoon', 'GEMS')."""
    low = (text or "").lower()
    return [w for w in words if w.lower() in low]
