"""High-level Copilot client — the recommended entry point.

One client, many conversations addressed by id. :meth:`CopilotClient.chat`
returns the full reply plus the conversation id; pass that id back to continue
the same conversation, or omit it to start a fresh one. :meth:`CopilotClient.stream`
is the incremental variant.

    from copilot import CopilotClient

    client = CopilotClient()                       # loads signed-in auth once
    r = client.chat("My name is Tomato. Remember it.")
    print(r.text, r.conversation_id)

    r2 = client.chat("What's my name?", r.conversation_id)   # continue
    print(r2.text)

    for chunk in client.stream("Tell me a joke"):  # new conversation, streamed
        print(chunk, end="", flush=True)

The signed-in access token is refreshed transparently over HTTP; sign in once
with ``python -m copilot login``. Pass ``proxy=...`` to route requests through a
proxy.
"""

import sys
import time
from dataclasses import dataclass, field
from typing import Generator, List, Optional, Union

from .auth import AUTH_MAX_AGE, load_auth
from .driver import AuthExpired, Copilot
from .models import Conversation, ImageResponse


def _status(msg: str) -> None:
    """Emit a recovery/progress line to stderr.

    Goes to stderr (not stdout) so it never mixes into the reply text the CLI and
    callers read off stdout. Plain ``print`` keeps it visible by default — this is
    a personal bridge, not a library that should stay silent."""
    print(f"[copilot] {msg}", file=sys.stderr, flush=True)


@dataclass
class ChatReply:
    """The full result of a :meth:`CopilotClient.chat` call."""

    text: str
    conversation_id: Optional[str]
    images: List[ImageResponse] = field(default_factory=list)


class ChatStream:
    """Iterable stream of reply chunks that also exposes the conversation id.

    Yields ``str`` text chunks (and :class:`~copilot.models.ImageResponse` for
    generated images). ``conversation_id`` is known up front when continuing an
    existing conversation, and is populated as soon as iteration begins when a
    new conversation is created.
    """

    def __init__(self, chunks: Generator, conversation_id: Optional[str]):
        self._chunks = chunks
        self.conversation_id = conversation_id

    def __iter__(self) -> Generator[Union[str, ImageResponse], None, None]:
        for item in self._chunks:
            if isinstance(item, Conversation):
                self.conversation_id = item.conversation_id
            else:
                yield item


class CopilotClient:
    """A Copilot client: one object, many conversations addressed by id.

    Parameters
    ----------
    anonymous:
        Not supported: copilot.com only chats when signed in. Kept so existing
        callers passing ``anonymous=False`` keep working.
    proxy:
        Optional ``scheme://user:pass@host:port`` proxy, applied to both the
        auth refresh and every request.
    max_age:
        Seconds a cached access token is trusted before it is refreshed.
    interactive_clear:
        Allow opening a visible sign-in window when no usable session exists.
        Default ``True``. Set ``False`` for headless/server use, where a
        ``RuntimeError`` is raised instead of popping a window.
    headless_clear:
        No effect; kept for compatibility with the previous version.
    """

    def __init__(
        self,
        anonymous: bool = False,
        proxy: Optional[str] = None,
        max_age: int = AUTH_MAX_AGE,
        interactive_clear: bool = True,
        headless_clear: bool = False,
    ):
        if anonymous:
            raise ValueError("copilot.com requires sign-in; anonymous chat isn't available.")
        self._driver = Copilot()
        self._proxy = proxy
        self._max_age = max_age
        self._interactive_clear = interactive_clear
        self._auth: Optional[dict] = None

    def stream(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        **kwargs,
    ) -> ChatStream:
        """Stream the reply to ``prompt`` as a :class:`ChatStream`.

        Starts a new conversation when ``conversation_id`` is ``None``; otherwise
        continues that conversation. Read ``.conversation_id`` on the returned
        stream (during/after iteration) to continue the chat later.

        If the access token turns out to be rejected, it is refreshed and the
        turn retried once. Recovery only happens before any text is emitted, so
        output is never duplicated.
        """
        return ChatStream(
            self._stream_with_recovery(prompt, conversation_id, kwargs),
            conversation_id,
        )

    def _stream_with_recovery(self, prompt, conversation_id, kwargs):
        """Drive the turn; on a rejected token, refresh it and retry once."""
        for attempt in range(2):
            auth = self._fresh_auth(force=attempt > 0)
            kw = dict(
                stream=True,
                proxy=self._proxy,
                access_token=auth["access_token"],
                oid=auth["oid"],
                **kwargs,
            )
            if conversation_id is None:
                kw["return_conversation"] = True  # have the driver hand back its id
            else:
                kw["conversation_id"] = conversation_id

            produced = False  # any user-visible output yet? (Conversation doesn't count)
            try:
                for item in self._driver.create_completion(prompt, **kw):
                    if not isinstance(item, Conversation):
                        produced = True
                    yield item
                return
            except AuthExpired:
                if produced or attempt:
                    raise
                _status("Access token rejected — refreshing it and retrying...")

    def chat(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        **kwargs,
    ) -> ChatReply:
        """Return the full reply to ``prompt`` as a :class:`ChatReply`.

        Buffers the whole response; use :meth:`stream` for incremental output.
        """
        s = self.stream(prompt, conversation_id=conversation_id, **kwargs)
        text: List[str] = []
        images: List[ImageResponse] = []
        for item in s:
            if isinstance(item, str):
                text.append(item)
            elif isinstance(item, ImageResponse):
                images.append(item)
        return ChatReply("".join(text), s.conversation_id, images)

    def _fresh_auth(self, force: bool = False) -> dict:
        """Return current signed-in auth, refreshing it when stale or forced."""
        now = time.time()
        if (force or self._auth is None
                or now - self._auth.get("saved_at", 0) >= self._max_age
                or now >= self._auth.get("expires_at", 0) - 60):
            self._auth = load_auth(max_age=self._max_age, proxy=self._proxy,
                                   auto_login=self._interactive_clear, force=force)
        return self._auth
