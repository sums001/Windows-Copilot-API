"""Signed-in session caching for the pure-HTTP path.

Bridges the one-time browser login to the headless :class:`copilot.driver.Copilot`
driver. ``session/token.json`` holds the chat access token (valid ~8h), the
refresh token that renews it, and the account id the chat socket is routed by.
Renewal is plain HTTP; the browser is only a fallback when the refresh token has
expired, and for the very first sign-in.
"""

import json
import time
from pathlib import Path
from typing import Optional

from curl_cffi.requests import Session

from .protocol import (
    BROKER_CLIENT_ID, BROKER_REDIRECT_URI, CHAT_CLIENT_ID, CHAT_REDIRECT_URI,
    CHAT_SCOPE, TOKEN_ORIGIN, TOKEN_URL,
)

# All session state (browser profile + cached auth) lives under one folder.
SESSION_DIR = "session"
DEFAULT_PROFILE_DIR = f"{SESSION_DIR}/profile"
DEFAULT_AUTH_FILE = f"{SESSION_DIR}/token.json"
# Chat access tokens live ~8h; this caps how long a cached one is trusted.
AUTH_MAX_AGE = 7 * 60 * 60
# Renew this long before the token's own expiry.
_EXPIRY_MARGIN = 5 * 60


def auth_from_token_response(body: dict, oid: str, anchor: str, stamp: float) -> dict:
    """Build the ``token.json`` dict from an OAuth token-endpoint response."""
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "oid": oid,
        "anchor": anchor,
        "expires_at": stamp + int(body.get("expires_in", 3600)),
        "saved_at": stamp,
    }


def save_auth(auth: dict, path: str = DEFAULT_AUTH_FILE) -> dict:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(auth, indent=2), encoding="utf-8")
    return auth


def refresh_auth(auth: dict, proxy: Optional[str] = None) -> Optional[dict]:
    """Renew the chat token over plain HTTP with the cached refresh token.

    Mirrors the request copilot.com's own MSAL broker makes. Returns the new
    auth dict, or ``None`` if the refresh token is missing, expired or revoked.
    """
    if not auth.get("refresh_token"):
        return None
    data = {
        "client_id": CHAT_CLIENT_ID,
        "redirect_uri": CHAT_REDIRECT_URI,
        "scope": CHAT_SCOPE,
        "grant_type": "refresh_token",
        "refresh_token": auth["refresh_token"],
        "client_info": "1",
        "brk_client_id": BROKER_CLIENT_ID,
        "brk_redirect_uri": BROKER_REDIRECT_URI,
    }
    if auth.get("anchor"):
        data["X-AnchorMailbox"] = auth["anchor"]
    try:
        with Session(impersonate="chrome", proxy=proxy, timeout=30) as session:
            response = session.post(
                f"{TOKEN_URL}?brk_client_id={BROKER_CLIENT_ID}",
                data=data,
                headers={"Origin": TOKEN_ORIGIN},
            )
        body = response.json()
    except Exception:
        return None
    if response.status_code != 200 or "access_token" not in body:
        return None
    renewed = auth_from_token_response(body, auth["oid"], auth.get("anchor"), time.time())
    # Some responses omit a new refresh token; keep the old one then.
    renewed["refresh_token"] = renewed["refresh_token"] or auth["refresh_token"]
    return renewed


def load_auth(
    path: str = DEFAULT_AUTH_FILE,
    profile_dir: str = DEFAULT_PROFILE_DIR,
    max_age: int = AUTH_MAX_AGE,
    proxy: Optional[str] = None,
    auto_login: bool = True,
    force: bool = False,
) -> dict:
    """Return ``{access_token, refresh_token, oid, ...}`` for the signed-in user.

    In order: the cached token at ``path`` while fresh; a plain-HTTP refresh with
    the cached refresh token; a headless browser on the persistent
    ``profile_dir`` re-minting the token; and — when nothing is signed in yet and
    ``auto_login`` is true — a visible browser for interactive sign-in. Set
    ``auto_login=False`` (headless/CI) to get a ``RuntimeError`` instead.
    ``force`` skips the cached token (e.g. after the server rejected it).

    Intended for the pure-HTTP :class:`copilot.driver.Copilot` path::

        auth = load_auth()
        Copilot().create_completion(..., access_token=auth["access_token"],
                                    oid=auth["oid"])
    """
    cached: dict = {}
    p = Path(path)
    if p.exists():
        try:
            cached = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            cached = {}  # corrupt/unreadable -> refresh below
    now = time.time()
    if (not force and cached.get("access_token") and cached.get("oid")
            and now < cached.get("expires_at", 0) - _EXPIRY_MARGIN
            and now - cached.get("saved_at", 0) < max_age):
        return cached

    if cached.get("oid"):
        renewed = refresh_auth(cached, proxy=proxy)
        if renewed:
            return save_auth(renewed, path)

    from .browser import BrowserCopilot

    # Refresh token expired: a signed-in profile can still mint a new one.
    bot = BrowserCopilot(profile_dir=profile_dir, headless=True, proxy=proxy)
    try:
        bot.start()
        auth = bot.capture_auth()
    finally:
        bot.close()
    if auth:
        return save_auth(auth, path)

    if not auto_login:
        raise RuntimeError(
            "Not signed in (no Copilot token in the browser profile). "
            "Run `python -m copilot login` and sign in first."
        )

    print("No saved Copilot session found — opening a browser to sign in...")
    auth = BrowserCopilot(profile_dir=profile_dir, headless=False, proxy=proxy).login(path=path)
    if not auth.get("access_token"):
        raise RuntimeError(
            "Sign-in did not complete (no Copilot token captured). "
            "Run `python -m copilot login` and finish the Microsoft sign-in."
        )
    return auth
