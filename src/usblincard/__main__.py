"""Runs the ``usblincard`` command: ``python -m usblincard``."""

import sys

from usblincard.cli import main

if __name__ == "__main__":
    sys.exit(main())
