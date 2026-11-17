"""Entry point kept for existing deployments: ``python main.py`` runs the bot.

Equivalent to ``python -m mathbot bot``. All logic lives in the ``mathbot`` package.
"""

import sys

from mathbot.cli import main

if __name__ == "__main__":
    sys.exit(main(["bot"]))
