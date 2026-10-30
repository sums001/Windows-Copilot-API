"""Browser-backed sign-in and chat-token capture.

Playwright support for the pure-HTTP :class:`copilot.driver.Copilot`: it does NOT
chat. Its only job is to sign in and capture the tokens the HTTP driver runs on.

``BrowserCopilot`` launches a **persistent** Chromium profile so the sign-in
survives restarts. When copilot.com loads signed in, its MSAL client requests a
chat token (scope ``substrate.office.com/sydney``) from the token endpoint; we
read that response — access token + refresh token — and the account id from the
request. After that, :mod:`copilot.auth` renews the token over plain HTTP and the
browser isn't needed again until the refresh token expires.

  * :meth:`login` — visible window for interactive sign-in, then snapshots
    ``session/token.json``.
  * :meth:`capture_auth` — headless: re-mint the token on an already signed-in
    profile (the fallback when the refresh token has expired).

Recapture token shapes with ``tests/diagnostic.py`` if Microsoft changes them.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from .auth import DEFAULT_AUTH_FILE, DEFAULT_PROFILE_DIR, auth_from_token_response, save_auth

NEW_CHAT_URL = "https://copilot.com/chat"
_LOGIN_HOSTS = ("login.microsoftonline.com", "login.live.com")

# copilot.com's own session cookie (OH.SID) — and Microsoft's sign-in cookies,
# unless "Stay signed in" was chosen — are session cookies, which Chromium drops
# when the browser closes, signing the profile out (issue #34). Like Chrome's
# "continue where you left off", keep them next to the profile and restore them.
_SESSION_COOKIES_FILE = "session_cookies.json"

# Drop the page's cached chat access token so MSAL fetches a fresh one over the
# network (with its refresh token) where we can read it. The cache values are
# encrypted, but the key names are plain: msal.<v>|...|accesstoken|...|<scope>|
_DROP_CHAT_TOKEN_JS = """
() => {
  let n = 0;
  for (const k of Object.keys(localStorage)) {
    if (k.indexOf('|accesstoken|') !== -1 && k.indexOf('substrate.office.com/sydney') !== -1) {
      localStorage.removeItem(k); n++;
    }
  }
  return n;
}
"""

_SSO_GRACE = 10  # seconds a /login redirect may take to bounce back via SSO


class BrowserCopilot:
    """Signs in to copilot.com in a persistent Playwright browser.

    Parameters
    ----------
    profile_dir:
        Directory for the persistent Chromium profile (cookies, sign-in).
        Reused across runs.
    headless:
        Run without a visible window. Use ``False`` (or :meth:`login`) for the
        interactive sign-in.
    """

    label = "Microsoft Copilot (browser)"

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
        # Route the browser through a proxy if needed, e.g.
        # proxy="http://user:pass@host:port" or "socks5://host:port".
        self.proxy = proxy

        self._pw = None
        self._context = None
        self._page = None
        self._login_log_fh = None
        self._captured: Optional[dict] = None
        self._cookies_file = Path(self.profile_dir).parent / _SESSION_COOKIES_FILE

    # -- lifecycle ----------------------------------------------------------

    def start(self, headless: Optional[bool] = None) -> "BrowserCopilot":
        """Launch the persistent browser context (no navigation yet)."""
        if self._context is not None:
            return self
        if headless is not None:
            self.headless = headless
        try:
            self._pw = sync_playwright().start()
            # The browser presents its own, real User-Agent: pinning a fixed one
            # contradicted the browser's client hints (issue #57).
            launch_kwargs = dict(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
            if self.proxy:
                launch_kwargs["proxy"] = self._parse_proxy(self.proxy)
            self._context = self._pw.chromium.launch_persistent_context(
                self.profile_dir, **launch_kwargs
            )
            self._restore_session_cookies()
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.set_default_timeout(self.nav_timeout * 1000)
            self._page.on("response", self._on_response)
        except PlaywrightError as exc:
            self.close()
            raise ConnectionError(f"Failed to start browser: {exc}") from exc
        return self

    @staticmethod
    def _parse_proxy(proxy: str) -> dict:
        """Turn a ``scheme://user:pass@host:port`` string into Playwright form."""
        u = urlparse(proxy)
        server = f"{u.scheme}://{u.hostname}:{u.port}" if u.port else f"{u.scheme}://{u.hostname}"
        cfg = {"server": server}
        if u.username:
            cfg["username"] = u.username
        if u.password:
            cfg["password"] = u.password
        return cfg

    def close(self) -> None:
        self._save_session_cookies()
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

    def _save_session_cookies(self) -> None:
        if self._context is None:
            return
        try:
            cookies = [c for c in self._context.cookies() if c.get("expires", -1) == -1]
            self._cookies_file.write_text(json.dumps(cookies), encoding="utf-8")
        except (PlaywrightError, OSError):
            pass

    def _restore_session_cookies(self) -> None:
        try:
            cookies = json.loads(self._cookies_file.read_text(encoding="utf-8"))
            self._context.add_cookies(cookies)
        except (OSError, ValueError, PlaywrightError):
            pass  # no/stale snapshot just means signing in again

    # -- token capture --------------------------------------------------------

    def _on_response(self, response) -> None:
        """Capture the chat token from copilot.com's own token-endpoint call."""
        try:
            if "/oauth2/v2.0/token" not in response.url or response.status != 200:
                return
            form = parse_qs(response.request.post_data or "")
            if "substrate.office.com/sydney" not in (form.get("scope") or [""])[0]:
                return
            body = response.json()
            anchor = (form.get("X-AnchorMailbox") or [None])[0]  # "Oid:<oid>@<tenant>"
            if not anchor or "access_token" not in body:
                return
            oid = anchor.split(":", 1)[-1].split("@", 1)[0]
            self._captured = auth_from_token_response(body, oid, anchor, time.time())
        except Exception:
            pass  # unrelated/unreadable response

    def _on_login_page(self) -> bool:
        u = urlparse(self._page.url)
        if u.hostname in _LOGIN_HOSTS:
            return True
        return (u.hostname or "").endswith("copilot.com") and u.path.startswith("/login")

    def _window_closed(self) -> bool:
        try:
            return self._page is None or self._page.is_closed()
        except Exception:
            return True

    def _await_token(self, timeout: float, give_up_on_login: bool) -> Optional[dict]:
        """Wait for the page to request a chat token; nudge it once if it doesn't.

        A page holding a still-valid cached token makes no token request, so
        after a short wait we drop that cached entry and reload — MSAL then
        fetches a fresh one (with its refresh token) over the network."""
        deadline = time.time() + timeout
        nudge_at = time.time() + 8
        nudged = False
        on_login_since: Optional[float] = None
        while time.time() < deadline:
            if self._captured:
                return self._captured
            if self._window_closed():
                return None
            if self._on_login_page():
                on_login_since = on_login_since or time.time()
                if give_up_on_login and time.time() - on_login_since > _SSO_GRACE:
                    return None
                nudge_at = time.time() + 8  # still signing in; don't nudge yet
            else:
                on_login_since = None
                if not nudged and time.time() >= nudge_at:
                    nudged = True
                    try:
                        if self._page.evaluate(_DROP_CHAT_TOKEN_JS):
                            self._page.reload(wait_until="domcontentloaded")
                    except PlaywrightError:
                        pass
            try:
                self._page.wait_for_timeout(500)
            except PlaywrightError:
                return None
        return self._captured

    def capture_auth(self, timeout: int = 45) -> Optional[dict]:
        """Headless: return a fresh auth dict from a signed-in profile, or None."""
        self.start()
        self._captured = None
        try:
            self._page.goto(NEW_CHAT_URL, wait_until="domcontentloaded")
        except PlaywrightError:
            return None
        return self._await_token(timeout, give_up_on_login=True)

    # -- auth ---------------------------------------------------------------

    def login(self, path: str = DEFAULT_AUTH_FILE, timeout: int = 300) -> dict:
        """Open a visible window for interactive Microsoft sign-in.

        Finishes by itself once copilot.com, signed in, requests its chat token —
        which we capture and snapshot to ``path``. No key-press needed; the
        browser closes itself. Steps are appended to ``<session>/login.log``.
        """
        self.close()
        self.start(headless=False)
        self._captured = None

        log = self._open_login_log(Path(path).resolve().parent / "login.log")
        log(f"login started; browser open at {NEW_CHAT_URL}")
        self._mirror_page_events(log)

        print(
            "\nA browser window is open at copilot.com.\n"
            "Sign in with your Microsoft account, and choose 'Yes' if asked to stay signed in.\n"
            "It finishes by itself once sign-in is detected — no need to press Enter.\n"
        )

        auth: dict = {}
        try:
            self._page.goto(NEW_CHAT_URL, wait_until="domcontentloaded")
            captured = self._await_token(timeout, give_up_on_login=False)
        except PlaywrightError as exc:
            log(f"login error: {exc}")
            captured = None
        if captured:
            auth = save_auth(captured, path)
            log(f"chat token captured; auth snapshot saved to {path}")
            print(f"Signed in. Auth snapshot saved to {path}")
        else:
            log("no chat token captured (window closed, timed out, or not signed in)")
            print("Sign-in not detected; no token saved.")

        log("closing browser")
        self.close()
        return auth

    def _open_login_log(self, log_path: Path):
        """Return a best-effort timestamped append-logger to ``log_path``."""
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
