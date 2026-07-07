"""Capture the Trouter registrar POST body (Copilot Cowork).

The enterprise reply is pushed over Trouter only after the client registers its
Trouter endpoint with the messaging backend (``…/registrar/…/registrations``).
That request is issued from a Web Worker, so the page-level request listener in
``capture_m365.py`` never saw its body. This uses a **context-level route**, which
does intercept worker requests, to dump the registrar request (method, url,
headers, and full JSON body — the piece needed to build the registration) to
``session/registrar_capture.log``.

Run signed in with the WORK account; send one message so the app registers:

    python tests/capture_registrar.py

SAFE TO SHARE: bearer tokens / JWTs / emails are redacted; the registration body
shape (transports, ttl, clientDescription) is what we keep. Skim before posting.
"""

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SESSION_DIR = Path("session")
PROFILE_DIR = SESSION_DIR / "profile"
OUT = SESSION_DIR / "registrar_capture.log"
M365_CHAT_URL = "https://m365.cloud.microsoft/chat/"

_REDACTORS = [
    (re.compile(r"(Bearer)\s+[A-Za-z0-9._\-]+", re.I), r"\1 <REDACTED>"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)?"), "<JWT>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<EMAIL>"),
]


def redact(text: str) -> str:
    for pat, repl in _REDACTORS:
        text = pat.sub(repl, text)
    return text


def main() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    fh = OUT.open("w", encoding="utf-8")

    print("\n" + "=" * 70)
    print("Sign in (work account), wait for the chat, then TYPE A MESSAGE and send")
    print("it. The app registers its Trouter endpoint on load/first turn; that")
    print("registration body is captured. Close the window when the reply arrives.")
    print("=" * 70 + "\n")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR.resolve()), headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )

        def on_route(route):
            req = route.request
            try:
                if "registrar" in req.url or req.url.rstrip("/").endswith("/registrations"):
                    body = req.post_data or "(no body exposed)"
                    hdrs = {k: v for k, v in (req.headers or {}).items()
                            if k.lower() in ("content-type", "authorization", "x-skypetoken", "mstrouterservicedata")}
                    entry = (f"### {req.method} {req.url}\n"
                             f"# headers: {redact(str(hdrs))}\n"
                             f"# body:\n{redact(body)}\n\n")
                    fh.write(entry); fh.flush()
                    print(f"captured registrar {req.method} ({len(body)} body bytes)")
            except Exception as e:  # noqa: BLE001
                fh.write(f"# route error: {e}\n")
            try:
                route.continue_()
            except Exception:
                pass

        # Context-level route sees Web Worker requests (where the registrar call lives).
        ctx.route("**/*", on_route)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(M365_CHAT_URL, wait_until="domcontentloaded")
        except Exception:
            pass

        ticks = 600  # 5 min ceiling
        try:
            while not page.is_closed() and ticks > 0:
                page.wait_for_timeout(500)
                ticks -= 1
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass

    fh.close()
    print(f"\nRegistrar capture -> {OUT}")


if __name__ == "__main__":
    main()
