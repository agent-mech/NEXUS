#!/usr/bin/env python3
"""
NEXUS -- terminal chat client (Textual TUI).

Quick start
-----------
pip install textual websockets

python3 nexus.py                              # connect to 127.0.0.1:8765
python3 nexus.py --host 1.2.3.4 --port 8765  # remote server

First time: choose "I have an invite", paste TEMP-... code, pick a username.
Save the passkey (NX-...) -- shown exactly once.
After that: the session is cached; next launch logs you in automatically.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── WebSocket ─────────────────────────────────────────────────────────────────
try:
    import websockets  # noqa: F401
    try:
        from websockets.asyncio.client import connect as ws_connect
    except ImportError:
        from websockets.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed, WebSocketException
except ImportError:
    sys.exit("Missing dependency: pip install websockets")

# ── Textual ───────────────────────────────────────────────────────────────────
try:
    from textual import events, on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.css.query import NoMatches
    from textual.screen import Screen, ModalScreen
    from textual.widgets import (
        Button, Input, Label,
        ListItem, ListView, RichLog, Static,
    )
    from rich.text import Text
    from rich.markup import escape as rich_escape
except ImportError:
    sys.exit("Missing dependency: pip install textual")

# ── Clipboard helper (cross-platform, no extra deps) ─────────────────────────

def _read_clipboard() -> str:
    """Return clipboard text, or '' on failure."""
    try:
        if sys.platform == "win32":
            import subprocess
            r = subprocess.run(
                ["powershell", "-NoProfile", "-command", "Get-Clipboard"],
                capture_output=True, text=True, timeout=3,
            )
            return r.stdout.strip()
        elif sys.platform == "darwin":
            import subprocess
            r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3)
            return r.stdout.strip()
        else:
            import subprocess
            for cmd in (
                ["wl-paste", "--no-newline"],
                ["xclip", "-selection", "clipboard", "-o"],
                ["xsel",  "--clipboard", "--output"],
            ):
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
                    if r.returncode == 0:
                        return r.stdout.strip()
                except FileNotFoundError:
                    continue
    except Exception:
        pass
    return ""


def _sanitize_key(raw: str) -> str:
    """Strip all whitespace and non-printable characters from a pasted key."""
    return "".join(ch for ch in raw if ch.isprintable() and not ch.isspace())


class ClipInput(Input):
    """Input that handles Ctrl+V via the OS clipboard (no bracketed-paste needed)."""

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "ctrl+v":
            text = _sanitize_key(_read_clipboard())
            if text:
                self.value = text
                self.cursor_position = len(text)
            event.prevent_default()
            event.stop()
            return
        await super()._on_key(event)

# ─────────────────────────────────────────────────────────────────────────────
# Version
# ─────────────────────────────────────────────────────────────────────────────

CLIENT_VERSION = "2.0.0"

# ─────────────────────────────────────────────────────────────────────────────
# Role constants
# ─────────────────────────────────────────────────────────────────────────────

ROLE_COLOR = {
    "owner":     "gold1",
    "admin":     "red1",
    "moderator": "cornflower_blue",
    "helper":    "green3",
    "user":      "grey82",
    "system":    "bright_cyan",
}
ROLE_MARK = {
    "owner":     "*",
    "admin":     "*",
    "moderator": "o",
    "helper":    ".",
}
ROLE_RANK = {
    "user": 0, "helper": 1, "moderator": 2,
    "admin": 3, "owner": 4, "system": 5,
}
ROLE_DESC = {
    "user":      "Standard member",
    "helper":    "Can mint user invites",
    "moderator": "Can warn, mute, kick and view reports",
    "admin":     "Can ban, promote and run invites",
    "owner":     "Full server control, Guardian access",
    "system":    "Internal server identity",
}
SEV_COLOR = {
    "critical": "red1",
    "warning":  "yellow1",
    "info":     "bright_cyan",
}

# ─────────────────────────────────────────────────────────────────────────────
# Server dialect
# ─────────────────────────────────────────────────────────────────────────────
#
# The server and this client disagree about the shape of a packet. The server
# labels the envelope "type" where this client reads "op", and names the parts
# of a message from / text / id / ts where this client reads username /
# content / message_id / timestamp.
#
# Nothing incoming matched, so every packet fell through the dispatcher and was
# discarded without a word - which is why the chat window stayed empty no
# matter how much was said in it. Sending worked the whole time, because the
# server reads "op" from clients; only the return direction was deaf.
#
# Translating once, here, leaves the fifty-odd handlers untouched. Every field
# is filled in only when absent, so a server that already speaks this client's
# dialect passes through unchanged.

#: Names the server uses for things this client calls something else.
_OP_ALIASES = {
    "system": "notice",     # a line from the server about the room
}


def _iso(value) -> str:
    """Server timestamps are epoch seconds; fmt_time wants an ISO string."""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    return value if isinstance(value, str) else ""


def normalise_packet(p: dict) -> dict:
    """Translate a server packet into the shape the dispatcher expects."""
    if not isinstance(p, dict):
        return p
    p = dict(p)

    if "op" not in p:
        kind = p.get("type", "")
        if kind == "list":
            # One packet covers several listings; "what" says which.
            p["op"] = p.get("what", "channels")
        elif kind == "history" and p.get("joined") and p.get("channel"):
            # The server has no "channel_joined" packet: the history it
            # returns on a join IS the acknowledgement, flagged with
            # "joined", and it is sent nowhere else. Without this the client
            # never learns which channel it is in, so self._channel stays
            # empty and every message it sends is addressed to no channel at
            # all. The flag matters: a reply to /history is the same packet
            # type and must NOT clear the pane or re-join anything.
            p["op"] = "channel_joined"
            p.setdefault("history", p.get("messages", []))
        else:
            p["op"] = _OP_ALIASES.get(kind, kind)

    # A "list" packet carries its payload under "items".
    if "items" in p:
        p.setdefault(p.get("op", "items"), p["items"])

    if "from"    in p: p.setdefault("username",   p["from"])
    if "from_id" in p: p.setdefault("user_id",    p["from_id"])
    if "id"      in p: p.setdefault("message_id", p["id"])
    if "ts"      in p: p.setdefault("timestamp",  _iso(p["ts"]))
    if "text"    in p:
        p.setdefault("content", p["text"])   # body of a chat message
        p.setdefault("message", p["text"])   # body of a notice

    # History and search results arrive with the messages nested inside
    # them, and each one needs the same field translation as a live message.
    # Without "results" here, /search rendered a row per hit with an empty
    # sender and an empty body.
    for key in ("messages", "history", "results"):
        if isinstance(p.get(key), list):
            p[key] = [normalise_packet(m) for m in p[key]]

    return p


# ─────────────────────────────────────────────────────────────────────────────
# Themes
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Outline style
# ─────────────────────────────────────────────────────────────────────────────
#
# Every border in NEXUS is drawn in this style. Change this one word and the
# whole application follows - panels, inputs, buttons and the rail divider.
#
#   round    ╭──────╮   │  ╰──────╯     rounded box drawing  (default)
#   solid    ┌──────┐   │  └──────┘     square box drawing
#   heavy    ┏━━━━━━┓   ┃  ┗━━━━━━┛     thick box drawing
#   double   ╔══════╗   ║  ╚══════╝     double rule
#   dashed   ┏╍╍╍╍╍╍┓   ╏  ┗╍╍╍╍╍╍┛     dashed box drawing
#   tall     ▊▔▔▔▔▔▔▎      ▊▁▁▁▁▁▁▎     half blocks, a heavy ribbon
#   hkey     ▔▔▔▔▔▔▔▔      ▁▁▁▁▁▁▁▁     a line above and below, no sides
#   ascii    +------+   |  +------+     plain ASCII, works in any font
#
# Everything but "ascii" needs a font with box-drawing glyphs. Almost every
# terminal font has them; if yours does not, it substitutes, and an edge
# arrives as a row of coloured rectangles rather than a line. That is the
# one case where "ascii" is worth having.
#: How many lines of each conversation to keep. Switching back to a
#: channel replays this many; beyond it the oldest lines are dropped so a
#: long session cannot grow without bound.
SCROLLBACK: int = 500

BORDER: str = "round"

#: The input box can differ - "hkey" gives it a line above and below and no
#: sides, which reads as a field rather than a container. Set to None to use
#: BORDER for the input too.
INPUT_BORDER: "str | None" = None


THEMES: dict[str, dict] = {
    "nexus":  {"a": "#00ff9d", "a2": "#00bfff", "border": "green"},
    "ocean":  {"a": "#00bfff", "a2": "#7c3aed", "border": "blue"},
    "ember":  {"a": "#ff6b35", "a2": "#f0b429", "border": "red"},
    "matrix": {"a": "#00ff00", "a2": "#39ff14", "border": "green"},
    "mono":   {"a": "#e2e8f0", "a2": "#94a3b8", "border": "white"},
}
_current_theme: dict = THEMES["nexus"]

def set_theme(name: str) -> bool:
    global _current_theme
    if name not in THEMES:
        return False
    _current_theme = THEMES[name]
    return True

def accent() -> str:
    return _current_theme["a"]

def accent2() -> str:
    return _current_theme["a2"]

# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────

def build_css() -> str:
    # Every outline is drawn "ascii": +-----+ with | down the sides, built
    # from three characters that exist in every font there has ever been.
    #
    # The previous "tall" and "round" borders used half-block and box-drawing
    # glyphs. A terminal whose font lacks them does not leave a gap - it
    # substitutes, and the edge of a box arrives as a ribbon of coloured
    # rectangles. Plain +-| cannot fail that way.
    a  = accent()
    a2 = accent2()
    b  = BORDER
    bi = INPUT_BORDER or BORDER
    return f"""
/* ═══════════════════════════════════════════════
   BASE
   ═══════════════════════════════════════════════ */
Screen {{ background: #0d1117; }}

/* ═══════════════════════════════════════════════
   MODALS -- centred overlays
   ═══════════════════════════════════════════════ */
LoginScreen, PasskeyScreen, InviteScreen, UsernameScreen,
PasskeyDisplayScreen, ConfirmScreen, GuardianScreen {{
    align: center middle;
    background: #0d111799;
}}

/* Login */
#login-box {{
    width: 52; height: auto;
    border: {b} {a};
    padding: 1 2;
    background: #161b22;
}}
#logo {{ text-align: center; color: {a}; padding-bottom: 1; }}
#login-subtitle {{
    text-align: center; color: #484f58;
    padding-bottom: 1;
}}
.login-btn {{
    width: 100%; margin-top: 1;
    background: #21262d;
    border: {b} #30363d;
    color: #c9d1d9;
}}
.login-btn:hover {{
    background: #30363d;
    border: {b} {a};
    color: {a};
}}

/* Passkey / invite / username dialogs */
#passkey-box, #invite-box, #username-box {{
    width: 56; height: auto;
    border: {b} {a};
    padding: 1 2;
    background: #161b22;
}}

/* Passkey display (save-your-key screen) */
#pk-display-box {{
    width: 62; height: auto;
    border: {b} #f0b429;
    padding: 1 2;
    background: #161b22;
}}
#pk-value {{
    text-align: center;
    color: {a};
    background: #0d1117;
    padding: 1 2;
    margin: 1 0;
}}

/* Confirm dialog */
#confirm-box {{
    width: 52; height: auto;
    border: {b} #ff4d4f;
    padding: 1 2;
    background: #161b22;
}}

/* ─── Shared modal elements ─── */
.modal-title {{
    text-align: center;
    color: #e6edf3;
    text-style: bold;
    padding-bottom: 1;
}}
.modal-label {{
    color: #484f58;
}}
.modal-input {{
    width: 100%;
    background: #0d1117;
    border: {b} #30363d;
    color: #e6edf3;
    margin-top: 1;
    margin-bottom: 1;
}}
.modal-input:focus {{ border: {b} {a}; }}
Input {{
    color: #e6edf3;
    background: #0d1117;
    border: {b} #30363d;
}}
Input:focus {{ border: {b} {a}; color: #e6edf3; }}
.modal-btn-primary {{
    width: 100%;
    background: #21262d;
    border: {b} {a};
    color: {a};
    margin-top: 1;
}}
.modal-btn-primary:hover {{ background: #2d333b; }}
.modal-btn-cancel {{
    width: 100%;
    background: #21262d;
    border: {b} #30363d;
    color: #484f58;
    margin-top: 1;
}}
.modal-btn-cancel:hover {{ background: #2d333b; color: #8b949e; }}
.error-label {{
    color: #f85149;
    text-align: center;
    height: 1;
}}

/* ═══════════════════════════════════════════════
   GUARDIAN overlay
   ═══════════════════════════════════════════════ */
#guardian-box {{
    width: 90; height: 38;
    border: {b} #f0b429;
    padding: 0 1;
    background: #161b22;
}}
#guardian-title {{
    text-align: center;
    color: #f0b429;
    height: 2;
    content-align: center middle;
}}
#guardian-log {{
    height: 1fr;
    border: {b} #21262d;
    background: #0d1117;
}}
#guardian-input-row {{ height: 3; margin-top: 1; }}
#guardian-input {{
    width: 1fr;
    background: #0d1117;
    border: {b} #30363d;
    color: #e6edf3;
}}
#btn-guardian-close {{
    width: 10;
    background: #21262d;
    border: {b} #30363d;
    color: #8b949e;
    margin-left: 1;
}}
#btn-guardian-close:hover {{
    border: {b} #f0b429;
    color: #f0b429;
}}

/* ═══════════════════════════════════════════════
   CHAT SCREEN -- topbar + body + bottom stack
   ═══════════════════════════════════════════════ */

Screen {{
    background: #0d1117;
}}

/* ── Top bar (1 line) ──────────────────────── */
#topbar {{
    dock: top;
    height: 1;
    padding: 0 1;
    background: #21262d;
    color: #8b949e;
}}

/* ── Body: sidebar + chat log ──────────────── */
#body {{
    height: 1fr;
}}

/* ── Left sidebar ──────────────────────────── */
#sidebar {{
    width: 26;
    background: #161b22;
    /* The divider between the rail and the chat. #21262d was within a
       shade of both backgrounds either side of it, so the line was being
       drawn and then disappearing. This grey reads clearly against both
       #161b22 and #0d1117 without competing with the messages, which an
       accent-coloured full-height line does. */
    border-right: {b} #484f58;
    overflow-y: auto;
}}

/* Section labels */
.rail-title {{
    height: 1;
    padding: 0 1;
    color: {a};
    text-style: bold;
    background: #21262d;
    /* A blank row above each heading so CHANNELS and DIRECT read as two
       lists rather than one run-on column. */
    margin-top: 1;
}}
.rail-title.first {{ margin-top: 0; }}

/* Channel / DM lists */
#channel-list, #dm-list {{
    height: auto;
    background: #161b22;
    border: none;
    padding: 0;
    margin: 0;
}}
#channel-list > ListItem, #dm-list > ListItem {{
    background: transparent;
    color: #8b949e;
    padding: 0 2;
    height: 1;
}}
#channel-list > ListItem.--highlight,
#dm-list > ListItem.--highlight {{
    background: #21262d;
    color: {a};
}}
#channel-list > ListItem:hover,
#dm-list > ListItem:hover {{
    background: #21262d;
    color: #c9d1d9;
}}

/* ── Chat log -- fills remaining space ─────── */
#chat-log {{
    padding: 0 2;
    background: #0d1117;
}}

/* ── Footer: one docked column, bars stacked inside ──────────
   Each bar used to dock: bottom on its own. Docked siblings do not stack
   that way - all three landed on the last row and the input box, laid out
   last, covered them. Nobody ever saw the status line, the command
   suggestions or the key hints. One container fixes all three. */
#footer {{
    dock: bottom;
    height: auto;
    background: #0d1117;
}}

/* ── Bottom status strip ───────────────────── */
#status {{
    height: 1;
    padding: 0 1;
    color: #8b949e;
    background: #0d1117;
}}

/* ── Command suggestions ───────────────────── */
#suggestions {{
    height: 1;
    padding: 0 1;
    color: {a};
    background: #21262d;
}}

/* ── Key hints ─────────────────────────────── */
#keys {{
    height: 1;
    padding: 0 1;
    /* Was #30363d on a #21262d bar - dark grey on dark grey, invisible. */
    color: #8b949e;
    background: #21262d;
}}

/* ── Input box ─────────────────────────────── */
#chat-input {{
    height: 3;
    border: {bi} #30363d;
    background: #161b22;
    color: #e6edf3;
}}
#chat-input:focus {{ border: {bi} {a}; }}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Token helpers
# ─────────────────────────────────────────────────────────────────────────────

TOKEN_FILE = Path(os.environ.get("NEXUS_TOKEN", "nexus_session.json"))

#: Certificate fingerprints this client has seen before, per server.
#:
#: A self-signed certificate cannot be checked against a certificate
#: authority - there is nothing to check it against. What CAN be checked is
#: that it is the SAME certificate as last time, which is how SSH has always
#: handled this. The first connection is taken on trust and remembered;
#: every connection after that is verified against it, so somebody
#: inserting themselves between you and the server afterwards is caught.
KNOWN_HOSTS = Path(os.environ.get("NEXUS_KNOWN_HOSTS", "nexus_known_hosts.json"))


def _known_host(where: str) -> "str | None":
    try:
        return json.loads(KNOWN_HOSTS.read_text()).get(where)
    except Exception:
        return None


def _remember_host(where: str, fingerprint: str):
    try:
        try:
            data = json.loads(KNOWN_HOSTS.read_text())
        except Exception:
            data = {}
        data[where] = fingerprint
        KNOWN_HOSTS.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


class CertificateChanged(Exception):
    """The server presented a different certificate than last time."""

def load_token(host: str, port: int) -> "str | None":
    try:
        return json.loads(TOKEN_FILE.read_text()).get(f"{host}:{port}")
    except Exception:
        return None

def save_token(host: str, port: int, token: str):
    try:
        try: data = json.loads(TOKEN_FILE.read_text())
        except Exception: data = {}
        data[f"{host}:{port}"] = token
        TOKEN_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

def clear_token(host: str, port: int):
    try:
        data = json.loads(TOKEN_FILE.read_text())
        data.pop(f"{host}:{port}", None)
        TOKEN_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# Session (WebSocket wrapper)
# ─────────────────────────────────────────────────────────────────────────────

class Session:
    def __init__(self, host: str, port: int, tls: "bool | None" = None,
                 insecure_tls: bool = False, cafile: "str | None" = None):
        self.host      = host
        self.port      = port
        #: True forces wss://, False forces ws://, None (the default) tries
        #: both and uses whichever the server actually speaks.
        self.tls       = tls
        #: A certificate authority to trust, beyond the system's own.
        #: macOS keeps its trusted roots in the Keychain, which Python does
        #: not read - so importing the root there fixes Safari and Chrome
        #: and leaves this client still unable to verify. Pointing straight
        #: at the file is the reliable answer on every platform.
        self.cafile    = cafile
        #: How this connection ended up protected, for the status bar.
        self.security  = ""
        self.url       = ""
        self.fingerprint = ""
        #: Accept a certificate nothing vouches for. Needed for the
        #: certificate a server presents when nothing public vouches for
        #: it, and for nothing else: it turns off the check that the
        #: server is the one it claims to be. Prefer --cafile, which keeps
        #: that check and simply trusts the right authority.
        self.insecure_tls = insecure_tls
        self.ws        = None
        self.username  = ""
        self.public_id = ""
        self.role      = "user"
        self.token     = ""
        self.channels: list[str] = []
        self.dm_convos: list[str] = []  # usernames with open DMs

    async def connect(self):
        """Connect, working out for itself whether the server speaks TLS.

        A plain ws:// client against a TLS server fails with "did not
        receive a valid HTTP response", which tells somebody nothing about
        what to change. Rather than make the flags compulsory, try the
        encrypted form first and fall back.

        Encrypted-but-unverified is tried before plaintext on purpose: it is
        strictly better than the alternative here, which is sending a
        passkey in the clear. Which one was used is recorded in
        ``self.security`` and shown, so an unverified connection is never
        silently passed off as a safe one.
        """
        import ssl

        verified = ssl.create_default_context()
        if self.cafile:
            try:
                verified.load_verify_locations(cafile=self.cafile)
            except Exception as exc:        # noqa: BLE001
                raise OSError(
                    f"Could not read the certificate authority file "
                    f"{self.cafile!r}: {exc}"
                ) from None
        unverified = ssl.create_default_context()
        unverified.check_hostname = False
        unverified.verify_mode = ssl.CERT_NONE

        wss = f"wss://{self.host}:{self.port}"
        ws  = f"ws://{self.host}:{self.port}"

        if self.tls is True:
            attempts = ([(wss, unverified, "pin")] if self.insecure_tls
                        else [(wss, verified, "verified")])
        elif self.tls is False:
            # Only ever on request. Plaintext is not a fallback.
            attempts = [(ws, None, "plain")]
        else:
            attempts = [(wss, verified, "verified"), (wss, unverified, "pin")]

        last = None
        for url, ctx, mode in attempts:
            try:
                sock = (await ws_connect(url, ssl=ctx) if ctx
                        else await ws_connect(url))
            except CertificateChanged:
                raise
            except Exception as exc:          # noqa: BLE001
                last = exc
                continue

            if mode == "pin":
                try:
                    await self._check_pin(sock)
                except CertificateChanged:
                    try:
                        await sock.close()
                    except Exception:
                        pass
                    raise
            else:
                self.security = ("encrypted and verified" if mode == "verified"
                                 else "NOT encrypted")
            self.ws = sock
            self.url = url
            return

        if self.tls is None:
            # Every encrypted attempt failed. The overwhelmingly common
            # cause is a server running without --cert, and the raw socket
            # error ("connection reset") says nothing about that.
            raise OSError(
                f"Could not open an encrypted connection to "
                f"{self.host}:{self.port}.\n\n"
                f"The server is probably running without TLS. Either start "
                f"it with --cert and --key, or connect with --no-tls to "
                f"accept an unencrypted connection.\n\n"
                f"(underlying error: {last})"
            )
        raise last if last else OSError("Could not connect")

    async def _check_pin(self, sock):
        """Trust this certificate on first sight; insist on it afterwards."""
        import hashlib
        where = f"{self.host}:{self.port}"
        der = None
        try:
            ssl_object = sock.transport.get_extra_info("ssl_object")
            der = ssl_object.getpeercert(binary_form=True)
        except Exception:
            pass
        if not der:
            self.security = "encrypted (certificate not verified)"
            return

        fingerprint = hashlib.sha256(der).hexdigest()
        self.fingerprint = fingerprint
        known = _known_host(where)
        if known is None:
            _remember_host(where, fingerprint)
            self.security = "encrypted (new server, remembered)"
        elif known == fingerprint:
            self.security = "encrypted (pinned)"
        else:
            pretty = ":".join(fingerprint[i:i + 2]
                              for i in range(0, len(fingerprint), 2)).upper()
            raise CertificateChanged(
                f"The certificate for {where} has CHANGED since you last "
                f"connected.\n\nThis happens if the server was reinstalled "
                f"or its certificate regenerated - but it is also exactly "
                f"what somebody intercepting the connection would look "
                f"like.\n\nNow: {pretty}\n\nIf you changed it yourself, "
                f"delete {KNOWN_HOSTS} and connect again. If you did not, "
                f"do not connect."
            )

    async def send(self, **kwargs):
        # The other half of the dialect gap. This client names a message
        # body "content"; the server reads "text" and drops anything whose
        # text is empty - without a word back, so a message typed into the
        # box went nowhere and nothing said so. Both names are sent, which
        # satisfies either server without having to guess which one is on
        # the far end.
        if "content" in kwargs and "text" not in kwargs:
            kwargs["text"] = kwargs["content"]
        elif "text" in kwargs and "content" not in kwargs:
            kwargs["content"] = kwargs["text"]

        # This client asks for a listing by name; the server takes one
        # "list" request and a "what". Only these two have a server-side
        # equivalent - the rest of this client's vocabulary is simply not
        # implemented by this server, and says so plainly when asked.
        if kwargs.get("op") in ("users", "channels"):
            kwargs = {"op": "list", "what": kwargs["op"]}

        await self.ws.send(json.dumps(kwargs))

    async def recv(self) -> dict:
        return json.loads(await self.ws.recv())

    async def close(self):
        if self.ws:
            try: await self.ws.close()
            except Exception: pass
            self.ws = None

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def fmt_time(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M")
    except Exception:
        return datetime.now().strftime("%H:%M")

def ts() -> str:
    return datetime.now().strftime("%H:%M")

def e(s: str) -> str:
    """Rich-escape a string."""
    return rich_escape(str(s))

def parse_duration(s: str) -> str:
    """Return duration string or empty if not a duration."""
    return s if re.match(r"^\d+[smhd]$", s or "") else ""

# ─────────────────────────────────────────────────────────────────────────────
# Modal screens
# ─────────────────────────────────────────────────────────────────────────────

class LoginScreen(ModalScreen):
    BINDINGS = []

    def compose(self) -> ComposeResult:
        with Vertical(id="login-box"):
            yield Static(
                "███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗\n"
                "████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝\n"
                "██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗\n"
                "██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║\n"
                "██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║\n"
                "╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝",
                id="logo",
            )
            yield Static("Invite-only . Encrypted . Private", id="login-subtitle")
            yield Button("I have a passkey  (NX-...)",   id="btn-passkey", classes="login-btn")
            yield Button("I have an invite  (TEMP-...)", id="btn-invite",  classes="login-btn")

    @on(Button.Pressed, "#btn-passkey")
    def go_passkey(self): self.dismiss("passkey")

    @on(Button.Pressed, "#btn-invite")
    def go_invite(self): self.dismiss("invite")


class PasskeyScreen(ModalScreen):
    def compose(self) -> ComposeResult:
        with Vertical(id="passkey-box"):
            yield Static("Sign in with passkey", classes="modal-title")
            yield Static("Ctrl+V to paste  .  Enter to submit", classes="modal-label")
            yield ClipInput(placeholder="NX-...", id="pk-input", classes="modal-input")
            yield Static("", id="pk-preview", classes="modal-label")
            yield Static("", id="pk-error",   classes="error-label")
            yield Button("Sign in", id="btn-pk-ok",     classes="modal-btn-primary")
            yield Button("Cancel",  id="btn-pk-cancel", classes="modal-btn-cancel")

    def on_mount(self):
        self.query_one("#pk-input", ClipInput).focus()

    def on_paste(self, event: events.Paste) -> None:
        inp = self.query_one("#pk-input", ClipInput)
        inp.value = _sanitize_key(event.text)
        inp.cursor_position = len(inp.value)
        self._show_preview(inp.value)
        event.stop()

    @on(Input.Changed, "#pk-input")
    def on_changed(self, event: Input.Changed) -> None:
        self._show_preview(event.value)

    def _show_preview(self, val: str) -> None:
        n = len(val)
        if n == 0:
            preview = ""
        elif n <= 12:
            preview = f"{n} chars: {val}"
        else:
            preview = f"{n} chars: {val[:6]}...{val[-4:]}"
        self.query_one("#pk-preview", Static).update(preview)

    @on(Input.Submitted, "#pk-input")
    @on(Button.Pressed,  "#btn-pk-ok")
    def submit(self):
        val = _sanitize_key(self.query_one("#pk-input", ClipInput).value)
        if not val:
            self.query_one("#pk-error", Static).update("Passkey cannot be empty.")
            return
        self.dismiss(val)

    @on(Button.Pressed, "#btn-pk-cancel")
    def cancel(self): self.dismiss(None)


class InviteScreen(ModalScreen):
    def compose(self) -> ComposeResult:
        with Vertical(id="invite-box"):
            yield Static("Enter invite code", classes="modal-title")
            yield Static("Invite code  (TEMP-...)  --  Ctrl+V to paste", classes="modal-label")
            yield ClipInput(placeholder="TEMP-...", id="inv-input", classes="modal-input")
            yield Static("", id="inv-error", classes="error-label")
            yield Button("Continue", id="btn-inv-ok",     classes="modal-btn-primary")
            yield Button("Cancel",   id="btn-inv-cancel", classes="modal-btn-cancel")

    def on_mount(self):
        self.query_one("#inv-input", ClipInput).focus()

    @on(Input.Submitted, "#inv-input")
    @on(Button.Pressed,  "#btn-inv-ok")
    def submit(self):
        val = self.query_one("#inv-input", ClipInput).value.strip()
        if not val:
            self.query_one("#inv-error", Static).update("Invite code cannot be empty.")
            return
        self.dismiss(val)

    @on(Button.Pressed, "#btn-inv-cancel")
    def cancel(self): self.dismiss(None)


class UsernameScreen(ModalScreen):
    def compose(self) -> ComposeResult:
        with Vertical(id="username-box"):
            yield Static("Choose a username", classes="modal-title")
            yield Static("Username", classes="modal-label")
            yield Input(placeholder="nova", id="un-input", classes="modal-input")
            yield Static("", id="un-error", classes="error-label")
            yield Button("Create account", id="btn-un-ok",     classes="modal-btn-primary")
            yield Button("Cancel",         id="btn-un-cancel", classes="modal-btn-cancel")

    @on(Input.Submitted, "#un-input")
    @on(Button.Pressed,  "#btn-un-ok")
    def submit(self):
        val = self.query_one("#un-input", Input).value.strip()
        if not val:
            self.query_one("#un-error", Static).update("Username cannot be empty.")
            return
        self.dismiss(val)

    @on(Button.Pressed, "#btn-un-cancel")
    def cancel(self): self.dismiss(None)


class PasskeyDisplayScreen(ModalScreen):
    BINDINGS = []

    def __init__(self, passkey: str):
        super().__init__()
        self._passkey = passkey

    def compose(self) -> ComposeResult:
        with Vertical(id="pk-display-box"):
            yield Static("!  Save your passkey now  !", classes="modal-title")
            yield Static(
                "This is the ONLY time it will be shown.\n"
                "Lose it and you lose access to your account.",
                classes="modal-label",
            )
            yield Static(self._passkey, id="pk-value")
            yield Button("I have saved my passkey →  Enter NEXUS",
                         id="btn-pk-saved", classes="modal-btn-primary")

    def on_key(self, event):
        if event.key == "escape": event.stop()

    @on(Button.Pressed, "#btn-pk-saved")
    def proceed(self): self.dismiss(True)


class ConfirmScreen(ModalScreen):
    """Generic YES/NO confirmation. Returns True on YES."""

    def __init__(self, prompt: str, danger: bool = False):
        super().__init__()
        self._prompt = prompt
        self._danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self._prompt, classes="modal-title")
            yield Static("Type  YES  to confirm.", classes="modal-label")
            yield Input(placeholder="YES", id="confirm-input", classes="modal-input")
            yield Static("", id="confirm-error", classes="error-label")
            yield Button("Confirm", id="btn-conf-ok",     classes="modal-btn-primary")
            yield Button("Cancel",  id="btn-conf-cancel", classes="modal-btn-cancel")

    @on(Input.Submitted, "#confirm-input")
    @on(Button.Pressed,  "#btn-conf-ok")
    def submit(self):
        if self.query_one("#confirm-input", Input).value.strip() != "YES":
            self.query_one("#confirm-error", Static).update("Type exactly  YES  to confirm.")
            return
        self.dismiss(True)

    @on(Button.Pressed, "#btn-conf-cancel")
    def cancel(self): self.dismiss(False)


class GuardianScreen(ModalScreen):
    BINDINGS = [Binding("escape", "close_guardian", "Close")]

    def __init__(self, session: "Session"):
        super().__init__()
        self._session = session
        self._refresh_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="guardian-box"):
            yield Static("*  NEXUS GUARDIAN  *", id="guardian-title")
            yield RichLog(id="guardian-log", highlight=True, markup=True, wrap=True)
            with Horizontal(id="guardian-input-row"):
                yield Input(
                    placeholder="status / events / ack <id> / sessions / lockdown on|off / broadcast <msg> / backup / shutdown",
                    id="guardian-input",
                )
                yield Button("Close", id="btn-guardian-close")

    async def on_mount(self) -> None:
        self._refresh_task = asyncio.ensure_future(self._auto_refresh())
        await self._session.send(op="guardian_stats")

    def _log(self, text: "Text | str"):
        self.query_one("#guardian-log", RichLog).write(text)

    async def _auto_refresh(self):
        while True:
            await asyncio.sleep(5)
            try: await self._session.send(op="guardian_stats")
            except Exception: break

    def handle_packet(self, pkt: dict):
        op = pkt.get("op", "")
        if op == "guardian_stats":
            s = pkt.get("stats", {})
            self._log(Text.assemble(
                ("── Guardian Stats ──────────────────────────\n", "gold1"),
                (f"  Users online:    {s.get('online', '?')}\n",        "grey82"),
                (f"  Total users:     {s.get('total_users', '?')}\n",   "grey82"),
                (f"  Messages today:  {s.get('messages_today', '?')}\n","grey82"),
                (f"  Alerts (24h):    {s.get('alerts_24h', '?')}\n",    "grey82"),
                (f"  Lockdown:        {'ON' if s.get('lockdown') else 'off'}\n",
                 "red1" if s.get("lockdown") else "green3"),
            ))
        elif op == "guardian_events":
            events = pkt.get("events", [])
            if not events:
                self._log(Text("  (no recent events)", style="dim"))
                return
            for ev in events:
                sev = ev.get("severity", "info")
                col = SEV_COLOR.get(sev, "grey82")
                t   = fmt_time(ev.get("timestamp", ""))
                self._log(Text.assemble(
                    (f"[{t}] ", "dim"),
                    (f"[{sev.upper()}] ", col),
                    (ev.get("message", ""), "grey82"),
                    (f"  id={ev.get('event_id','')}", "dim"),
                ))
        elif op == "guardian_sessions":
            self._log(Text("── Active sessions ─", style="gold1"))
            for s in pkt.get("sessions", []):
                self._log(Text.assemble(
                    (f"  {s.get('username','?')} ", "grey82"),
                    (f"({s.get('role','?')}) ", ROLE_COLOR.get(s.get("role","user"), "grey82")),
                    (f"from {s.get('ip','?')}", "dim"),
                ))
        elif op in ("guardian_ack", "broadcast_sent"):
            self._log(Text(f"  [ok] {pkt.get('message', 'done')}", style="green3"))
        elif op == "lockdown_update":
            state = pkt.get("state", False)
            self._log(Text(
                f"  [ok] Lockdown {'ENABLED' if state else 'DISABLED'}",
                style="red1" if state else "green3",
            ))
        elif op == "backup_done":
            self._log(Text(f"  [ok] Backup: {pkt.get('path','done')}", style="green3"))
        elif op == "shutdown_pending":
            self._log(Text(f"  ! Shutdown: {pkt.get('reason','')}", style="yellow1"))
        elif op == "error":
            self._log(Text(f"  [!] {pkt.get('message','error')}", style="red1"))

    @on(Input.Submitted, "#guardian-input")
    async def run_command(self):
        inp  = self.query_one("#guardian-input", Input)
        raw  = inp.value.strip(); inp.value = ""
        if not raw: return
        parts = raw.split(maxsplit=1)
        cmd   = parts[0].lower()
        arg   = parts[1] if len(parts) > 1 else ""
        try:
            if   cmd == "status":    await self._session.send(op="guardian_stats")
            elif cmd == "events":    await self._session.send(op="guardian_events")
            elif cmd == "sessions":  await self._session.send(op="guardian_sessions")
            elif cmd == "watch":     await self._session.send(op="guardian_watch")
            elif cmd == "audit":
                n = int(arg) if arg.isdigit() else 50
                await self._session.send(op="audit", count=n)
            elif cmd == "ack":
                if not arg: self._log(Text("  Usage: ack <event_id>", style="yellow1")); return
                await self._session.send(op="guardian_ack", event_id=arg)
            elif cmd == "lockdown":
                if arg.lower() in ("on","1","true"):
                    await self._session.send(op="guardian_lockdown", state=True)
                elif arg.lower() in ("off","0","false"):
                    await self._session.send(op="guardian_lockdown", state=False)
                else:
                    self._log(Text("  Usage: lockdown on|off", style="yellow1"))
            elif cmd == "broadcast":
                if not arg: self._log(Text("  Usage: broadcast <message>", style="yellow1")); return
                await self._session.send(op="guardian_broadcast", message=arg)
            elif cmd == "backup":
                await self._session.send(op="backup")
            elif cmd == "shutdown":
                await self._session.send(op="shutdown", reason=arg or "Manual shutdown")
            elif cmd == "promote":
                p2 = arg.split()
                if len(p2) < 2: self._log(Text("  Usage: promote <user> <role>", style="yellow1")); return
                await self._session.send(op="promote", target=p2[0], role=p2[1])
            elif cmd == "notice":
                p2 = arg.split(maxsplit=1)
                if len(p2) < 2: self._log(Text("  Usage: notice <user> <text>", style="yellow1")); return
                await self._session.send(op="notice", target=p2[0], text=p2[1])
            else:
                self._log(Text.assemble(
                    ("  Commands: ", "dim"),
                    ("status  events  ack <id>  sessions  watch  audit [n]  "
                     "lockdown on|off  broadcast <msg>  backup  "
                     "shutdown [reason]  promote <user> <role>  notice <user> <text>",
                     "grey82"),
                ))
        except Exception as exc:
            self._log(Text(f"  Error: {exc}", style="red1"))

    @on(Button.Pressed, "#btn-guardian-close")
    def action_close_guardian(self):
        if self._refresh_task: self._refresh_task.cancel()
        self.dismiss(None)

# ─────────────────────────────────────────────────────────────────────────────
# Main chat screen
# ─────────────────────────────────────────────────────────────────────────────

class ChatScreen(Screen):
    BINDINGS = [Binding("ctrl+q", "quit_app", "Quit")]

    def __init__(self, session: "Session", host: str, port: int):
        super().__init__()
        self._session   = session
        self._host      = host
        self._port      = port
        self._channel   = ""
        self._dm_mode   = False
        self._dm_target = ""
        self._recv_task: asyncio.Task | None = None
        self._guardian: GuardianScreen | None = None
        # One transcript per conversation, keyed "#channel" or "@user".
        # Everything used to land in a single stream, so #general and
        # #europe were the same pane of text with no way to tell which
        # line belonged where. Keeping them apart is what lets the pane be
        # cleared on a switch without losing anything.
        self._buffers: dict[str, list[str]] = {}

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("CHANNELS", classes="rail-title first")
                yield ListView(id="channel-list")
                yield Static("DIRECT", classes="rail-title")
                yield ListView(id="dm-list")
            yield RichLog(
                id="chat-log", highlight=False, markup=True,
                wrap=True, auto_scroll=True,
            )
        with Vertical(id="footer"):
            yield Static("", id="status")
            yield Static("", id="suggestions")
            yield Static(
                "ctrl+q quit   /help commands   /join #channel   /dm @user",
                id="keys",
            )
            yield Input(
                placeholder="Message  .  /help for commands",
                id="chat-input",
            )

    async def on_mount(self) -> None:
        self._refresh_topbar()
        self._rebuild_channel_list()
        if self._session.channels:
            await self._join(self._session.channels[0])
        #: Set when the server signs this connection out because the
        #: account was opened elsewhere, so the close is not misreported.
        self._displaced = False
        self._recv_task = asyncio.ensure_future(self._receive_loop())
        # Nothing claimed focus, so it fell to the first focusable widget in
        # the tree - the channel list. Typing drove the list instead of the
        # message box and Enter picked a channel, so a typed message went
        # nowhere at all. The composer is where a chat window should start.
        self.query_one("#chat-input", Input).focus()

    # ── Receive loop ──────────────────────────────────────────────────────────

    async def _receive_loop(self):
        try:
            while True:
                self._dispatch(await self._session.recv())
        except (ConnectionClosed, WebSocketException, OSError):
            if not getattr(self, "_displaced", False):
                self._sys("Connection lost.")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._err(f"Receive error: {exc}")

    # ── Packet dispatcher ─────────────────────────────────────────────────────

    def _dispatch(self, p: dict):
        p  = normalise_packet(p)
        op = p.get("op", "")

        # ── Chat messages ──────────────────────────────────────────────────
        if op == "msg":
            if not self._dm_mode: self._render_msg(p)
        elif op == "dm":
            self._render_dm(p)
        elif op == "msg_edited":
            self._info(f"Message [{p.get('msg_id','')}] edited.")
        elif op == "msg_deleted":
            self._info(f"Message [{p.get('msg_id','')}] deleted.")
        elif op == "announcement":
            self._markup(
                f"[yellow1 bold]>>  ANNOUNCEMENT[/yellow1 bold]  "
                f"[yellow1]{e(p.get('text',''))}[/yellow1]"
            )
        elif op == "notice":
            self._markup(f"[bright_cyan]--  {e(p.get('message',''))}[/bright_cyan]")

        # ── Channel / group events ─────────────────────────────────────────
        elif op == "channel_joined":
            self._on_channel_joined(p)
        elif op == "channel_left":
            ch = p.get("channel", "")
            self._sys(f"Left #{ch}")
            if ch in self._session.channels:
                self._session.channels.remove(ch)
            self._rebuild_channel_list()
        elif op == "channels":
            self._on_channels(p)
        elif op == "channel_created":
            ch = p.get("name", "")
            self._info(f"Channel #{ch} created.")
            if ch not in self._session.channels:
                self._session.channels.append(ch)
            self._rebuild_channel_list()
        elif op == "groups":
            groups = p.get("groups", [])
            if not groups:
                self._info("No groups found.")
            else:
                self._markup("[bright_cyan]Groups:[/bright_cyan]")
                for g in groups:
                    self._markup(f"  [grey82]{e(g.get('name','?'))}[/grey82]  [dim]{e(g.get('topic',''))}[/dim]")
        elif op == "group_joined":
            self._sys(f"Joined group: {p.get('group','?')}")

        # ── Users / people ─────────────────────────────────────────────────
        elif op == "users":
            self._on_users(p)
        elif op == "user_found":
            u = p.get("user", {})
            rc  = ROLE_COLOR.get(u.get("role","user"), "grey82")
            self._markup(
                f"[bright_cyan]Found:[/bright_cyan]  "
                f"[{rc}]{e(u.get('username','?'))}[/{rc}]  "
                f"[dim]{u.get('public_id','')}[/dim]  "
                f"[{rc}]{u.get('role','?')}[/{rc}]"
            )
        elif op == "dms_list":
            convos = p.get("convos", [])
            self._session.dm_convos = [c.get("username","") for c in convos]
            self._rebuild_dm_list()
            if not convos:
                self._info("No DM conversations yet.")
            else:
                for c in convos:
                    self._markup(f"  [bright_cyan]@{e(c.get('username','?'))}[/bright_cyan]  [dim]{e(c.get('last',''))}[/dim]")
        elif op == "friends_list":
            friends = p.get("friends", [])
            requests_in  = p.get("requests_in",  [])
            requests_out = p.get("requests_out", [])
            blocked = p.get("blocked", [])
            self._markup("[bright_cyan]── Friends ─────────────────────[/bright_cyan]")
            for f in friends:
                rc = ROLE_COLOR.get(f.get("role","user"), "grey82")
                self._markup(f"  [{rc}]{'* ' if f.get('favourite') else ''}{e(f.get('username','?'))}[/{rc}]")
            if requests_in:
                self._markup("[yellow1]  Incoming requests:[/yellow1]")
                for r in requests_in:
                    self._markup(f"    [grey82]{e(r)}[/grey82]  (/accept {e(r)} or /decline {e(r)})")
            if requests_out:
                self._markup("[dim]  Sent requests: " + "  ".join(e(r) for r in requests_out) + "[/dim]")
            if blocked:
                self._markup("[dim]  Blocked: " + "  ".join(e(b) for b in blocked) + "[/dim]")
            if not any([friends, requests_in, requests_out, blocked]):
                self._info("No friends yet. Use /add @user to send a request.")
        elif op == "friend_request":
            self._markup(f"[yellow1]->  Friend request from [bold]{e(p.get('from','?'))}[/bold] -- /accept {e(p.get('from','?'))} or /decline {e(p.get('from','?'))}[/yellow1]")
        elif op == "friend_accepted":
            self._info(f"{e(p.get('user','?'))} accepted your friend request.")
        elif op == "friend_declined":
            self._info(f"{e(p.get('user','?'))} declined your friend request.")
        elif op == "blocked_list":
            bl = p.get("blocked", [])
            if not bl: self._info("No blocked users.")
            else: self._markup("[bright_cyan]Blocked:[/bright_cyan] " + "  ".join(e(b) for b in bl))
        elif op == "contact_code":
            self._markup(
                f"[bright_cyan]Your contact code:[/bright_cyan]\n"
                f"  [bold]{e(p.get('code','?'))}[/bold]\n"
                f"  [dim]Share this out-of-band for verified contact.[/dim]"
            )
        elif op == "verify_result":
            matched = p.get("match", False)
            num     = p.get("safety_number", "?")
            self._markup(
                f"[{'green3' if matched else 'red1'}]"
                f"{'[ok] Verified' if matched else '[!] Mismatch'}:[/{'green3' if matched else 'red1'}]  "
                f"[dim]{e(num)}[/dim]"
            )
        elif op == "devices_list":
            devs = p.get("devices", [])
            self._markup("[bright_cyan]── Your Devices ─────────────────[/bright_cyan]")
            for d in devs:
                self._markup(
                    f"  [grey82]{e(d.get('id','?')[:8])}[/grey82]  "
                    f"[dim]{e(d.get('name','?'))}[/dim]  "
                    f"{'[green3]current[/green3]' if d.get('current') else ''}"
                )
        elif op == "trust_list":
            entries = p.get("entries", [])
            self._markup("[bright_cyan]── Trusted Keys ─────────────────[/bright_cyan]")
            for t in entries:
                self._markup(f"  [grey82]{e(t.get('user','?'))}[/grey82]  [dim]{e(t.get('fingerprint','?')[:16])}...[/dim]")
        elif op == "vault_status":
            locked = p.get("locked", True)
            sz     = p.get("size_bytes", 0)
            self._markup(
                f"[bright_cyan]Vault:[/bright_cyan]  "
                f"{'[red1]Locked[/red1]' if locked else '[green3]Unlocked[/green3]'}  "
                f"[dim]{sz} bytes[/dim]"
            )
        elif op == "pin_set":
            self._info("PIN updated.")
        elif op == "listed_set":
            self._info(f"Directory listing: {'on' if p.get('state') else 'off'}.")
        elif op == "settings_info":
            self._markup("[bright_cyan]── Settings ─────────────────────[/bright_cyan]")
            for k, v in p.get("settings", {}).items():
                self._markup(f"  [dim]{e(k)}[/dim]  [grey82]{e(str(v))}[/grey82]")

        # ── Invites / keys ─────────────────────────────────────────────────
        elif op == "invite_created":
            self._info(f"Invite code: [bold]{e(p.get('code','?'))}[/bold]  role={p.get('role','user')}")
        elif op == "invite_list":
            invites = p.get("invites", [])
            if not invites: self._info("No active invites.")
            else:
                self._markup("[bright_cyan]── Active Invites ────────────────[/bright_cyan]")
                for inv in invites:
                    self._markup(
                        f"  [grey82]{e(inv.get('code','?'))}[/grey82]  "
                        f"[dim]{e(inv.get('role','?'))}  {e(inv.get('created','?'))}[/dim]"
                    )
        elif op == "invite_revoked":
            self._info(f"Invite revoked.")
        elif op == "rolekeys_list":
            keys = p.get("keys", [])
            if not keys: self._info("No role-upgrade keys.")
            else:
                self._markup("[bright_cyan]── Role-Upgrade Keys ─────────────[/bright_cyan]")
                for k in keys:
                    self._markup(f"  [grey82]{e(k.get('key','?'))}[/grey82]  [dim]{e(k.get('role','?'))}  {e(k.get('note',''))}[/dim]")
        elif op == "rolekey_created":
            self._info(f"Role-upgrade key: [bold]{e(p.get('key','?'))}[/bold]  → {p.get('role','?')}")
        elif op == "roleup_ok":
            new = p.get("new_role", "")
            self._session.role = new
            self._info(f"Role upgraded → [bold]{e(new)}[/bold]")
            self._update_me_label()

        # ── Moderation ─────────────────────────────────────────────────────
        elif op == "warn_issued":
            self._markup(f"[yellow1]!  Warned [bold]{e(p.get('target','?'))}[/bold]: {e(p.get('reason',''))}[/yellow1]")
        elif op == "muted":
            self._markup(f"[yellow1][mute]  Muted [bold]{e(p.get('target','?'))}[/bold] for {e(p.get('duration','?'))}: {e(p.get('reason',''))}[/yellow1]")
        elif op == "unmuted":
            self._info(f"{e(p.get('target','?'))} unmuted.")
        elif op == "kicked":
            self._markup(f"[yellow1][kick]  Kicked [bold]{e(p.get('target','?'))}[/bold]: {e(p.get('reason',''))}[/yellow1]")
        elif op == "banned":
            self._markup(f"[red1][ban]  Banned [bold]{e(p.get('target','?'))}[/bold] for {e(p.get('duration','?'))}: {e(p.get('reason',''))}[/red1]")
        elif op == "unbanned":
            self._info(f"{e(p.get('target','?'))} unbanned.")
        elif op == "you_were_muted":
            self._markup(f"[red1][mute]  You have been muted for {e(p.get('duration','?'))}: {e(p.get('reason',''))}[/red1]")
        elif op == "you_were_kicked":
            self._markup(f"[red1][kick]  You were kicked: {e(p.get('reason',''))}[/red1]")
        elif op == "you_were_banned":
            self._markup(f"[red1][ban]  You have been banned for {e(p.get('duration','?'))}: {e(p.get('reason',''))}[/red1]")
        elif op == "promoted":
            target = p.get("target","?"); new = p.get("new_role","?")
            if target == self._session.username:
                self._session.role = new; self._update_me_label()
                self._info(f"Your role was changed to [bold]{e(new)}[/bold].")
            else:
                self._info(f"{e(target)} promoted to {e(new)}.")
        elif op == "reports_list":
            reports = p.get("reports", [])
            if not reports: self._info("No reports.")
            else:
                self._markup("[bright_cyan]── Reports ──────────────────────[/bright_cyan]")
                for r in reports:
                    status_col = {"open":"yellow1","closed":"dim","actioned":"green3"}.get(r.get("status","open"),"grey82")
                    self._markup(
                        f"  [{status_col}]{e(r.get('id','?')[:8])}[/{status_col}]  "
                        f"[grey82]{e(r.get('target','?'))}[/grey82]  "
                        f"[dim]{e(r.get('reason','?'))}[/dim]  "
                        f"[{status_col}]{e(r.get('status','?'))}[/{status_col}]"
                    )
        elif op == "report_info":
            r = p.get("report", {})
            self._markup(
                f"[bright_cyan]── Report {e(r.get('id','?')[:8])} ──────────────[/bright_cyan]\n"
                f"  Target:  [grey82]{e(r.get('target','?'))}[/grey82]\n"
                f"  Reason:  [grey82]{e(r.get('reason','?'))}[/grey82]\n"
                f"  Detail:  [grey82]{e(r.get('detail',''))}[/grey82]\n"
                f"  Status:  [grey82]{e(r.get('status','?'))}[/grey82]\n"
                f"  By:      [grey82]{e(r.get('reporter','?'))}[/grey82]"
            )
        elif op == "report_closed":
            self._info(f"Report {e(p.get('report_id','?')[:8])} closed: {e(p.get('outcome','?'))}")
        elif op == "report_created":
            self._info(f"Report submitted. ID: {e(p.get('report_id','?')[:8])}")
        elif op == "audit_log":
            entries = p.get("entries", [])
            if not entries: self._info("Audit log empty.")
            else:
                self._markup("[bright_cyan]── Audit Log ─────────────────────[/bright_cyan]")
                for entry in entries:
                    t   = fmt_time(entry.get("timestamp",""))
                    act = e(entry.get("action","?"))
                    by  = e(entry.get("by","?"))
                    tgt = e(entry.get("target",""))
                    self._markup(f"  [dim][{t}][/dim]  [grey82]{by}[/grey82] → [grey82]{act}[/grey82]  [dim]{tgt}[/dim]")

        # ── Account ────────────────────────────────────────────────────────
        elif op == "user_deleted":
            self._info(f"User {e(p.get('target_id','?'))} deleted.")
        elif op == "account_deleted":
            self._markup("[red1]Your account has been permanently deleted.[/red1]")
            asyncio.ensure_future(self._do_logout())
        elif op == "roles_info":
            roles = p.get("roles", [])
            self._markup("[bright_cyan]── Roles ─────────────────────────[/bright_cyan]")
            for r in roles:
                rc = ROLE_COLOR.get(r.get("name","user"), "grey82")
                self._markup(f"  [{rc}]{e(r.get('name','?'))}[/{rc}]  [dim]{e(r.get('desc',''))}[/dim]")
        elif op == "version_info":
            sv = p.get("server_version","?")
            self._markup(
                f"[bright_cyan]NEXUS[/bright_cyan]  "
                f"[grey82]client {CLIENT_VERSION}  .  server {e(sv)}[/grey82]"
            )
        elif op == "whoami":
            role = p.get("role", self._session.role)
            rc   = ROLE_COLOR.get(role, "grey82")
            self._markup(
                f"[bright_cyan]You:[/bright_cyan]  "
                f"[{rc}]{e(p.get('username', self._session.username))}[/{rc}]  "
                f"[dim]{e(p.get('public_id', self._session.public_id))}[/dim]  "
                f"[{rc}]{e(role)}[/{rc}]  "
                f"[dim]PIN: {'set' if p.get('pin_set') else 'not set'}[/dim]"
            )

        # ── Search / history ───────────────────────────────────────────────
        elif op == "history":
            msgs = p.get("messages", [])
            if not msgs: self._info("No history found.")
            else:
                self._sys(f"History ({len(msgs)})")
                for m in msgs: self._render_msg(m)
        elif op == "search_results":
            results = p.get("results", [])
            if not results: self._info("No messages found.")
            else:
                self._sys(f"Search results ({len(results)})")
                for m in results: self._render_msg(m)

        # ── Server / guardian ──────────────────────────────────────────────
        elif op in ("guardian_stats","guardian_events","guardian_sessions",
                    "guardian_ack","broadcast_sent","lockdown_update",
                    "backup_done","shutdown_pending"):
            if self._guardian and self._guardian.is_attached:
                self._guardian.handle_packet(p)
            else:
                self._markup(f"[dim][guardian] {e(str(p))}[/dim]")
        elif op == "guardian_alert":
            sev = p.get("severity","info"); col = SEV_COLOR.get(sev,"grey82")
            self._markup(f"[{col}][GUARDIAN/{sev.upper()}][/{col}] {e(p.get('message',''))}")
            if self._guardian and self._guardian.is_attached:
                self._guardian.handle_packet(p)
        elif op == "session_displaced":
            # The server is closing this connection: somebody signed in to
            # this account somewhere else. Say so plainly rather than
            # letting it look like the network dropped - and remember it, so
            # the close that follows does not then blame the network anyway.
            self._displaced = True
            self._markup(
                "[red1 bold]!  Signed out: NEXUS was opened somewhere "
                "else.[/red1 bold]"
            )
            self._markup(
                "[dim]   Only one session can be open at a time. "
                "Restart this client to take the account back.[/dim]"
            )
        elif op == "shutdown_notice":
            self._markup(f"[red1 bold]!  SERVER SHUTDOWN: {e(p.get('reason',''))}[/red1 bold]")
        elif op == "wws_status":
            st = p.get("status", {})
            self._markup("[bright_cyan]── WWS Status ─────────────────────[/bright_cyan]")
            for k, v in st.items():
                self._markup(f"  [dim]{e(k)}:[/dim] [grey82]{e(str(v))}[/grey82]")

        # ── VoltDeck ───────────────────────────────────────────────────────
        elif op == "vdkey_result":
            key = e(p.get("key", "?"))
            for_user = e(p.get("for_user", "?"))
            note = e(p.get("note", ""))
            self._markup(
                f"[bright_cyan]VoltDeck key for [bold]{for_user}[/bold]:[/bright_cyan]\n"
                f"  [bold yellow1]{key}[/bold yellow1]\n"
                f"  [dim]{note}[/dim]"
            )
        elif op == "vdkey_list":
            keys = p.get("keys", [])
            for_user = e(p.get("for_user", "?"))
            if not keys:
                self._info(f"No VoltDeck keys for {for_user}.")
            else:
                self._markup(f"[bright_cyan]── VoltDeck Keys for {for_user} ────────────[/bright_cyan]")
                for k in keys:
                    status = "[green3]active[/green3]" if k.get("active") else "[red1]revoked[/red1]"
                    self._markup(
                        f"  [grey82]{e(k.get('key_id','?'))}[/grey82]  "
                        f"{status}  "
                        f"[dim]{e(k.get('label',''))}  ch={e(k.get('channel','?'))}  "
                        f"created={e(k.get('created','?')[:10])}[/dim]"
                    )
        elif op == "vdkey_revoked":
            self._info(f"VoltDeck key {e(p.get('key_id','?'))} revoked.")

        # ── Misc ───────────────────────────────────────────────────────────
        elif op == "pong":
            self._info("Pong!")
        elif op == "error":
            self._err(p.get("message", "Server error"))

    # ── Message rendering ─────────────────────────────────────────────────────

    def _render_msg(self, p: dict, convo: "str | None" = None):
        role   = p.get("role","user")
        sender = p.get("username","?")
        body   = p.get("content","")
        msg_id = p.get("message_id","") or p.get("msg_id","")
        t      = fmt_time(p.get("timestamp",""))
        mark   = ROLE_MARK.get(role, "")
        col    = ROLE_COLOR.get(role, "grey82")
        id_tag = ""
        # The id is what /delete and /edit take, so it has to be visible on
        # anything you are allowed to delete or edit. Your own messages
        # always; everybody's from moderator up, who can delete anyone's.
        if msg_id and (sender == self._session.username
                       or ROLE_RANK.get(self._session.role, 0)
                       >= ROLE_RANK["moderator"]):
            id_tag = f" [dim]#{e(msg_id[:6])}[/dim]"
        reply_tag = ""
        if p.get("reply_to"):
            reply_tag = f"[dim] <-{e(p['reply_to'][:6])}[/dim] "
        channel = p.get("channel", "")
        self._markup(
            f"[dim][{t}][/dim] "
            + (f"[{col}]{e(mark)}[/{col}] " if mark else "")
            + f"[{col}]{e(sender)}[/{col}]{id_tag}: {reply_tag}{e(body)}",
            convo=convo or (f"#{channel}" if channel else None),
        )

    def _render_dm(self, p: dict):
        role   = p.get("role","user")
        sender = p.get("from","?")
        # The copy sent to the recipient carries no "to" - the server only
        # puts one on the sender's echo. An incoming DM without one arrived
        # here, so the recipient is this account.
        target = p.get("to") or self._session.username
        body   = p.get("content","")
        t      = fmt_time(p.get("timestamp",""))
        col    = ROLE_COLOR.get(role,"grey82")
        mark   = ROLE_MARK.get(role,"")
        # Track in sidebar
        other = sender if sender != self._session.username else target
        if other and other not in self._session.dm_convos:
            self._session.dm_convos.append(other)
            self._rebuild_dm_list()

        key = f"@{other}" if other else None
        self._markup(
            f"[dim][{t}][/dim] "
            + (f"[{col}]{e(mark)}[/{col}] " if mark else "")
            + f"[{col}]{e(sender)}[/{col}] → [{col}]{e(target)}[/{col}]: {e(body)}",
            convo=key,
        )
        # A direct message that arrives while you are reading a channel no
        # longer appears in it. Say that it came rather than let it land
        # silently in a pane you are not looking at.
        if key and key != self._view_key() and sender != self._session.username:
            self._markup(
                f"[bright_cyan]--  direct message from "
                f"{e(sender)}  ([dim]/dm {e(sender)}[/dim] to read)[/bright_cyan]"
            )

    # ── Chat log helpers ──────────────────────────────────────────────────────

    def _view_key(self) -> str:
        """Which conversation is on screen right now."""
        return f"@{self._dm_target}" if self._dm_mode else f"#{self._channel}"

    def _markup(self, m: str, convo: "str | None" = None):
        """Record a line against a conversation, and show it if that is the
        one being looked at.

        *convo* is the conversation the line belongs to. Left out, it means
        the current view - which is right for notices, errors and anything
        else about the here and now.
        """
        key = convo or self._view_key()
        buf = self._buffers.setdefault(key, [])
        buf.append(m)
        if len(buf) > SCROLLBACK:
            del buf[:-SCROLLBACK]
        if key != self._view_key():
            return                      # belongs to a pane you are not on
        try: self.query_one("#chat-log", RichLog).write(Text.from_markup(m))
        except Exception: pass

    def _show_conversation(self):
        """Clear the pane and replay the transcript for the current view."""
        try:
            log = self.query_one("#chat-log", RichLog)
        except NoMatches:
            return
        log.clear()
        for line in self._buffers.get(self._view_key(), []):
            try: log.write(Text.from_markup(line))
            except Exception: pass

    def _info(self, m: str):
        self._markup(f"[bright_cyan]i  {m}[/bright_cyan]")

    def _warn(self, m: str):
        self._markup(f"[yellow1]!  {m}[/yellow1]")

    def _err(self, m: str):
        self._markup(f"[red1][!]  {m}[/red1]")

    def _sys(self, m: str):
        self._markup(f"[dim]── {m}[/dim]")

    # ── Sidebar / topbar helpers ──────────────────────────────────────────────

    def _refresh_topbar(self):
        """Rebuild the single-line topbar: user · role · location."""
        role = self._session.role
        col  = ROLE_COLOR.get(role, "grey82")
        mark = ROLE_MARK.get(role, "")
        prefix = f"{e(mark)} " if mark else ""
        if self._dm_mode:
            loc = f"@{e(self._dm_target)}"
        elif self._channel:
            loc = f"#{e(self._channel)}"
        else:
            loc = "no channel"
        try:
            self.query_one("#topbar", Static).update(
                f"[{col}]{prefix}{e(self._session.username)}[/{col}]"
                f"  [dim]{e(role)}[/dim]"
                f"  [dim]|[/dim]  {loc}"
            )
        except NoMatches:
            pass

    def _update_topic(self, text: str):
        """Legacy helper — now just refreshes the topbar (location already encoded there)."""
        self._refresh_topbar()

    def _update_me_label(self):
        """Legacy helper — now just refreshes the topbar."""
        self._refresh_topbar()

    def _update_online_count(self, n: int):
        try:
            self.query_one("#status", Static).update(
                f"[dim]{n} online[/dim]{self._security_note()}"
            )
        except NoMatches:
            pass

    def _security_note(self) -> str:
        """How this connection is protected, said plainly in the status bar.

        An unverified certificate is still encrypted, which is worth having,
        but it is not the same as a verified one and should not look like
        it. Plaintext gets said loudest, because that is the one where a
        passkey crosses the network readable.
        """
        how = getattr(self._session, "security", "")
        if not how:
            return ""
        if how.startswith("NOT"):
            return "   [red1]connection NOT encrypted[/red1]"
        if "new server" in how:
            return "   [yellow1]encrypted - new server, remembered[/yellow1]"
        if "not verified" in how:
            return "   [yellow1]encrypted, certificate unverified[/yellow1]"
        if "pinned" in how:
            return "   [green3]encrypted (pinned)[/green3]"
        return "   [green3]encrypted[/green3]"

    def _rebuild_channel_list(self):
        try:
            lv = self.query_one("#channel-list", ListView)
            lv.clear()
            for ch in self._session.channels:
                active = ch == self._channel and not self._dm_mode
                lbl = (f"[bright_cyan]#[/bright_cyan]{e(ch)}" if active
                       else f"[dim]#[/dim]{e(ch)}")
                lv.append(ListItem(Label(Text.from_markup(lbl)), name=ch))
        except NoMatches: pass

    def _rebuild_dm_list(self):
        try:
            lv = self.query_one("#dm-list", ListView)
            lv.clear()
            for user in self._session.dm_convos:
                active = self._dm_mode and self._dm_target == user
                lbl = (f"[bright_cyan]@{e(user)}[/bright_cyan]" if active
                       else f"[dim]@{e(user)}[/dim]")
                lv.append(ListItem(Label(Text.from_markup(lbl)), name=f"dm:{user}"))
        except NoMatches: pass

    # ── Input ─────────────────────────────────────────────────────────────────

    @on(Input.Changed, "#chat-input")
    def on_input_changed(self, event: Input.Changed) -> None:
        """Show command hints in #suggestions while the user types."""
        value = event.value
        hint  = ""
        ALL_CMDS = [
            "/help", "/who", "/join", "/leave", "/channels", "/dm", "/dms",
            "/back", "/history", "/search", "/reply", "/edit", "/delete",
            "/clear", "/add", "/accept", "/decline", "/unfriend", "/friends",
            "/favourite", "/block", "/unblock", "/blocked", "/report",
            "/find", "/whoami", "/ping", "/version", "/logout", "/quit",
            "/guardian", "/mycode", "/verify", "/trust", "/devices", "/vault",
            "/pin", "/listed", "/releasekeys", "/makechannel", "/mychannel",
            "/groups", "/g", "/group", "/channel", "/invite", "/invites",
            "/revoke", "/rolekeys", "/rolekey", "/promote", "/demote",
            "/mute", "/unmute", "/kick", "/ban", "/unban", "/warn",
            "/audit", "/reports", "/closereport", "/announce", "/notice",
            "/shutdown", "/lockdown", "/backup", "/theme", "/voltdeck",
        ]
        if value.startswith("/") and " " not in value:
            matches = [c for c in ALL_CMDS if c.startswith(value.lower())]
            hint = "  ".join(matches[:10])
        try:
            self.query_one("#suggestions", Static).update(hint)
        except NoMatches:
            pass

    @on(Input.Submitted, "#chat-input")
    async def on_submit(self, event: Input.Submitted):
        text = event.value.strip(); event.input.value = ""
        if not text: return
        if text.startswith("/"):
            await self._handle_command(text)
        else:
            await self._send_msg(text)

    async def _send_msg(self, text: str):
        try:
            if self._dm_mode:
                await self._session.send(op="dm", to=self._dm_target, content=text)
            else:
                await self._session.send(op="msg", channel=self._channel, content=text)
        except Exception as exc:
            self._err(f"Send failed: {exc}")

    async def _join(self, channel: str):
        try: await self._session.send(op="join", channel=channel)
        except Exception as exc: self._err(f"Join failed: {exc}")

    @on(ListView.Selected, "#channel-list")
    async def on_channel_click(self, event: ListView.Selected):
        name = event.item.name
        if name:
            if name != self._channel:
                self._dm_mode = False; self._dm_target = ""
                await self._join(name)
            elif self._dm_mode:
                # Already joined, but reading a DM - just come back to it
                # rather than asking the server for the history again.
                self._dm_mode = False; self._dm_target = ""
                self._refresh_topbar()
                self._rebuild_channel_list(); self._rebuild_dm_list()
                self._show_conversation()
        # Hand typing back, or the next keystroke drives the list again.
        self.query_one("#chat-input", Input).focus()

    @on(ListView.Selected, "#dm-list")
    async def on_dm_click(self, event: ListView.Selected):
        name = event.item.name or ""
        if name.startswith("dm:"):
            target = name[3:]
            self._dm_mode = True; self._dm_target = target
            self._refresh_topbar()
            self._rebuild_dm_list()
            self._show_conversation()
            if not self._buffers.get(self._view_key()):
                self._sys(f"DM view with @{target}  .  /back to return")
        self.query_one("#chat-input", Input).focus()

    # ── Packet helpers (channel events) ──────────────────────────────────────

    def _on_channel_joined(self, p: dict):
        ch      = p.get("channel","")
        history = p.get("history",[])
        users   = p.get("users",[])

        self._dm_mode = False
        self._dm_target = ""
        self._channel = ch
        if ch not in self._session.channels:
            self._session.channels.append(ch)
        self._refresh_topbar()
        self._rebuild_channel_list()
        self._rebuild_dm_list()
        self._update_online_count(len(users))

        # The server returns the channel's history on every join, and that
        # is the authoritative record - so the pane is rebuilt from it
        # rather than added to. Anything this client had buffered for the
        # channel is replaced, which is also what stops a second visit
        # showing every message twice.
        key = f"#{ch}"
        self._buffers[key] = []
        try: self.query_one("#chat-log", RichLog).clear()
        except Exception: pass

        # The server does not always report who is present; saying
        # "(0 online)" when it stayed quiet reads as nobody being there.
        self._sys(f"Joined #{ch}" + (f"  ({len(users)} online)" if users else ""))
        for h in history:
            self._render_msg(h, convo=key)

    def _on_channels(self, p: dict):
        # The server describes each channel as {name, desc, kind}; the sidebar
        # and this line both want the bare name. Formatting the dicts printed
        # the whole record into the chat pane.
        names = [c.get("name", "") if isinstance(c, dict) else str(c)
                 for c in p.get("channels", [])]
        names = [n for n in names if n]
        self._session.channels = names
        self._rebuild_channel_list()
        self._info("Channels: " + "  ".join(f"#{e(c)}" for c in names))

    def _on_users(self, p: dict):
        users = p.get("users",[])
        self._update_online_count(len(users))
        lines = ["[bright_cyan]── Online ───────────────────────[/bright_cyan]"]
        for u in users:
            role = u.get("role","user"); col = ROLE_COLOR.get(role,"grey82")
            mark = ROLE_MARK.get(role,"")
            lines.append(
                f"  [{col}]{e(mark + ' ' if mark else '')}{e(u.get('username','?'))}[/{col}]"
                f"  [dim]{e(u.get('public_id',''))}[/dim]"
            )
        for l in lines: self._markup(l)

    # ── Commands ──────────────────────────────────────────────────────────────

    async def _handle_command(self, text: str):
        parts   = text.split()
        cmd     = parts[0].lower()
        args    = parts[1:]
        my_rank = ROLE_RANK.get(self._session.role, 0)

        async def send(**kw):
            try: await self._session.send(**kw)
            except Exception as exc: self._err(str(exc))

        # ━━━━━━━━━━━━━━ TALKING ━━━━━━━━━━━━━━

        if cmd in ("/quit", "/q", "/exit"):
            self.app.exit()
            return

        if cmd == "/help":
            await self._cmd_help(my_rank, args)
            return

        if cmd == "/who":
            await send(op="users"); return

        if cmd == "/history":
            count = int(args[0]) if args and args[0].isdigit() else 50
            await send(op="history", channel=self._channel, count=count); return

        if cmd == "/search":
            if not args: self._warn("Usage: /search <text>"); return
            await send(op="search", channel=self._channel, query=" ".join(args)); return

        if cmd == "/reply":
            if len(args) < 2: self._warn("Usage: /reply <id> <text>"); return
            await send(op="msg", channel=self._channel,
                       content=" ".join(args[1:]), reply_to=args[0]); return

        if cmd == "/edit":
            if len(args) < 2: self._warn("Usage: /edit <id> <new text>"); return
            await send(op="edit_msg", msg_id=args[0], content=" ".join(args[1:])); return

        if cmd == "/delete":
            if not args: self._warn("Usage: /delete <message_id>"); return
            await send(op="delete_msg", msg_id=args[0]); return

        if cmd == "/clear":
            self._buffers[self._view_key()] = []
            try: self.query_one("#chat-log", RichLog).clear()
            except Exception: pass
            return

        # ━━━━━━━━━━━━ CHANNELS & GROUPS ━━━━━━━━━━━━

        if cmd == "/join":
            if not args: self._warn("Usage: /join #channel"); return
            ch = args[0].lstrip("#")
            was_dm = self._dm_mode
            self._dm_mode = False; self._dm_target = ""
            if ch == self._channel and was_dm:
                self._refresh_topbar(); self._show_conversation(); return
            await self._join(ch); return

        if cmd == "/leave":
            ch = (args[0].lstrip("#") if args else self._channel)
            await send(op="leave", channel=ch); return

        if cmd == "/channels":
            await send(op="channels"); return

        if cmd == "/makechannel":
            if not args: self._warn("Usage: /makechannel <name> [topic]"); return
            name  = args[0].lstrip("#")
            topic = " ".join(args[1:]) if len(args) > 1 else ""
            await send(op="makechannel", name=name, topic=topic); return

        if cmd == "/mychannel":
            action = args[0].lower() if args else "info"
            await send(op="mychannel", action=action); return

        if cmd == "/groups":
            await send(op="groups"); return

        if cmd == "/g":
            if not args: self._warn("Usage: /g <group>"); return
            await send(op="join_group", group=args[0]); return

        if cmd == "/group":
            if not args: self._warn("Usage: /group new|join|leave|invite|members|delete|open ..."); return
            await send(op="group", action=args[0], args=args[1:]); return

        if cmd == "/channel":
            if len(args) < 2: self._warn("Usage: /channel new|delete <name> [kind]"); return
            await send(op="channel_admin", action=args[0], name=args[1],
                       kind=args[2] if len(args) > 2 else "public"); return

        # ━━━━━━━━━━━━━━ PEOPLE ━━━━━━━━━━━━━━

        if cmd == "/dm":
            if not args: self._warn("Usage: /dm @user [message]"); return
            target = args[0].lstrip("@")
            body   = " ".join(args[1:])
            if body:
                await send(op="dm", to=target, content=body)
            else:
                self._dm_mode = True; self._dm_target = target
                if target not in self._session.dm_convos:
                    self._session.dm_convos.append(target)
                    self._rebuild_dm_list()
                self._refresh_topbar()
                self._rebuild_dm_list()
                self._show_conversation()
                if not self._buffers.get(self._view_key()):
                    self._sys(
                        f"DM view with @{target}  .  type /back to return"
                    )
            return

        if cmd == "/dms":
            await send(op="dms"); return

        if cmd == "/back":
            self._dm_mode = False; self._dm_target = ""
            self._refresh_topbar()
            self._rebuild_channel_list(); self._rebuild_dm_list()
            self._show_conversation(); return

        if cmd == "/find":
            if not args: self._warn("Usage: /find <user or #ID>"); return
            await send(op="find_user", query=args[0].lstrip("@")); return

        if cmd == "/add":
            if not args: self._warn("Usage: /add <user, #ID or contact code>"); return
            await send(op="add_friend", target=args[0].lstrip("@")); return

        if cmd == "/accept":
            if not args: self._warn("Usage: /accept <user>"); return
            await send(op="accept_friend", target=args[0].lstrip("@")); return

        if cmd == "/decline":
            if not args: self._warn("Usage: /decline <user>"); return
            await send(op="decline_friend", target=args[0].lstrip("@")); return

        if cmd == "/unfriend":
            if not args: self._warn("Usage: /unfriend <user>"); return
            await send(op="unfriend", target=args[0].lstrip("@")); return

        if cmd == "/friends":
            await send(op="friends"); return

        if cmd == "/favourite":
            if not args: self._warn("Usage: /favourite <user>"); return
            await send(op="favourite", target=args[0].lstrip("@")); return

        if cmd == "/block":
            if not args: self._warn("Usage: /block <user>"); return
            await send(op="block", target=args[0].lstrip("@")); return

        if cmd == "/unblock":
            if not args: self._warn("Usage: /unblock <user>"); return
            await send(op="unblock", target=args[0].lstrip("@")); return

        if cmd == "/blocked":
            await send(op="blocked"); return

        # ━━━━━━━━━━━━━━ SECURITY ━━━━━━━━━━━━━━

        if cmd == "/mycode":
            await send(op="mycode"); return

        if cmd == "/verify":
            target = args[0].lstrip("@") if args else ""
            await send(op="verify", target=target); return

        if cmd == "/trust":
            action = args[0].lower() if args else "list"
            if action == "list":
                await send(op="trust", action="list")
            elif action == "accept" and len(args) >= 3:
                await send(op="trust", action="accept", target=args[1], code=args[2])
            elif action == "forget" and len(args) >= 2:
                await send(op="trust", action="forget", target=args[1])
            else:
                self._warn("Usage: /trust [list|accept <user> <code>|forget <user>]")
            return

        if cmd == "/devices":
            action = args[0].lower() if args else "list"
            if action == "list":
                await send(op="devices", action="list")
            elif action == "rename" and len(args) >= 3:
                await send(op="devices", action="rename", device_id=args[1], name=" ".join(args[2:]))
            elif action == "revoke" and len(args) >= 2:
                await send(op="devices", action="revoke", device_id=args[1])
            elif action == "revoke" and len(args) == 1:
                self._warn("/devices revoke <id>  or  /devices revoke others")
            elif action == "others":
                await send(op="devices", action="revoke_others")
            else:
                self._warn("Usage: /devices [rename <id> <name>|revoke <id>|revoke others]")
            return

        if cmd == "/vault":
            action = args[0].lower() if args else "status"
            await send(op="vault", action=action); return

        if cmd == "/releasekeys":
            await send(op="releasekeys"); return

        if cmd == "/pin":
            if not args: self._warn("Usage: /pin <digits>|clear"); return
            action = args[0]
            if action == "clear": await send(op="set_pin", action="clear")
            elif action.isdigit(): await send(op="set_pin", pin=action)
            else: self._warn("Usage: /pin <digits>|clear")
            return

        if cmd == "/listed":
            if not args or args[0].lower() not in ("on","off"):
                self._warn("Usage: /listed on|off"); return
            await send(op="set_listed", state=args[0].lower() == "on"); return

        if cmd == "/report":
            if not args: self._warn("Usage: /report <user> [reason] [detail]"); return
            target = args[0].lstrip("@")
            reason = args[1] if len(args) > 1 else ""
            detail = " ".join(args[2:]) if len(args) > 2 else ""
            await send(op="report", target=target, reason=reason, detail=detail); return

        if cmd == "/report-message":
            if not args: self._warn("Usage: /report-message <id> [reason]"); return
            await send(op="report_msg", msg_id=args[0],
                       reason=" ".join(args[1:]) if len(args) > 1 else ""); return

        if cmd == "/review":
            if len(args) < 2: self._warn("Usage: /review yes|no <request-id>"); return
            await send(op="review", answer=args[0].lower(), request_id=args[1]); return

        # ━━━━━━━━━━━━ YOUR ACCOUNT ━━━━━━━━━━━━━

        if cmd in ("/whoami", "/me"):
            await send(op="whoami"); return

        if cmd == "/roles":
            await send(op="roles"); return

        if cmd == "/settings":
            await send(op="settings", args=args); return

        if cmd == "/theme":
            themes = list(THEMES.keys())
            if not args or args[0] not in THEMES:
                self._info(f"Available themes: {', '.join(themes)}")
                self._info(f"Usage: /theme <name>")
                return
            name = args[0]
            if set_theme(name):
                NexusApp.CSS = build_css()
                self.app.refresh_css()
                self._info(f"Theme → [bold]{name}[/bold]")
            return

        if cmd == "/version":
            await send(op="version"); return

        if cmd == "/checkupdate":
            await send(op="checkupdate"); return

        if cmd == "/update":
            go = "go" in [a.lower() for a in args]
            await send(op="update", apply=go); return

        if cmd == "/logout":
            await self._do_logout(); return

        if cmd == "/delete-account":
            if not args: self._warn("Usage: /delete-account <passkey>"); return
            confirmed = await self.app.push_screen_wait(
                ConfirmScreen("Permanently delete your account?", danger=True)
            )
            if confirmed:
                await send(op="delete_account", passkey=args[0])
            return

        if cmd in ("/roleup", "/rc"):
            if not args: self._warn("Usage: /roleup NX-UP-..."); return
            await send(op="roleup", key=args[0]); return

        if cmd == "/ping":
            await send(op="ping"); return

        # ━━━━━━━━━━━━ MODERATION ━━━━━━━━━━━━━

        if cmd == "/warn":
            if my_rank < ROLE_RANK["moderator"]:
                self._err("Moderators and above can warn users."); return
            if len(args) < 2: self._warn("Usage: /warn <user> <reason>"); return
            await send(op="warn", target=args[0].lstrip("@"), reason=" ".join(args[1:])); return

        if cmd == "/mute":
            if my_rank < ROLE_RANK["moderator"]:
                self._err("Moderators and above can mute users."); return
            if not args: self._warn("Usage: /mute <user> [duration] [reason]"); return
            target = args[0].lstrip("@")
            dur    = parse_duration(args[1]) if len(args) > 1 else ""
            reason = " ".join(args[2:] if dur else args[1:])
            await send(op="mute", target=target, duration=dur, reason=reason); return

        if cmd == "/unmute":
            if my_rank < ROLE_RANK["moderator"]:
                self._err("Moderators and above can unmute users."); return
            if not args: self._warn("Usage: /unmute <user>"); return
            await send(op="unmute", target=args[0].lstrip("@")); return

        if cmd == "/kick":
            if my_rank < ROLE_RANK["moderator"]:
                self._err("Moderators and above can kick users."); return
            if not args: self._warn("Usage: /kick <user> [reason]"); return
            await send(op="kick", target=args[0].lstrip("@"),
                       reason=" ".join(args[1:]) if len(args) > 1 else ""); return

        if cmd == "/reports":
            if my_rank < ROLE_RANK["moderator"]:
                self._err("Moderators and above can view reports."); return
            filt = args[0].lower() if args else "open"
            await send(op="reports", filter=filt); return

        if cmd == "/report-view":
            if my_rank < ROLE_RANK["moderator"]:
                self._err("Moderators and above can view reports."); return
            if not args: self._warn("Usage: /report-view <id>"); return
            await send(op="report_view", report_id=args[0]); return

        if cmd == "/report-close":
            if my_rank < ROLE_RANK["moderator"]:
                self._err("Moderators and above can close reports."); return
            if len(args) < 2: self._warn("Usage: /report-close <id> <outcome>"); return
            await send(op="report_close", report_id=args[0], outcome=" ".join(args[1:])); return

        if cmd == "/ban":
            if my_rank < ROLE_RANK["admin"]:
                self._err("Admins and above can ban users."); return
            if not args: self._warn("Usage: /ban <user> [duration] [reason]"); return
            target = args[0].lstrip("@")
            dur    = parse_duration(args[1]) if len(args) > 1 else ""
            reason = " ".join(args[2:] if dur else args[1:])
            await send(op="ban", target=target, duration=dur, reason=reason); return

        if cmd == "/unban":
            if my_rank < ROLE_RANK["admin"]:
                self._err("Admins and above can unban users."); return
            if not args: self._warn("Usage: /unban <user>"); return
            await send(op="unban", target=args[0].lstrip("@")); return

        if cmd == "/announce":
            if my_rank < ROLE_RANK["admin"]:
                self._err("Admins and above can announce."); return
            if not args: self._warn("Usage: /announce <text>"); return
            await send(op="announce", text=" ".join(args)); return

        if cmd == "/review-request":
            if my_rank < ROLE_RANK["admin"]:
                self._err("Admins and above can request reviews."); return
            if len(args) < 2: self._warn("Usage: /review-request <report-id> <user>"); return
            await send(op="review_request", report_id=args[0],
                       target=args[1].lstrip("@")); return

        if cmd == "/review-read":
            if my_rank < ROLE_RANK["admin"]:
                self._err("Admins and above can read review contexts."); return
            if len(args) < 2: self._warn("Usage: /review-read <report-id> <user>"); return
            await send(op="review_read", report_id=args[0],
                       target=args[1].lstrip("@")); return

        # ━━━━━━━━━━━━ RUNNING THE SERVER ━━━━━━━━━━━━

        if cmd == "/invite":
            if my_rank < ROLE_RANK["helper"]:
                self._err("Helpers and above can mint invites."); return
            # /invite list | revoke <id> | [role] [note]
            if args and args[0].lower() == "list":
                await send(op="invite", action="list"); return
            if args and args[0].lower() == "revoke":
                if len(args) < 2: self._warn("Usage: /invite revoke <id>"); return
                await send(op="invite", action="revoke", code=args[1]); return
            # Mint invite
            target_role = "user"; note = ""
            allowed = {
                "helper":    ["user"],
                "moderator": ["user","helper"],
                "admin":     ["user","helper","moderator"],
                "owner":     ["user","helper","moderator","admin"],
            }
            ok_roles = allowed.get(self._session.role, [])
            if args and args[0].lower() in ok_roles:
                target_role = args[0].lower()
                note = " ".join(args[1:])
            elif args and args[0].lower() not in ok_roles and args[0].lower() != "user":
                self._err(f"You can only mint invites for: {', '.join(ok_roles)}"); return
            await send(op="invite", action="create", role=target_role, note=note); return

        if cmd == "/passkey":
            if my_rank < ROLE_RANK["admin"]:
                self._err("Admins and above can manage passkeys."); return
            if not args: self._warn("Usage: /passkey new|revoke ..."); return
            await send(op="passkey_admin", action=args[0], args=args[1:]); return

        if cmd == "/vdkey":
            if my_rank < ROLE_RANK["admin"]:
                self._err("Admins and above can create VoltDeck keys."); return
            target = args[0] if args else self._session.username
            channel = "voltdeck"
            label   = ""
            i = 1
            while i < len(args):
                if args[i] == "--channel" and i + 1 < len(args):
                    channel = args[i + 1]; i += 2
                elif args[i] == "--label" and i + 1 < len(args):
                    label = args[i + 1]; i += 2
                else:
                    i += 1
            await send(op="vdkey_create", target=target, channel=channel, label=label); return

        if cmd == "/vdkeys":
            if my_rank < ROLE_RANK["admin"]:
                self._err("Admins and above can list VoltDeck keys."); return
            target = args[0] if args else self._session.username
            await send(op="vdkey_list", target=target); return

        if cmd == "/vdrevoke":
            if my_rank < ROLE_RANK["admin"]:
                self._err("Admins and above can revoke VoltDeck keys."); return
            if not args: self._warn("Usage: /vdrevoke <key_id>"); return
            await send(op="vdkey_revoke", key_id=args[0]); return

        if cmd == "/rolekey":
            if my_rank < ROLE_RANK["admin"]:
                self._err("Admins and above can create role keys."); return
            if not args: self._warn("Usage: /rolekey <role> [note]"); return
            await send(op="rolekey", role=args[0],
                       note=" ".join(args[1:]) if len(args) > 1 else ""); return

        if cmd == "/rolekeys":
            if my_rank < ROLE_RANK["admin"]:
                self._err("Admins and above can list role keys."); return
            await send(op="rolekeys"); return

        if cmd == "/audit":
            if my_rank < ROLE_RANK["admin"]:
                self._err("Admins and above can view the audit log."); return
            count = int(args[0]) if args and args[0].isdigit() else 50
            await send(op="audit", count=count); return

        if cmd == "/promote":
            if my_rank < ROLE_RANK["owner"]:
                self._err("Owners can promote users."); return
            if len(args) < 2: self._warn("Usage: /promote <user> <role>"); return
            await send(op="promote", target=args[0].lstrip("@"), role=args[1]); return

        if cmd == "/guardian":
            if my_rank < ROLE_RANK["owner"]:
                self._err("Guardian is owner-only."); return
            self._guardian = GuardianScreen(self._session)
            await self.app.push_screen(self._guardian); return

        if cmd == "/backup":
            if my_rank < ROLE_RANK["owner"]:
                self._err("Owners can trigger backups."); return
            await send(op="backup"); return

        if cmd == "/shutdown":
            if my_rank < ROLE_RANK["owner"]:
                self._err("Owners can shut down the server."); return
            reason = " ".join(args) if args else "Manual shutdown"
            confirmed = await self.app.push_screen_wait(
                ConfirmScreen(f"Shut down server? Reason: {reason}", danger=True)
            )
            if confirmed: await send(op="shutdown", reason=reason); return

        if cmd == "/wws":
            if my_rank < ROLE_RANK["owner"]:
                self._err("Owners can manage WWS."); return
            action = args[0].lower() if args else "status"
            await send(op="wws", action=action, args=args[1:]); return

        if cmd == "/notice":
            if my_rank < ROLE_RANK["owner"]:
                self._err("System/owner can send notices."); return
            if not args: self._warn("Usage: /notice [<user>] <text>"); return
            # If first arg looks like a username, target it; otherwise broadcast
            if len(args) >= 2 and not args[0].startswith("/"):
                await send(op="notice", target=args[0].lstrip("@"), text=" ".join(args[1:]))
            else:
                await send(op="notice", target="", text=" ".join(args))
            return

        if cmd == "/owner":
            if my_rank < ROLE_RANK["system"]:
                self._err("System-only command."); return
            if len(args) < 2: self._warn("Usage: /owner grant|revoke <user>"); return
            await send(op="owner_transfer", action=args[0].lower(),
                       target=args[1].lstrip("@")); return

        # ━━━━━━━━━━━━━━ UNKNOWN ━━━━━━━━━━━━━━

        self._warn(f"Unknown command: {cmd}  (type /help)")

    # ── /help command ──────────────────────────────────────────────────────────

    async def _cmd_help(self, my_rank: int, args: list):
        a = accent()
        lines = [
            f"[{a}]── NEXUS Commands ─────────────────────────────────────[/{a}]",
            "",
            f"[{a}]TALKING[/{a}]",
            "[dim]  /help [command]       List commands or explain one[/dim]",
            "[dim]  /who                  Everyone currently online[/dim]",
            "[dim]  /history [count]      Reload recent messages[/dim]",
            "[dim]  /search <text>        Search message history[/dim]",
            "[dim]  /reply <id> <text>    Reply to a message[/dim]",
            "[dim]  /edit <id> <text>     Edit one of your messages[/dim]",
            "[dim]  /delete <id>          Delete one of your messages[/dim]",
            "[dim]  /clear                Clear the chat pane[/dim]",
            "",
            f"[{a}]CHANNELS AND GROUPS[/{a}]",
            "[dim]  /join #channel        Open a channel[/dim]",
            "[dim]  /leave [name]         Leave this channel or group[/dim]",
            "[dim]  /channels             Refresh the channel list[/dim]",
            "[dim]  /makechannel <name>   Make a channel of your own[/dim]",
            "[dim]  /groups               List groups you can see[/dim]",
            "[dim]  /g <group>            Open a group you are in[/dim]",
            "[dim]  /group new|join|...   Group management[/dim]",
            "",
            f"[{a}]DIRECT MESSAGES[/{a}]",
            "[dim]  /dm @user [text]      Open or send a DM[/dim]",
            "[dim]  /dms                  Your DM conversations[/dim]",
            "[dim]  /back                 Return to channel view[/dim]",
            "",
            f"[{a}]PEOPLE[/{a}]",
            "[dim]  /find @user           Look someone up[/dim]",
            "[dim]  /add @user            Send a friend request[/dim]",
            "[dim]  /accept @user         Accept a friend request[/dim]",
            "[dim]  /decline @user        Decline a friend request[/dim]",
            "[dim]  /unfriend @user       Remove a friend[/dim]",
            "[dim]  /friends              Friends, requests and blocks[/dim]",
            "[dim]  /favourite @user      Pin friend to top[/dim]",
            "[dim]  /block @user          Block all messages[/dim]",
            "[dim]  /unblock @user        Lift a block[/dim]",
            "[dim]  /blocked              List who you have blocked[/dim]",
            "",
            f"[{a}]SECURITY[/{a}]",
            "[dim]  /mycode               Your contact code[/dim]",
            "[dim]  /verify [@user]       Compare safety number[/dim]",
            "[dim]  /trust [list|accept|forget]  Manage trusted keys[/dim]",
            "[dim]  /devices [rename|revoke|...]  Your sessions[/dim]",
            "[dim]  /vault [status|lock]  Encrypted local store[/dim]",
            "[dim]  /pin <digits>|clear   Set or remove PIN[/dim]",
            "[dim]  /listed on|off        Directory visibility[/dim]",
            "[dim]  /report @user [reason]  Report a user[/dim]",
            "[dim]  /report-message <id>  Report a message[/dim]",
            "",
            f"[{a}]YOUR ACCOUNT[/{a}]",
            "[dim]  /whoami               Username, ID, role, PIN state[/dim]",
            "[dim]  /roles                Rank list and descriptions[/dim]",
            "[dim]  /settings             How things are shown[/dim]",
            f"[dim]  /theme [name]         Switch colour theme  ({', '.join(THEMES.keys())})[/dim]",
            "[dim]  /version              Client and server version[/dim]",
            "[dim]  /checkupdate          Check for a newer NEXUS[/dim]",
            "[dim]  /logout               Sign out[/dim]",
            "[dim]  /delete-account <pk>  Permanently delete account[/dim]",
            "[dim]  /ping                 Ping server[/dim]",
            "[dim]  /quit                 Exit[/dim]",
        ]
        if my_rank >= ROLE_RANK["helper"]:
            lines += [
                "",
                f"[{a}]MODERATION  [dim](your role: {self._session.role})[/dim][/{a}]",
            ]
        if my_rank >= ROLE_RANK["moderator"]:
            lines += [
                "[dim]  /warn @user <reason>       Warn a user[/dim]",
                "[dim]  /mute @user [dur] [reason]  Mute a user[/dim]",
                "[dim]  /unmute @user              Unmute[/dim]",
                "[dim]  /kick @user [reason]        Kick from channel[/dim]",
                "[dim]  /reports [open|closed|all]  View reports[/dim]",
                "[dim]  /report-view <id>           View a report[/dim]",
                "[dim]  /report-close <id> <outcome> Close a report[/dim]",
                "[dim]  /invite                     Mint a user invite[/dim]",
            ]
        if my_rank >= ROLE_RANK["admin"]:
            lines += [
                "[dim]  /ban @user [dur] [reason]   Ban a user[/dim]",
                "[dim]  /unban @user                Unban[/dim]",
                "[dim]  /announce <text>             Server-wide message[/dim]",
                "[dim]  /invite [role] [note]        Mint a role invite[/dim]",
                "[dim]  /invite list|revoke          Manage invites[/dim]",
                "[dim]  /rolekey <role> [note]       Create upgrade key[/dim]",
                "[dim]  /rolekeys                    List upgrade keys[/dim]",
                "[dim]  /audit [count]               Audit log[/dim]",
                "[dim]  /vdkey [@user] [--channel c] [--label l]  Create VoltDeck key[/dim]",
                "[dim]  /vdkeys [@user]              List VoltDeck keys[/dim]",
                "[dim]  /vdrevoke <key_id>           Revoke a VoltDeck key[/dim]",
            ]
        if my_rank >= ROLE_RANK["owner"]:
            lines += [
                "[dim]  /promote @user <role>        Change a role[/dim]",
                "[dim]  /guardian [command]          Security console[/dim]",
                "[dim]  /backup                      Trigger a backup[/dim]",
                "[dim]  /shutdown [reason]           Shut down server[/dim]",
                "[dim]  /wws status|request|...      WWS control[/dim]",
                "[dim]  /notice [@user] <text>       Send a notice[/dim]",
            ]
        for l in lines: self._markup(l)

    # ── Logout ────────────────────────────────────────────────────────────────

    async def _do_logout(self):
        clear_token(self._host, self._port)
        if self._recv_task: self._recv_task.cancel()
        await self._session.close()
        self.app.pop_screen()
        self.app.run_worker(self.app._login_flow(), exclusive=True)

    def action_quit_app(self):
        self.app.exit()

# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────

class NexusApp(App):
    CSS = build_css()
    TITLE = "NEXUS"

    def __init__(self, host: str, port: int,
                 passkey: str | None = None,
                 invite: str | None = None,
                 username: str | None = None,
                 tls: "bool | None" = None, insecure_tls: bool = False,
                 cafile: "str | None" = None):
        super().__init__()
        self._host = host
        self._port = port
        self._tls = tls
        self._insecure_tls = insecure_tls
        self._cafile = cafile
        self._cli_passkey = passkey
        self._cli_invite   = invite
        self._cli_username = username

    def on_mount(self) -> None:
        self.run_worker(self._login_flow(), exclusive=True)

    async def _login_flow(self):
        host, port = self._host, self._port

        # 1. Try cached token
        token = load_token(host, port)
        if token:
            sess = Session(host, port, self._tls, self._insecure_tls, self._cafile)
            try:
                await sess.connect()
                await sess.send(op="auth", token=token)
                pkt = await sess.recv()
                if pkt.get("type") == "auth_ok":
                    sess.username  = pkt["username"]
                    sess.public_id = pkt.get("public_id","")
                    sess.role      = pkt.get("role","user")
                    sess.token     = token
                    sess.channels  = pkt.get("channels",["general"])
                    await self.push_screen(ChatScreen(sess, host, port))
                    return
                clear_token(host, port)
                await sess.close()
            except Exception:
                clear_token(host, port)

        # 2. Login loop
        # Consume CLI flags (first pass only)
        _cli_pk  = self._cli_passkey;  self._cli_passkey = None
        _cli_inv = self._cli_invite;   self._cli_invite  = None
        _cli_un  = self._cli_username; self._cli_username = None

        while True:
            # ── Passkey path ──────────────────────────────────────────────────
            if _cli_pk:
                pk = _cli_pk
                _cli_pk = None
            elif _cli_inv:
                # skip login screen entirely -- go straight to register
                pk = None
                choice = "invite"
            else:
                choice = await self.push_screen_wait(LoginScreen())
                if choice == "passkey":
                    pk = await self.push_screen_wait(PasskeyScreen())
                    if pk is None: continue
                else:
                    pk = None

            if pk is not None:
                sess = Session(host, port, self._tls, self._insecure_tls, self._cafile)
                try:
                    await sess.connect()
                    await sess.send(op="auth", passkey=pk)
                    pkt = await sess.recv()
                except Exception as exc:
                    await self._alert(f"Connection failed: {exc}"); continue
                if pkt.get("type") != "auth_ok":
                    await self._alert(pkt.get("text","Authentication failed.")); await sess.close(); continue
                sess.username  = pkt["username"]
                sess.public_id = pkt.get("public_id","")
                sess.role      = pkt.get("role","user")
                sess.token     = pkt.get("token","")
                sess.channels  = pkt.get("channels",["general"])
                save_token(host, port, sess.token)
                await self.push_screen(ChatScreen(sess, host, port))
                return

            # ── Invite / register path ────────────────────────────────────────
            if choice == "invite":
                if _cli_inv:
                    inv = _cli_inv; _cli_inv = None
                else:
                    inv = await self.push_screen_wait(InviteScreen())
                    if inv is None: continue
                if _cli_un:
                    un = _cli_un; _cli_un = None
                else:
                    un = await self.push_screen_wait(UsernameScreen())
                    if un is None: continue
                sess = Session(host, port, self._tls, self._insecure_tls, self._cafile)
                try:
                    await sess.connect()
                    await sess.send(op="register", invite=inv, username=un)
                    pkt = await sess.recv()
                except Exception as exc:
                    await self._alert(f"Connection failed: {exc}"); continue
                if pkt.get("type") != "registered":
                    await self._alert(pkt.get("text","Registration failed.")); await sess.close(); continue
                passkey        = pkt["passkey"]
                sess.username  = pkt["username"]
                sess.public_id = pkt.get("public_id","")
                sess.role      = pkt.get("role","user")
                sess.token     = pkt.get("token","")
                sess.channels  = pkt.get("channels",["general"])
                save_token(host, port, sess.token)
                await self.push_screen_wait(PasskeyDisplayScreen(passkey))
                await self.push_screen(ChatScreen(sess, host, port))
                return

    async def _alert(self, msg: str):
        class _Alert(ModalScreen):
            def compose(self_):
                with Vertical(id="invite-box"):
                    yield Static("Error", classes="modal-title")
                    yield Static(msg, classes="modal-label")
                    yield Button("OK", id="btn-ok", classes="modal-btn-primary")
            @on(Button.Pressed, "#btn-ok")
            def ok(self_): self_.dismiss(None)
        await self.push_screen_wait(_Alert())

# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NEXUS terminal chat client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--theme", default="nexus", choices=list(THEMES.keys()))
    parser.add_argument("--passkey",  default=None,
                        help="Sign in with this passkey (skips the prompt)")
    parser.add_argument("--invite",   default=None,
                        help="Register using this invite code (skips the prompt)")
    parser.add_argument("--username", default=None,
                        help="Username to use when registering with --invite")
    parser.add_argument("--tls", action="store_true",
                        help="Force wss://. Not normally needed: without "
                             "this the client tries encrypted first and "
                             "falls back to plaintext on its own.")
    parser.add_argument("--no-tls", action="store_true",
                        help="Force plain ws:// and skip the TLS attempt.")
    parser.add_argument("--cafile", default=os.environ.get("NEXUS_CAFILE"),
                        help="Trust this certificate authority, e.g. the "
                             "nexus-ca.pem from 'cert --ca'. Needed on "
                             "macOS, where Python does not read the "
                             "Keychain. Also read from NEXUS_CAFILE.")
    parser.add_argument("--insecure-tls", action="store_true",
                        help="Accept a certificate nothing vouches for. "
                             "This turns off the check that the server is "
                             "who it says. Prefer --cafile.")
    args = parser.parse_args()
    set_theme(args.theme)
    NexusApp.CSS = build_css()
    NexusApp(args.host, args.port,
             tls=(False if args.no_tls else
                  (True if (args.tls or args.insecure_tls) else None)),
             insecure_tls=args.insecure_tls,
             cafile=args.cafile,
             passkey=args.passkey,
             invite=args.invite,
             username=args.username).run()

if __name__ == "__main__":
    main()
