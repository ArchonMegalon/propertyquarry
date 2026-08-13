#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "ea"))

from app.services.propertyquarry_play_review_access import build_password_digest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a PropertyQuarry Google Play reviewer password digest without echoing the password.",
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read one password line from stdin instead of a no-echo terminal prompt.",
    )
    args = parser.parse_args()
    password = (
        sys.stdin.readline().rstrip("\r\n")
        if args.password_stdin
        else getpass.getpass("Reviewer password: ")
    )
    try:
        digest = build_password_digest(password)
    except ValueError as exc:
        parser.error(str(exc))
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
