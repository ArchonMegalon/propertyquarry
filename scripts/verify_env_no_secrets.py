#!/usr/bin/env python3
"""Validate tracked EA env templates do not contain real secrets."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

ALLOWED_PLACEHOLDERS = {
    "",
    "CHANGE_ME_STRONG",
    "local_dev_only",
    "replace-with-a-strong-password",
    "replace-with-a-strong-shared-secret",
    "replace-with-a-newly-issued-key",
}

SUSPICIOUS_KEY_RE = re.compile(
    r"(?:^|_)(?:TOKEN|API_KEY|CLIENT_ID|CLIENT_SECRET|SECRET|PASSWORD|USERNAME|LOGIN_EMAIL)(?:_|$)",
)


def is_tracked_env_template(path_text: str) -> bool:
    name = Path(path_text).name
    return bool(name.startswith(".env") and name != ".env")


def tracked_env_template_paths() -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=False,
        )
        tracked_paths = [
            ROOT / Path(path_text)
            for path_text in result.stdout.decode("utf-8").split("\0")
            if path_text and is_tracked_env_template(path_text)
        ]
        if tracked_paths:
            return sorted(set(tracked_paths))
    except Exception:
        pass
    return sorted(path for path in ROOT.glob(".env*") if path.is_file() and is_tracked_env_template(path.name))


def normalized_value(raw_value: str) -> str:
    value = raw_value.strip()
    if (
        (value.startswith('"') and value.endswith('"'))
        or (value.startswith("'") and value.endswith("'"))
        or (value.startswith("`") and value.endswith("`"))
    ):
        value = value[1:-1].strip()
    return value


def is_allowed_value(value: str) -> bool:
    if value in ALLOWED_PLACEHOLDERS:
        return True
    if value.startswith("replace-with-"):
        return True
    if re.fullmatch(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", value):
        return True
    return False


def extract_env_pairs(path: Path):
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        yield line_number, key.strip(), value.strip()


def main() -> int:
    failed = False
    for path in tracked_env_template_paths():
        if not path.exists():
            continue
        for line_number, key, value in extract_env_pairs(path):
            if not SUSPICIOUS_KEY_RE.search(key):
                continue
            value = normalized_value(value)
            if not is_allowed_value(value):
                print(
                    f"{path}: line {line_number}: suspicious non-placeholder env value in {key}"
                )
                failed = True
    if failed:
        print("env secret guard failed: replace concrete values with placeholders.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
