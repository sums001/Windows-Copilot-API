"""Browser-backed Copilot driver.

A Playwright fallback for the pure-HTTP :class:`copilot.client.Copilot`: it runs
the *exact same protocol* inside a real browser that already holds Cloudflare
clearance and (optionally) a signed-in Microsoft session. Useful if Microsoft
ever escalates the challenge to a Cloudflare Turnstile CAPTCHA, which needs a
browser-solved token.

``BrowserCopilot`` launches a **persistent** Playwright Chromium profile so that
Cloudflare clearance and any sign-in survive restarts. The chat protocol
(``POST /c/api/conversations`` then a ``wss://.../c/api/chat`` WebSocket speaking
``send`` -> ``appendText``* -> ``done``) is executed *in the page* via
``page.evaluate`` so the browser's own ``fetch``/``WebSocket`` carry the cookies,
Cloudflare token, and auth headers.

It exposes the same ``create_completion(prompt, stream=...)`` generator API as
:class:`copilot.client.Copilot`, so it is a drop-in replacement.

PROTOCOL ASSUMPTIONS (verify at runtime against a live session):
  * Conversation create:  POST /c/api/conversations  -> {"id": "..."}
  * Chat socket:          wss://copilot.microsoft.com/c/api/chat?api-version=2
                          &clientSessionId=<uuid> (with &accessToken=<token> when
                          signed in)
  * Handshake:            send SET_OPTIONS_FRAME then CONSENTS_FRAME before the
                          first send, or the backend returns invalid-event
  * Send frame:           {"event":"send","conversationId":...,
                           "content":[{"type":"text","text":...}],
                           "mode":"smart","context":{}}
  * Stream frames:        {"event":"appendText","text":...}, then {"event":"done"}
The wire shapes are the single source of truth in :mod:`copilot.protocol`; these
JS templates just replay them. Recapture with ``tests/diagnostic.py`` if Microsoft
changes the protocol.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Generator, Optional
from urllib.parse import quote

from playwright.sync_api import sync_playwright, Error as PlaywrightError

from .auth import DEFAULT_AUTH_FILE, DEFAULT_PROFILE_DIR
from .protocol import CHAT_WEBSOCKET_URL, CONSENTS_FRAME, SET_OPTIONS_FRAME

COPILOT_URL = "https://copilot.microsoft.com/"

# --- in-page JavaScript -----------------------------------------------------

# Create a conversation. Runs in the page so cookies/Cloudflare apply.
_CREATE_CONVERSATION_JS = """
async () => {
  const res = await fetch('/c/api/conversations', {
    method: 'POST',
    credentials: 'include',
    headers: {'content-type': 'application/json'},
  });
  const text = await res.text();
  if (!res.ok) return {ok: false, status: res.status, text: text};
  let data = {};
  try { data = JSON.parse(text); } catch (e) {}
  return {ok: true, id: data.id || data.conversationId || null, raw: text};
}
"""

# Discover the Copilot chat MSAL access token from localStorage. The cache holds
# several tokens for different scopes; the chat WebSocket only accepts the one
# scoped 'ChatAI.ReadWrite' — a wrong-audience token (e.g. the Graph
# User.Read/Files.Read token) makes the WS upgrade 401. We therefore PREFER the
# ChatAI token and only fall back to the first token found if none matches.
# Returns null for anonymous sessions (anonymous chat may still work via cookies).
_FIND_TOKEN_JS = """
() => {
  try {
    let fallback = null;
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      const v = localStorage.getItem(k);
      if (v && v.indexOf('"credentialType":"AccessToken"') !== -1) {
        try {
          const o = JSON.parse(v);
          if (o && o.secret) {
            // Match the chat scope (e.g. '<resource>/ChatAI.ReadWrite'); take the
            // first non-matching token only as a last-resort fallback.
            if (o.target && o.target.indexOf('ChatAI') !== -1) return o.secret;
            if (!fallback) fallback = o.secret;
          }
        } catch (e) {}
      }
    }
    return fallback;
  } catch (e) {}
  return null;
}
"""

# Open the chat WebSocket and wire handlers that push into a window-scoped
# buffer. Returns immediately; messages accumulate while Python polls.
_START_STREAM_JS = """
([url, conversationId, prompt, prelude]) => {
  const state = {queue: [], done: false, error: null, started: false};
  window.__copilot = state;
  let ws;
  try { ws = new WebSocket(url); } catch (e) { state.error = 'ws-init: ' + e; state.done = true; return false; }
  window.__copilotWs = ws;
  ws.onopen = () => {
    // Initialise the session (setOptions, reportLocalConsents) before sending,
    // or the backend rejects `send` with invalid-event.
    for (const frame of prelude) ws.send(JSON.stringify(frame));
    ws.send(JSON.stringify({
      event: 'send',
      conversationId: conversationId,
      content: [{type: 'text', text: prompt}],
      mode: 'smart',
      context: {}
    }));
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    const e = msg.event;
    if (e === 'appendText') { state.started = true; if (msg.text) state.queue.push(msg.text); }
    else if (e === 'done') { state.done = true; try { ws.close(); } catch (x) {} }
    else if (e === 'error') { state.error = JSON.stringify(msg); state.done = true; try { ws.close(); } catch (x) {} }
  };
  ws.onerror = () => { state.error = state.error || 'websocket error'; state.done = true; };
  ws.onclose = () => { state.done = true; };
  return true;
}
"""

# Drain the buffer and report status in one round-trip.
_POLL_JS = """
() => {
  const s = window.__copilot || {queue: [], done: true, error: 'not started', started: false};
  const q = s.queue;
  s.queue = [];
  return {q: q, done: s.done, error: s.error, started: s.started};
}
"""


class BrowserCopilot:
    """Drives Microsoft Copilot through a real Playwright browser.

    Parameters
    ----------
    profile_dir:
        Directory for the persistent Chromium profile (cookies, Cloudflare
        clearance, sign-in). Reused across runs.
    headless:
        Run without a visible window. Use ``False`` (or :meth:`login`) for the
        first interactive sign-in, then ``True`` afterwards.
    """

    label = "Microsoft Copilot (browser)"
    default_model = "Copilot"

    def __init__(
        self,
        profile_dir: str = DEFAULT_PROFILE_DIR,
        headless: bool = True,
        nav_timeout: int = 60,
        proxy: Optional[str] = None,
    ):
        self.profile_dir = str(Path(profile_dir).resolve())
        self.headless = headless
        self.nav_timeout = nav_timeout
        # Copilot consumer chat is geo-restricted. If you are outside a supported
        # region, route the browser through a proxy/VPN in a supported region,
        # e.g. proxy="http://user:pass@host:port" or "socks5://host:port".
        self.proxy = proxy

        self._pw = None
        self._context = None
        self._page = None
        self._login_log_fh = None

    # -- lifecycle ----------------------------------------------------------

    def start(self, headless: Optional[bool] = None) -> "BrowserCopilot":
        """Launch the persistent browser context and open Copilot."""
        if self._context is not None:
            return self
        if headless is not None:
            self.headless = headless
        try:
            self._pw = sync_playwright().start()
            launch_kwargs = dict(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            if self.proxy:
                launch_kwargs["proxy"] = self._parse_proxy(self.proxy)
            self._context = self._pw.chromium.launch_persistent_context(
                self.profile_dir,
                **launch_kwargs,
            )
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.set_default_timeout(self.nav_timeout * 1000)
            self._page.goto(COPILOT_URL, wait_until="domcontentloaded")
            # Give Cloudflare a moment to clear on first paint. We deliberately do
            # NOT wait for "networkidle": Copilot's SPA keeps telemetry/heartbeat
            # connections open indefinitely, so the network never goes idle and the
            # wait would always time out. A short fixed settle is enough.
            self._page.wait_for_timeout(2000)
        except PlaywrightError as exc:
            self.close()
            raise ConnectionError(f"Failed to start browser: {exc}") from exc
        return self

    @staticmethod
    def _parse_proxy(proxy: str) -> dict:
        """Turn a ``scheme://user:pass@host:port`` string into Playwright form."""
        from urllib.parse import urlparse

        u = urlparse(proxy)
        server = f"{u.scheme}://{u.hostname}:{u.port}" if u.port else f"{u.scheme}://{u.hostname}"
        cfg = {"server": server}
        if u.username:
            cfg["username"] = u.username
        if u.password:
            cfg["password"] = u.password
        return cfg

    def region_blocked(self) -> bool:
        """True if Copilot is showing the 'Not available in your region' notice."""
        if self._page is None:
            return False
        try:
            text = self._page.evaluate("() => document.body ? document.body.innerText : ''")
        except PlaywrightError:
            return False
        return "available in your region" in (text or "").lower()

    def close(self) -> None:
        for attr, closer in (
            ("_context", lambda c: c.close()),
            ("_pw", lambda p: p.stop()),
            ("_login_log_fh", lambda f: f.close()),
        ):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    closer(obj)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._page = None

    def __enter__(self) -> "BrowserCopilot":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- auth ---------------------------------------------------------------

    def login(self, path: str = DEFAULT_AUTH_FILE, timeout: int = 300) -> dict:
        """Open a visible window for interactive Microsoft sign-in.

        Auto-detects success — the Copilot chat access token appearing in the
        page, the same signal :mod:`copilot.auth` uses — then snapshots the
        session and closes the browser by itself. No key-press needed. Every step
        is appended to ``<session>/login.log`` so a failed sign-in is diagnosable.
        ``timeout`` bounds the wait before giving up and snapshotting whatever
        state exists. The session persists in ``profile_dir`` for headless reuse.
        """
        self.close()
        self.start(headless=False)

        log = self._open_login_log(Path(path).resolve().parent / "login.log")
        log(f"login started; browser open at {COPILOT_URL}")
        self._mirror_page_events(log)

        print(
            "\nA browser window is open at copilot.microsoft.com.\n"
            "Sign in (and pass any 'verify you're human' check).\n"
            "It closes by itself once sign-in is detected — no need to press Enter.\n"
        )

        # Poll for the signed-in chat token; bail early if the user closes the
        # window or the timeout elapses.
        detected = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._window_closed():
                log("browser window closed before sign-in was detected")
                break
            try:
                token = self.access_token()
            except PlaywrightError:
                token = None
            if token:
                log("chat access token detected — sign-in successful")
                detected = True
                break
            try:
                self._page.wait_for_timeout(1500)
            except PlaywrightError:
                break

        if not detected:
            log(f"no chat access token within {timeout}s; snapshotting current state")
            print("Sign-in not auto-detected; saving whatever session state exists.")

        # Let cookies/token settle, then snapshot for the headless curl_cffi path.
        auth: dict = {}
        try:
            if detected and not self._window_closed():
                self._page.wait_for_timeout(800)
            auth = self.export_auth(path=path, stamp=time.time())
            log(f"auth snapshot saved to {path} (access_token={'yes' if auth.get('access_token') else 'no'})")
            print(f"Auth snapshot saved to {path}")
        except Exception as exc:
            log(f"could not snapshot auth: {exc}")
            print(f"(could not snapshot auth: {exc})")

        log("closing browser")
        self.close()
        print(f"Session saved to {self.profile_dir}")
        return auth

    def _open_login_log(self, log_path: Path):
        """Return a best-effort timestamped append-logger to ``log_path``.

        The handle is parked on the context so :meth:`close` can release it; if the
        file can't be opened, the returned logger is a silent no-op.
        """
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._login_log_fh = log_path.open("a", encoding="utf-8")
        except OSError:
            self._login_log_fh = None

        def log(message: str) -> None:
            fh = self._login_log_fh
            if fh is None:
                return
            try:
                fh.write(f"{datetime.now(timezone.utc).isoformat()}\t{message}\n")
                fh.flush()
            except Exception:
                pass

        return log

    def _mirror_page_events(self, log) -> None:
        """Stream main-frame navigations and console errors into the login log."""
        try:
            self._page.on(
                "framenavigated",
                lambda fr: fr == self._page.main_frame and log(f"navigated: {fr.url}"),
            )
            self._page.on(
                "console",
                lambda m: m.type == "error" and log(f"console.error: {m.text}"),
            )
        except PlaywrightError:
            pass

    def _window_closed(self) -> bool:
        """True if the page/context is gone (e.g. the user closed the window)."""
        try:
            return self._page is None or self._page.is_closed()
        except Exception:
            return True

    def access_token(self) -> Optional[str]:
        """Return the page's MSAL access token, or ``None`` if anonymous."""
        self._ensure_started()
        try:
            return self._page.evaluate(_FIND_TOKEN_JS)
        except PlaywrightError:
            return None

    def cookies(self) -> Dict[str, str]:
        """Return the signed-in Microsoft cookies as a name->value dict."""
        self._ensure_started()
        try:
            raw = self._context.cookies()
        except PlaywrightError:
            return {}
        return {c["name"]: c["value"] for c in raw if "microsoft.com" in c.get("domain", "")}

    def export_auth(self, path: str = DEFAULT_AUTH_FILE, stamp: Optional[float] = None) -> dict:
        """Snapshot the signed-in cookies + access token to ``path`` as JSON.

        ``stamp`` is the epoch seconds to record as ``saved_at`` (pass
        ``time.time()`` from the caller). Returns the auth dict.
        """
        auth = {
            "cookies": self.cookies(),
            "access_token": self.access_token(),
            "saved_at": stamp if stamp is not None else 0,
        }
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(auth, indent=2), encoding="utf-8")
        return auth

    # -- chat ---------------------------------------------------------------

    def create_completion(
        self,
        prompt: str,
        stream: bool = False,
        timeout: int = 900,
        **kwargs,
    ) -> Generator[str, None, None]:
        """Stream a Copilot reply to ``prompt``. Mirrors ``Copilot.create_completion``.

        Yields text chunks as they arrive. ``stream`` is accepted for API
        compatibility; chunks are always produced incrementally.
        """
        self._ensure_started()

        if self.region_blocked():
            raise RuntimeError(
                "Microsoft Copilot is not available in your region. "
                "Route the browser through a proxy/VPN in a supported region, e.g.:\n"
                "    BrowserCopilot(proxy='http://user:pass@host:port')\n"
                "or 'socks5://host:port'. See README for details."
            )

        conv = self._page.evaluate(_CREATE_CONVERSATION_JS)
        if not conv.get("ok"):
            status = conv.get("status")
            body = (conv.get("text") or "")[:500]
            if status in (401, 403):
                raise RuntimeError(
                    f"Conversation create returned HTTP {status}. "
                    f"Run login() / `python -m copilot login` to sign in. Body: {body}"
                )
            raise RuntimeError(f"Conversation create failed (HTTP {status}): {body}")

        conversation_id = conv.get("id")
        if not conversation_id:
            raise RuntimeError(f"No conversation id in response: {conv.get('raw')!r}")

        token = self._page.evaluate(_FIND_TOKEN_JS)

        ws_url = f"{CHAT_WEBSOCKET_URL}&clientSessionId={uuid.uuid4()}"
        if token:
            ws_url += f"&accessToken={quote(token)}"
        prelude = [SET_OPTIONS_FRAME, CONSENTS_FRAME]
        started_ok = self._page.evaluate(_START_STREAM_JS, [ws_url, conversation_id, prompt, prelude])
        if started_ok is False:
            state = self._page.evaluate(_POLL_JS)
            raise ConnectionError(f"WebSocket failed to start: {state.get('error')}")

        yield from self._pump(timeout)

    # -- internals ----------------------------------------------------------

    def _pump(self, timeout: int) -> Generator[str, None, None]:
        deadline = time.time() + timeout
        any_text = False
        while True:
            state = self._page.evaluate(_POLL_JS)
            for chunk in state.get("q") or []:
                if chunk:
                    any_text = True
                    yield chunk
            if state.get("error"):
                raise RuntimeError(f"Copilot error: {state['error']}")
            if state.get("done") and not state.get("q"):
                break
            if time.time() > deadline:
                raise TimeoutError(f"No 'done' within {timeout}s")
            time.sleep(0.08)

        if not any_text and not state.get("started"):
            raise RuntimeError("Invalid response: stream produced no text")

    def _ensure_started(self) -> None:
        if self._context is None or self._page is None:
            self.start()
