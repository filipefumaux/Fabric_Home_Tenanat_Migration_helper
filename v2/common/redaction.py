"""Credential redaction for diagnostic output, never for API or backup payloads."""
import re
from collections.abc import Mapping
from typing import TypeVar, overload
from urllib.parse import quote

REDACTED = "<redacted>"
if "_REDACTION_VALUES" not in globals():
    _REDACTION_VALUES: set[str] = set()

_REDACTION_KEYS = {
    "authorization", "accesstoken", "refreshtoken", "clientsecret", "serviceprincipalsecret",
    "password", "passwd", "accountkey", "apikey", "personalaccesstoken", "pat", "secret", "token",
    "connectionstring", "sastoken", "sharedaccesssignature", "sig",
}
_REDACTION_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
               r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.DOTALL),
)
_REDACTION_AUTH = re.compile(r"\b(Bearer|Basic)([ \t]+)[A-Za-z0-9_+/=.-]{16,}", re.IGNORECASE)
_REDACTION_QUERY = re.compile(
    r"([?&](?:sig|access_token|refresh_token|client_secret|password|api_key)=)[^&\s\"'<>]+",
    re.IGNORECASE,
)
_REDACTION_ASSIGNMENT = re.compile(
    r"""(\b(?:client[_-]?secret|servicePrincipalSecret|access[_-]?token|refresh[_-]?token|
    password|passwd|account[_-]?key|api[_-]?key|personalAccessToken|sas[_-]?token|pat|secret|token)
    \b["']?\s*[:=]\s*["']?)([^\s"'&,;}\]<>]+)""",
    re.IGNORECASE | re.VERBOSE,
)


def register_sensitive_value(value: str) -> None:
    if len(value) >= 8:
        _REDACTION_VALUES.add(value)


def redact_sensitive_text(text: str) -> str:
    for value in sorted(_REDACTION_VALUES.copy(), key=len, reverse=True):
        text = text.replace(value, REDACTED).replace(quote(value, safe=""), REDACTED)
    for pattern in _REDACTION_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _REDACTION_AUTH.sub(lambda m: m[1] + m[2] + REDACTED, text)
    text = _REDACTION_QUERY.sub(lambda m: m[1] + REDACTED, text)
    return _REDACTION_ASSIGNMENT.sub(lambda m: m[1] + REDACTED, text)


_Key = TypeVar("_Key")


@overload
def redact_sensitive_data(value: str) -> str: ...


@overload
def redact_sensitive_data(value: Mapping[_Key, object]) -> dict[_Key, object]: ...


@overload
def redact_sensitive_data(value: list[object]) -> list[object]: ...


@overload
def redact_sensitive_data(value: tuple[object, ...]) -> tuple[object, ...]: ...


@overload
def redact_sensitive_data(value: object) -> object: ...


def redact_sensitive_data(value: object) -> object:
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in _REDACTION_KEYS and item is not None and not (isinstance(item, str) and item == ""):
                out[key] = REDACTED
            else:
                out[key] = redact_sensitive_data(item)
        return out
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item) for item in value)
    return redact_sensitive_text(value) if isinstance(value, str) else value
