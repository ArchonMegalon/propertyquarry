#!/usr/local/bin/python
"""Minimal signal-transparent entrypoint for the PropertyQuarry web image."""

from __future__ import annotations

import os
import sys


def main() -> None:
    command = sys.argv[1:]
    if not command:
        raise SystemExit("property_web_entrypoint_command_required")
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
