"""Shared Microsoft Copilot chat-protocol constants — the single source of truth.

Both the pure-HTTP driver (:mod:`copilot.driver`) and the browser driver
(:mod:`copilot.browser`) speak the *same* chat-socket protocol, so the wire
shapes live here once. When Microsoft changes the protocol, recapture it with
``tests/diagnostic.py`` (its browser capture writes ``session/ws_capture.log``)
and update this file — both drivers follow.

Captured from a live copilot.microsoft.com session. The connect sequence is:

    ws_connect(CHAT_WEBSOCKET_URL + &clientSessionId=<uuid> [+ &accessToken=<tok>])
    -> send SET_OPTIONS_FRAME
    -> send CONSENTS_FRAME
    -> send {"event":"send", ..., "mode":"smart", "context":{}}
    -> receive appendText* then done

A `send` issued *before* the setOptions/consents handshake is rejected by the
backend with ``error: invalid-event``.
"""

# Base chat socket; callers append &clientSessionId=<uuid> and, when signed in,
# &accessToken=<token>.
CHAT_WEBSOCKET_URL = "wss://copilot.microsoft.com/c/api/chat?api-version=2"

# First handshake frame: advertise the features/cards/UI components the client
# supports. The lists only describe what a UI *could* render; a text prompt still
# streams back as plain `appendText`, so they're harmless for this bridge.
SET_OPTIONS_FRAME = {
    "event": "setOptions",
    "supportedFeatures": [
        "partial-generated-images",
        "composer-prefill-conversation-action",
        "composer-send-conversation-action-v2",
        "side-by-side-comparison",
        "session-duration-nudge",
        "compose-email-html",
    ],
    "supportedCards": [
        "weather", "local", "image", "sports", "video", "healthcareEntity",
        "healthcareInfo", "healthRecordsConnectNewProvider", "healthRecordsUpdate",
        "suggestHealth", "chart", "ads", "safetyHelpline", "quiz", "finance",
        "recipe", "personalArtifacts", "flashcard", "navigation", "person",
        "powerPointCreator", "consentV2", "composeEmail", "createCalendarEvent",
        "modifyCalendarEvent", "deleteCalendarEvent", "practiceTest", "tapToReveal",
    ],
    "supportedUIComponents": {
        "Badge": "1.2", "Basic": "1.2", "Box": "1.2", "Button": "1.2",
        "Card": "1.2", "Caption": "1.2", "Chart": "1.2", "Checkbox": "1.2",
        "Col": "1.2", "DatePicker": "1.3", "Divider": "1.2", "Form": "1.2",
        "Icon": "1.2", "Image": "1.2", "Label": "1.2", "ListView": "1.2",
        "ListViewItem": "1.2", "Map": "1.3", "Markdown": "1.2", "Pressable": "1.3",
        "RadioGroup": "1.3", "Row": "1.2", "Select": "1.3", "Spacer": "1.2",
        "Table": "1.3", "Table.Cell": "1.3", "Table.Row": "1.3", "Text": "1.2",
        "Textarea": "1.3", "Title": "1.2", "Transition": "1.2",
    },
    "ads": {"supportedTypes": ["text", "product", "multimedia", "tourActivity", "propertyPromotion"]},
    "supportedActions": [],
}

# Second handshake frame: declare no locally-granted consents.
CONSENTS_FRAME = {"event": "reportLocalConsents", "grantedConsents": []}
