from __future__ import annotations

import html
import re
from typing import Iterable

from telethon import types, utils


def resolve_peer_id(peer: object) -> int:
    try:
        raw_id = utils.get_peer_id(peer)
        if isinstance(peer, (types.PeerChannel, types.PeerChat)):
            raw = str(raw_id)
            if not raw.startswith("-100") and not raw.startswith("-"):
                return int(f"-100{raw}")
        return int(raw_id)
    except Exception:
        return 0


def dialog_filter_title(folder: types.DialogFilter) -> str:
    raw = folder.title
    return raw.text if hasattr(raw, "text") else str(raw)


def build_message_link(chat: object, chat_id: int, msg_id: int) -> str:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    raw = str(abs(chat_id))
    if raw.startswith("100") and len(raw) >= 12:
        return f"https://t.me/c/{raw[3:]}/{msg_id}"
    return ""


def format_duration(seconds: float) -> str:
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分")
    return " ".join(parts) or "不足1分钟"


def normalize_pattern_from_terms(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("empty pattern")
    regex_hint = any(ch in raw for ch in ["\\", "(", ")", "[", "]", "{", "}", "|", ".", "+", "?", "^", "$"])
    if regex_hint:
        return raw
    parts = [p.strip() for p in raw.split() if p.strip()]
    if not parts:
        raise ValueError("empty terms")
    return "(" + "|".join(re.escape(term) for term in parts) + ")"


def try_remove_terms_from_pattern(pattern: str, terms: Iterable[str]) -> str | None:
    pattern = pattern.strip()
    if not pattern:
        return None
    inner = pattern[1:-1] if pattern.startswith("(") and pattern.endswith(")") else pattern
    tokens = [t.strip() for t in re.split(r"(?<!\\)\|", inner) if t.strip()]
    cleaned = {t.strip() for t in terms if t.strip()}
    left = [token for token in tokens if token not in cleaned and html.unescape(token) not in cleaned]
    if not left:
        return None
    if len(left) == 1:
        return left[0]
    return "(" + "|".join(left) + ")"
