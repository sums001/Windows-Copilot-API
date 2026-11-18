"""Server configuration — shared constants."""

import os

# Public OpenAI model ids mapped to Copilot's `tone` request field.
MODEL_NAME = "copilot-reasoning"
MODEL_TONES = {
    "copilot": "Magic",
    "copilot-quick": "Chat",
    "copilot-reasoning": "Reasoning",
}

# Self-imposed rate limit (Copilot publishes none). Tune to whatever ceiling the
# probe in tests/ratelimit.py shows your account tolerates.
#   RATE_LIMIT_RPM   requests/minute the bridge will accept; 0 disables limiting.
#   RATE_LIMIT_BURST max requests allowed back-to-back before pacing kicks in.
# Default 12 rpm sits safely below the ~15 rpm where one account starts seeing
# upstream 502s, so the limiter only bites when callers try to exceed that.
RATE_LIMIT_RPM = float(os.environ.get("RATE_LIMIT_RPM", "12"))  # 12 rpm ≈ 5s per call
RATE_LIMIT_BURST = int(os.environ.get("RATE_LIMIT_BURST", "4"))
