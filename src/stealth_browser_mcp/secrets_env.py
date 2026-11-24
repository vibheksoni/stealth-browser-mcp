"""Environment-based secret resolution and payload redaction."""

import os
import re
import threading
from typing import Any, Iterable, List, Optional, Set, Union

SECRET_ENV_PREFIX = "STEALTH_SECRET_"
REDACTED_PLACEHOLDER = "[REDACTED]"


class SecretEnvError(ValueError):
    """Raised when a secret environment variable name is invalid or missing."""
    pass


class Redactor:
    """Registry of secret values with recursive redaction support."""

    def __init__(self, placeholder: str = REDACTED_PLACEHOLDER):
        self.placeholder = placeholder
        self._secrets: Set[str] = set()
        self._lock = threading.Lock()
        self._cached_pattern: Optional[re.Pattern] = None

    def register(self, secret: Union[str, Iterable[str]]) -> None:
        """Register one or more secret values to be redacted.

        Empty strings and non-string items are ignored to avoid unintentional
        over-redaction.
        """
        with self._lock:
            added = False
            if isinstance(secret, str):
                if secret and secret not in self._secrets:
                    self._secrets.add(secret)
                    added = True
            elif hasattr(secret, "__iter__"):
                for s in secret:
                    if isinstance(s, str) and s and s not in self._secrets:
                        self._secrets.add(s)
                        added = True
            if added:
                self._cached_pattern = None

    def clear(self) -> None:
        """Clear all registered secrets."""
        with self._lock:
            self._secrets.clear()
            self._cached_pattern = None

    @property
    def secrets(self) -> Set[str]:
        """Return a copy of currently registered secrets."""
        with self._lock:
            return set(self._secrets)

    def __contains__(self, secret: str) -> bool:
        with self._lock:
            return secret in self._secrets

    def __len__(self) -> int:
        with self._lock:
            return len(self._secrets)

    def _get_pattern(self) -> Optional[re.Pattern]:
        with self._lock:
            if not self._secrets:
                return None
            if self._cached_pattern is None:
                sorted_secrets = sorted(self._secrets, key=len, reverse=True)
                pattern_str = "|".join(re.escape(s) for s in sorted_secrets)
                self._cached_pattern = re.compile(pattern_str)
            return self._cached_pattern

    def redact(self, obj: Any) -> Any:
        """Recursively replace occurrences of registered secrets in obj with placeholder.

        Handles strings, nested dicts, lists, tuples, and sets.
        Other objects are returned unchanged.
        """
        pattern = self._get_pattern()
        if pattern is None:
            return obj
        return self._redact_value(obj, pattern, None)

    def _redact_value(
        self,
        obj: Any,
        pattern: re.Pattern,
        seen: Optional[Set[int]],
    ) -> Any:
        if isinstance(obj, str):
            return pattern.sub(self.placeholder, obj)

        if isinstance(obj, dict):
            obj_id = id(obj)
            if seen is None:
                seen = set()
            elif obj_id in seen:
                return self.placeholder
            seen.add(obj_id)
            try:
                return {
                    self._redact_value(k, pattern, seen): self._redact_value(v, pattern, seen)
                    for k, v in obj.items()
                }
            finally:
                seen.remove(obj_id)

        if isinstance(obj, list):
            obj_id = id(obj)
            if seen is None:
                seen = set()
            elif obj_id in seen:
                return self.placeholder
            seen.add(obj_id)
            try:
                return [self._redact_value(item, pattern, seen) for item in obj]
            finally:
                seen.remove(obj_id)

        if isinstance(obj, tuple):
            obj_id = id(obj)
            if seen is None:
                seen = set()
            elif obj_id in seen:
                return self.placeholder
            seen.add(obj_id)
            try:
                return tuple(self._redact_value(item, pattern, seen) for item in obj)
            finally:
                seen.remove(obj_id)

        if isinstance(obj, set):
            obj_id = id(obj)
            if seen is None:
                seen = set()
            elif obj_id in seen:
                return self.placeholder
            seen.add(obj_id)
            try:
                return {self._redact_value(item, pattern, seen) for item in obj}
            finally:
                seen.remove(obj_id)

        return obj


default_redactor = Redactor()
redactor = default_redactor


def resolve_value_env(name: str, redactor: Optional[Redactor] = None) -> str:
    """Resolve an environment variable value, enforcing the STEALTH_SECRET_ prefix.

    The resolved secret value is automatically registered with the redactor
    so that it is masked in debug logs, payloads, and exports.

    Args:
        name: The name of the environment variable (must start with STEALTH_SECRET_).
        redactor: Optional Redactor instance to register the secret into.
            Defaults to default_redactor.

    Returns:
        str: The secret value from os.environ.

    Raises:
        SecretEnvError: If name does not start with STEALTH_SECRET_ or if the
            variable is not set in os.environ.
    """
    if not isinstance(name, str) or not name.startswith(SECRET_ENV_PREFIX):
        raise SecretEnvError(
            f"Secret environment variable '{name}' must start with '{SECRET_ENV_PREFIX}'"
        )
    if name not in os.environ:
        raise SecretEnvError(
            f"Secret environment variable '{name}' is not set"
        )
    value = os.environ[name]
    target_redactor = redactor if redactor is not None else default_redactor
    target_redactor.register(value)
    return value


def redact(obj: Any) -> Any:
    """Redact secrets using the default redactor."""
    return default_redactor.redact(obj)


def register_secret(secret: Union[str, Iterable[str]]) -> None:
    """Register a secret with the default redactor."""
    default_redactor.register(secret)


def clear_secrets() -> None:
    """Clear all secrets registered in the default redactor."""
    default_redactor.clear()


__all__ = [
    "SECRET_ENV_PREFIX",
    "REDACTED_PLACEHOLDER",
    "SecretEnvError",
    "Redactor",
    "default_redactor",
    "redactor",
    "resolve_value_env",
    "redact",
    "register_secret",
    "clear_secrets",
]
