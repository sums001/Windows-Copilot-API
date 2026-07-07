"""Server configuration — shared constants."""

import os

# The consumer (personal-account) model id this bridge advertises.
MODEL_NAME = "copilot"
# The enterprise (work/school account) model id — routed to the Microsoft 365
# Copilot "Cowork" driver (copilot.m365). Requires a one-time bootstrap:
# `python -m copilot.m365.bootstrap`. Select it from clients via this model name.
WORK_MODEL_NAME = os.environ.get("WORK_MODEL_NAME", "copilot-work")

# Self-imposed rate limit (Copilot publishes none). Tune to whatever ceiling the
# probe in tests/ratelimit.py shows your account tolerates.
#   RATE_LIMIT_RPM   requests/minute the bridge will accept; 0 disables limiting.
#   RATE_LIMIT_BURST max requests allowed back-to-back before pacing kicks in.
# Default 12 rpm sits safely below the ~15 rpm where one account starts seeing
# upstream 502s, so the limiter only bites when callers try to exceed that.
RATE_LIMIT_RPM = float(os.environ.get("RATE_LIMIT_RPM", "12"))  # 12 rpm ≈ 5s per call
RATE_LIMIT_BURST = int(os.environ.get("RATE_LIMIT_BURST", "4"))

# Ceiling on the flattened prompt sent upstream, in characters. The bridge
# resends the whole conversation each turn, so long agent sessions eventually
# exceed what Copilot accepts; past this cap the oldest turns are dropped
# (system + tool instructions + newest turns always survive). 0 disables.
MAX_PROMPT_CHARS = int(os.environ.get("MAX_PROMPT_CHARS", "120000"))
