"""Diagnose (and often fix) a broken Copilot session — for bug reports.

One run does two jobs and writes two files under ``session/``:

  * **Capture** (interactive): opens a browser on the shared profile at
    copilot.com; you send ONE message in the Copilot UI. That captures the live
    chat protocol to ``session/ws_capture.log`` — what's needed to follow a
    protocol change in :mod:`copilot.protocol`.
  * **Log** (always): writes a redacted, shareable report to
    ``session/diagnostic_report.txt`` — environment, the *shape* of your saved
    session (never the secrets), a live chat probe, and redacted log tails.

Run::

    python tests/diagnostic.py                # browser capture + fix + report
    python tests/diagnostic.py --report-only  # headless/VPS: report only, no browser

SAFE TO SHARE. Secrets never reach either file as plaintext: the report reports
cookie *names* only and the token's *length*, and every captured/log line is run
through a redactor that strips access tokens, JWTs, OAuth codes, and emails —
including inside ``ws_capture.log`` itself. Skim before posting anyway, and
attach ``diagnostic_report.txt`` (not the raw capture).
"""

import argparse
import io
import json
import platform
import re
import sys
import time
import traceback
from pathlib import Path

# Run as a plain script (`python tests/diagnostic.py`): put the project root on
# sys.path so the `copilot` package imports without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SESSION_DIR = Path("session")
TOKEN_FILE = SESSION_DIR / "token.json"
PROFILE_DIR = SESSION_DIR / "profile"
LOGIN_LOG = SESSION_DIR / "login.log"
WS_LOG = SESSION_DIR / "ws_capture.log"
REPORT = SESSION_DIR / "diagnostic_report.txt"

COPILOT_URL = "https://copilot.com/"
CHAT_HINT = "/m365Copilot/Chathub/"  # the chat socket; other sockets are telemetry noise

# Packages whose versions matter for reproducing chat/auth issues.
_PACKAGES = ["curl_cffi", "playwright", "fastapi", "uvicorn", "pydantic", "websockets"]


_LOG_TAIL_LINES = 40       # trailing log lines to include (redacted)
_CAPTURE_TICKS = 600       # 600 * 500ms = 5 min ceiling on the browser capture


# --- redaction ------------------------------------------------------------
# Order matters: specific named params first, then a generic long-token catch-all.
_REDACTORS = [
    (re.compile(r"(accessToken|access_token)=[^&\s\"]+", re.I), r"\1=<REDACTED>"),
    (re.compile(r"(code|client_info|state|nonce|epct|epctrc|reconnectionToken)=[^&\s\"]+", re.I), r"\1=<REDACTED>"),
    # JWTs / MSAL artifacts: eyJ... .... (.optional third segment)
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)?"), "<JWT>"),
    (re.compile(r"\bM\.[A-Za-z0-9_\-.!*$%]{20,}"), "<AUTHCODE>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<EMAIL>"),
    # Catch-all: any remaining opaque run that looks like a secret.
    (re.compile(r"[A-Za-z0-9_\-]{40,}"), "<REDACTED>"),
]


def redact(text: str) -> str:
    """Strip secrets (tokens, JWTs, auth codes, emails) from a line of text."""
    for pattern, repl in _REDACTORS:
        text = pattern.sub(repl, text)
    return text


def _to_text(payload) -> str:
    if isinstance(payload, (bytes, bytearray)):
        return payload.decode("utf-8", "replace")
    return str(payload)


# --- report assembly ------------------------------------------------------
class Report:
    """Accumulates the report and echoes each line to the console as it's built."""

    def __init__(self) -> None:
        self._buf = io.StringIO()

    def line(self, text: str = "") -> None:
        print(text)
        self._buf.write(text + "\n")

    def section(self, title: str) -> None:
        self.line()
        self.line(f"== {title} " + "=" * max(0, 60 - len(title)))

    def text(self) -> str:
        return self._buf.getvalue()


def env_section(r: Report) -> None:
    r.section("Environment")
    r.line(f"python   : {platform.python_version()} ({sys.executable})")
    r.line(f"platform : {platform.platform()}")
    r.line(f"machine  : {platform.machine()}")


def packages_section(r: Report) -> None:
    r.section("Package versions")
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - py<3.8
        r.line("importlib.metadata unavailable")
        return
    for name in _PACKAGES:
        try:
            r.line(f"{name:<12}: {version(name)}")
        except PackageNotFoundError:
            r.line(f"{name:<12}: NOT INSTALLED")


def playwright_section(r: Report) -> None:
    r.section("Playwright browser")
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            exe = Path(pw.chromium.executable_path)
            r.line(f"chromium : {'present' if exe.exists() else 'MISSING'} ({exe})")
    except Exception as e:  # noqa: BLE001 - report whatever went wrong
        r.line(f"could not query Playwright: {type(e).__name__}: {e}")
        r.line("hint: run `playwright install chromium`")


def session_section(r: Report) -> None:
    r.section("Session state")
    r.line(f"profile dir : {'present' if PROFILE_DIR.is_dir() else 'MISSING'} ({PROFILE_DIR})")

    if not TOKEN_FILE.exists():
        r.line("token.json  : MISSING (not signed in yet — run `python -m copilot login`)")
        return

    import json

    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        r.line(f"token.json  : UNREADABLE ({type(e).__name__}: {e})")
        return

    saved_at = data.get("saved_at", 0) or 0
    age = time.time() - saved_at if saved_at else None
    token = data.get("access_token")
    left = (data.get("expires_at") or 0) - time.time()

    r.line("token.json  : present")
    r.line(f"  saved_at  : {age:.0f}s ago" if age is not None else "  saved_at  : unknown")
    # Report presence + length only — never the token itself.
    r.line(f"  access_token : {'yes (len ' + str(len(token)) + ')' if token else 'NO'}"
           + (f", expires in {left / 60:.0f} min" if token else ""))
    r.line(f"  refresh_token : {'yes' if data.get('refresh_token') else 'NO — re-run login'}")
    r.line(f"  account oid   : {'yes' if data.get('oid') else 'NO — re-run login'}")


def browser_capture(r: Report) -> None:
    """Open the real browser, sniff the chat socket (redacted) to ws_capture.log."""
    r.section("Live protocol capture (browser)")
    try:
        from playwright.sync_api import sync_playwright

        from copilot.auth import DEFAULT_PROFILE_DIR
    except Exception as e:  # noqa: BLE001
        r.line(f"skipped — Playwright unavailable: {type(e).__name__}: {e}")
        return

    summary = {"chat_open": False, "challenge": None, "append": False,
               "done": False, "frames": 0}
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    sink = WS_LOG.open("w", encoding="utf-8")

    def write(tag: str, payload) -> str:
        raw = _to_text(payload)
        if not sink.closed:  # frames can arrive after we stop; don't crash
            sink.write(redact(f"{tag} {raw}") + "\n")
            sink.flush()
        return raw

    print("\n" + "=" * 70)
    print("A browser is opening with your signed-in profile.")
    print("Type ONE short message into the Copilot UI and send it.")
    print("(If a 'verify you're human' check appears, pass it — that's the fix.)")
    print("Frames stream to session/ws_capture.log. CLOSE THE WINDOW when the")
    print("reply finishes (or after ~15s if it hangs). Auto-stops after 5 min.")
    print("=" * 70 + "\n")

    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(Path(DEFAULT_PROFILE_DIR).resolve()),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            def on_ws(ws) -> None:
                is_chat = CHAT_HINT in ws.url
                if is_chat:
                    summary["chat_open"] = True
                write("[OPEN]", f"{'CHAT' if is_chat else 'other'} {ws.url}")

                def on_recv(*a) -> None:
                    text = write("[RECV]", a[0] if a else b"")
                    summary["frames"] += 1
                    if '"writeAtCursor"' in text:
                        summary["append"] = True
                    if re.search(r'"type"\s*:\s*3\b', text):
                        summary["done"] = True

                ws.on("framesent", lambda *a: write("[SENT]", a[0] if a else b""))
                ws.on("framereceived", on_recv)
                ws.on("close", lambda *a: write("[CLOSE]", ws.url))

            page.on("websocket", on_ws)
            page.goto(COPILOT_URL, wait_until="domcontentloaded")

            ticks = _CAPTURE_TICKS
            try:
                while not page.is_closed() and ticks > 0:
                    page.wait_for_timeout(500)
                    ticks -= 1
            except Exception:
                pass  # window/context torn down by the close
            sink.close()
            try:
                ctx.close()
            except Exception:
                pass
    except Exception:  # noqa: BLE001 - capture failures shouldn't kill the report
        if not sink.closed:
            sink.close()
        r.line("capture failed (traceback redacted):")
        for tb in redact(traceback.format_exc()).splitlines():
            r.line(f"  {tb}")
        return

    # Summarise what we saw — this is the gold for captcha/protocol diagnosis.
    r.line(f"ws_capture.log written: {summary['frames']} frames (tokens redacted)")
    if summary["chat_open"]:
        states = [k for k in ("append", "done") if summary[k]]
        r.line(f"chat socket seen (saw: {', '.join(states) or 'connect only'})")
    else:
        r.line("no chat socket observed — was a message sent in the Copilot window?")


def live_probe_section(r: Report) -> None:
    r.section("Live chat probe (HTTP driver)")
    r.line("sending one short message (60s budget)...")
    started = time.time()
    try:
        from copilot import CopilotClient

        reply = CopilotClient().chat("Reply with exactly one word: pong", timeout=60)
        elapsed = time.time() - started
        snippet = (reply.text or "").strip().replace("\n", " ")[:80]
        r.line(f"RESULT   : OK in {elapsed:.1f}s")
        r.line(f"reply    : {snippet!r}")
        r.line(f"conv_id  : {'set' if reply.conversation_id else 'none'}")
    except Exception:  # noqa: BLE001 - the whole point is to capture the failure
        elapsed = time.time() - started
        r.line(f"RESULT   : FAILED after {elapsed:.1f}s")
        r.line("traceback (redacted):")
        for tb in redact(traceback.format_exc()).splitlines():
            r.line(f"  {tb}")


def log_tail_section(r: Report, title: str, path: Path) -> None:
    r.section(f"{title} (last {_LOG_TAIL_LINES} lines, redacted)")
    if not path.exists():
        r.line("(not present)")
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        r.line(f"(unreadable: {e})")
        return
    for raw in lines[-_LOG_TAIL_LINES:]:
        r.line(redact(raw))


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose/fix a Copilot session for bug reports.")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Skip the interactive browser capture (for headless/VPS); just write the report.",
    )
    args = parser.parse_args()

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    r = Report()
    r.line("Windows Copilot API — diagnostic report")
    r.line("(safe to share: secrets are redacted; skim before posting)")

    env_section(r)
    packages_section(r)
    playwright_section(r)
    session_section(r)

    if args.report_only:
        r.section("Live protocol capture (browser)")
        r.line("skipped (--report-only)")
    else:
        browser_capture(r)

    live_probe_section(r)
    log_tail_section(r, "login.log", LOGIN_LOG)
    log_tail_section(r, "ws_capture.log", WS_LOG)

    REPORT.write_text(r.text(), encoding="utf-8")
    print("\n" + "=" * 62)
    print(f"Report written to {REPORT}")
    print("Attach that file to your GitHub issue. Secrets are already redacted,")
    print("but give it a skim before posting.")


if __name__ == "__main__":
    main()
