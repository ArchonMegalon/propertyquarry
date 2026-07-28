#!/usr/bin/env python3
from __future__ import annotations

import argparse
import compileall
import sys
import tempfile
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATHS = (
    ROOT / "ea" / "app",
    ROOT / "tests",
)


def compile_paths(paths: Sequence[Path]) -> bool:
    previous_prefix = sys.pycache_prefix
    try:
        with tempfile.TemporaryDirectory(
            prefix="propertyquarry-compileall-",
            dir="/tmp",
        ) as cache_root:
            sys.pycache_prefix = cache_root
            return all(
                compileall.compile_dir(
                    path,
                    quiet=1,
                )
                for path in paths
            )
    finally:
        sys.pycache_prefix = previous_prefix


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compile PropertyQuarry Python sources while keeping bytecode "
            "outside the repository."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Directories to compile (defaults to ea/app and tests).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = tuple(path.resolve() for path in args.paths) or DEFAULT_PATHS
    return 0 if compile_paths(paths) else 1


if __name__ == "__main__":
    raise SystemExit(main())
