"""Message text. Everything user-supplied goes through h() (HTML escaping, B11)."""

from __future__ import annotations

import html


def h(value: object) -> str:
    return html.escape(str(value), quote=False)


def b(value: object) -> str:
    return f"<b>{h(value)}</b>"


def popup(text: str) -> str:
    """Callback popups are plain text (no markup, B26) and max 200 chars."""
    return text if len(text) <= 200 else text[:197] + "..."


def human_duration(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    hh, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = [f"{d}d"] if d else []
    if d or hh:
        parts.append(f"{hh}h")
    parts.append(f"{m}m")
    return " ".join(parts)


START = """{name}

Commands:
/ping - your user id (works for everyone)
/status - server, Broadlink devices, users
/list - rooms, devices and their buttons
/keyboard [room or device] - button remote
/on &lt;device&gt;, /off &lt;device&gt; - power
/d &lt;device&gt; &lt;action&gt; - e.g. /d bedroom_ac power on high
/user - approved users (remove)
/adduser &lt;user id&gt; &lt;name&gt; - approve a user"""

NOT_ALLOWED = "You are not approved to use this bot. Send /ping and give your user id to an approved user."
