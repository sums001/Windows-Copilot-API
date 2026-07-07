"""CLI for enterprise Copilot Cowork.

    python -m copilot.m365.bootstrap    # one-time interactive sign-in (work account)
    python -m copilot.m365 "hello"      # ask, headless
"""

import sys

from .driver import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
