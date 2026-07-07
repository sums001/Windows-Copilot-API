"""Enterprise Microsoft 365 Copilot ("Copilot Cowork") support.

A work/school (Entra ID) account is not served by consumer copilot.microsoft.com
— Microsoft redirects it to the enterprise experience at m365.cloud.microsoft,
which runs on **Teams messaging infrastructure**, not the consumer chat socket:

  * the prompt is sent into a Teams *thread* (``19:…@thread``, a cowork chat);
  * the streamed reply is pushed back over **Trouter** — Teams' Socket.IO push
    channel on ``*.trouter.teams.microsoft.com`` — as a tunnelled message the
    client must ACK, carrying ``copilotTextSegments`` and a terminal
    ``copilotTaskState:"completed"``;
  * auth is Bearer tokens for ``substrate.office.com`` and ``ic3.teams.office.com``
    (the Trouter/Skype token), minted server-side (never in localStorage).

So this is a small headless Teams-messaging client, captured with
``tests/capture_m365.py``. Layers:

  * :mod:`copilot.m365.trouter`  — Socket.IO 0.9 frame codec (pure).
  * :mod:`copilot.m365.protocol` — parse a Trouter delivery into a reply event,
    build the delivery ACK (pure).
  * auth / send / driver land here as the capture pins down their endpoints.

The consumer path (:mod:`copilot.driver`) is unaffected; account-type routing
picks between them.

    from copilot.m365 import M365Copilot
    print(M365Copilot().ask("hello"))     # after `python -m copilot.m365.bootstrap`
"""

from .driver import M365Copilot, new_conversation_id

__all__ = ["M365Copilot", "new_conversation_id"]
