from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass


PLAY_REVIEW_USERNAME_ENV = "PROPERTYQUARRY_PLAY_REVIEW_USERNAME"
PLAY_REVIEW_PASSWORD_DIGEST_ENV = "PROPERTYQUARRY_PLAY_REVIEW_PASSWORD_DIGEST"
PLAY_REVIEW_PASSWORD_SCHEME = "pbkdf2_sha256"
PLAY_REVIEW_PASSWORD_ITERATIONS = 600_000
PLAY_REVIEW_MIN_PASSWORD_BYTES = 20
PLAY_REVIEW_MAX_PASSWORD_BYTES = 1_024
_PLAY_REVIEW_MAX_ITERATIONS = 1_200_000
_PLAY_REVIEW_USERNAME_RE = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9.-]{1,189}"
)


def _urlsafe_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _parse_password_digest(value: object) -> tuple[int, bytes, bytes] | None:
    parts = str(value or "").strip().split("$")
    if len(parts) != 4 or parts[0] != PLAY_REVIEW_PASSWORD_SCHEME:
        return None
    try:
        iterations = int(parts[1])
        salt = _urlsafe_b64decode(parts[2])
        expected = _urlsafe_b64decode(parts[3])
    except (TypeError, ValueError):
        return None
    if not PLAY_REVIEW_PASSWORD_ITERATIONS <= iterations <= _PLAY_REVIEW_MAX_ITERATIONS:
        return None
    if not 16 <= len(salt) <= 32 or len(expected) != 32:
        return None
    return iterations, salt, expected


def build_password_digest(
    password: str,
    *,
    salt: bytes | None = None,
    iterations: int = PLAY_REVIEW_PASSWORD_ITERATIONS,
) -> str:
    password_bytes = str(password or "").encode("utf-8")
    if not PLAY_REVIEW_MIN_PASSWORD_BYTES <= len(password_bytes) <= PLAY_REVIEW_MAX_PASSWORD_BYTES:
        raise ValueError("play_review_password_length_invalid")
    if not PLAY_REVIEW_PASSWORD_ITERATIONS <= int(iterations) <= _PLAY_REVIEW_MAX_ITERATIONS:
        raise ValueError("play_review_password_iterations_invalid")
    normalized_salt = bytes(salt if salt is not None else os.urandom(16))
    if not 16 <= len(normalized_salt) <= 32:
        raise ValueError("play_review_password_salt_invalid")
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password_bytes,
        normalized_salt,
        int(iterations),
        dklen=32,
    )
    return "$".join(
        (
            PLAY_REVIEW_PASSWORD_SCHEME,
            str(int(iterations)),
            _urlsafe_b64encode(normalized_salt),
            _urlsafe_b64encode(derived),
        )
    )


def password_matches(password: str, encoded_digest: str) -> bool:
    parsed = _parse_password_digest(encoded_digest)
    if parsed is None:
        return False
    password_bytes = str(password or "").encode("utf-8")
    if len(password_bytes) > PLAY_REVIEW_MAX_PASSWORD_BYTES:
        return False
    iterations, salt, expected = parsed
    observed = hashlib.pbkdf2_hmac(
        "sha256",
        password_bytes,
        salt,
        iterations,
        dklen=len(expected),
    )
    return hmac.compare_digest(observed, expected)


@dataclass(frozen=True)
class PlayReviewAccessConfig:
    username: str
    password_digest: str
    principal_id: str


def load_play_review_access_config() -> PlayReviewAccessConfig | None:
    username = str(os.getenv(PLAY_REVIEW_USERNAME_ENV) or "").strip().lower()
    password_digest = str(os.getenv(PLAY_REVIEW_PASSWORD_DIGEST_ENV) or "").strip()
    if not username and not password_digest:
        return None
    if (
        not username
        or len(username) > 254
        or _PLAY_REVIEW_USERNAME_RE.fullmatch(username) is None
        or _parse_password_digest(password_digest) is None
    ):
        return None
    principal_digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:20]
    return PlayReviewAccessConfig(
        username=username,
        password_digest=password_digest,
        principal_id=f"play-review-{principal_digest}",
    )


def credentials_match(
    config: PlayReviewAccessConfig,
    *,
    username: str,
    password: str,
) -> bool:
    normalized_username = str(username or "").strip().lower()
    username_matches = hmac.compare_digest(
        normalized_username.encode("utf-8"),
        config.username.encode("utf-8"),
    )
    password_is_valid = password_matches(password, config.password_digest)
    return bool(username_matches and password_is_valid)


class PlayReviewAttemptLimiter:
    def __init__(
        self,
        *,
        max_failures: int = 5,
        window_seconds: int = 15 * 60,
        max_keys: int = 2_048,
    ) -> None:
        self.max_failures = max(int(max_failures), 1)
        self.window_seconds = max(int(window_seconds), 1)
        self.max_keys = max(int(max_keys), 1)
        self._lock = threading.Lock()
        self._failures: OrderedDict[str, list[float]] = OrderedDict()

    def _active_failures(self, key: str, now: float) -> list[float]:
        cutoff = now - self.window_seconds
        return [value for value in self._failures.get(key, ()) if value > cutoff]

    def blocked(self, key: str, *, now: float | None = None) -> bool:
        observed_at = time.monotonic() if now is None else float(now)
        with self._lock:
            active = self._active_failures(key, observed_at)
            if active:
                self._failures[key] = active
                self._failures.move_to_end(key)
            else:
                self._failures.pop(key, None)
            return len(active) >= self.max_failures

    def record_failure(self, key: str, *, now: float | None = None) -> None:
        observed_at = time.monotonic() if now is None else float(now)
        with self._lock:
            active = self._active_failures(key, observed_at)
            active.append(observed_at)
            self._failures[key] = active
            self._failures.move_to_end(key)
            while len(self._failures) > self.max_keys:
                self._failures.popitem(last=False)

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


PLAY_REVIEW_ATTEMPT_LIMITER = PlayReviewAttemptLimiter()


def attempt_key(*, client_host: object, username: object) -> str:
    material = f"{str(client_host or '').strip().lower()}\n{str(username or '').strip().lower()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
