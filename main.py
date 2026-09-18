"""Entrypoint used by Render (``startCommand: python main.py``).

    python main.py              # run the bot
    python main.py --selftest    # check config/UI wiring without connecting
"""

import sys

from formbot.main import debug_selftest, main

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        debug_selftest()
    else:
        main()
