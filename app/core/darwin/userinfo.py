"""`/api/userinfo`: Darwin's OIDC userinfo shim for Supabase Auth's `custom:auxi` provider (C8).

Kept exactly as `netlify/functions/userinfo.ts` behaves, because Supabase Auth calls it server-side and
reads `email` and `sub` from it:

* the token is NOT verified (no signature, issuer, audience or expiry check): the payload segment is
  base64-decoded with JavaScript's `atob` (a Latin-1 string, so non-ASCII names come out as mojibake,
  e.g. "José" -> "JosÃ©") and parsed as JSON;
* `email` is `emails[0] || email || upn` (B2C, OIDC, Entra), `sub` is `oid || sub`; claims that are
  absent are left out of the body (JSON.stringify drops undefined), a null claim stays null;
* two 401 bodies: `{"error": "Unauthorized"}` (no token) and `{"error": "Invalid token", "detail":
  String(e)}` (the payload does not decode); `detail` reproduces the JavaScript exception text for
  the common cases (Node 24 wording);
* errors carry `text/plain;charset=UTF-8`, the Fetch default for `new Response(string)`: Darwin set no
  content-type on them.

It is trusted only because Supabase hands it a token the identity provider just issued; anything else
that calls it learns nothing a decoded JWT would not tell it. Plan D25: kept only while the identity
flow uses it.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any

_BEARER_PREFIX = re.compile(r"^Bearer\s+", re.IGNORECASE)
# `atob`'s forgiving-base64 alphabet: ASCII whitespace, then the base64 characters and '='.
_WHITESPACE = "\t\n\f\r "
_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")

_UNDEFINED: Any = object()


class JsError(Exception):
    """A JavaScript exception, carried as `String(e)` ("<Name>: <message>")."""


@dataclass(frozen=True)
class UserinfoAnswer:
    status: int
    body: dict[str, Any]


def atob(data: str) -> str:
    """Node's `atob` (lib/buffer.js): forgiving base64 to a Latin-1 string, with its two errors."""
    significant = 0
    equals = 0
    for ch in data:
        if ch in _WHITESPACE:
            continue
        if ch == "=":
            significant += 1
            equals += 1
            if equals > 2:
                raise JsError("InvalidCharacterError: Invalid character")
            continue
        if ch not in _ALPHABET:
            raise JsError("InvalidCharacterError: Invalid character")
        if equals:
            raise JsError("InvalidCharacterError: Invalid character")  # '=' only at the end
        significant += 1
    remainder = significant % 4
    if not remainder:
        remainder = (significant - equals) % 4
    elif equals:
        raise JsError("InvalidCharacterError: Invalid character")
    if remainder == 1:
        raise JsError("InvalidCharacterError: The string to be decoded is not correctly encoded.")
    cleaned = "".join(ch for ch in data if ch not in _WHITESPACE).rstrip("=")
    return base64.b64decode(cleaned + "=" * (-len(cleaned) % 4)).decode("latin-1")


def _reject_constant(name: str) -> Any:
    raise ValueError(name)


def json_parse(text: str) -> Any:
    """`JSON.parse`, with V8's message for the two common failures."""
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        if not text.strip():
            raise JsError("SyntaxError: Unexpected end of JSON input") from exc
        shown = text if len(text) <= 10 else text[:10] + "..."
        position = getattr(exc, "pos", 0) or 0
        token = text[position] if position < len(text) else ""
        if not token:
            raise JsError("SyntaxError: Unexpected end of JSON input") from exc
        raise JsError(f"SyntaxError: Unexpected token '{token}', \"{shown}\" is not valid JSON") from exc


def _get(value: Any, key: str) -> Any:
    """`value.key` / `value[key]` on a parsed JSON value (`_UNDEFINED` when there is no such property)."""
    if isinstance(value, dict):
        return value.get(key, _UNDEFINED)
    if isinstance(value, (list, str)) and key.isdigit():
        index = int(key)
        return value[index] if index < len(value) else _UNDEFINED
    return _UNDEFINED


def _truthy(value: Any) -> bool:
    if value is _UNDEFINED or value is None or value is False:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value != ""
    return True


def _or(*values: Any) -> Any:
    """`a || b || c`: the first truthy operand, else the last one."""
    for value in values[:-1]:
        if _truthy(value):
            return value
    return values[-1]


def answer(authorization: str | None) -> UserinfoAnswer:
    """The status and body `/api/userinfo` answers for this Authorization header."""
    token = _BEARER_PREFIX.sub("", authorization or "", count=1)
    if not token:
        return UserinfoAnswer(401, {"error": "Unauthorized"})
    try:
        parts = token.split(".")
        if len(parts) < 2:
            raise JsError("TypeError: Cannot read properties of undefined (reading 'replace')")
        payload = json_parse(atob(parts[1].replace("-", "+").replace("_", "/")))
        if payload is None:
            raise JsError("TypeError: Cannot read properties of null (reading 'emails')")
        emails = _get(payload, "emails")
        first = _UNDEFINED if emails is _UNDEFINED or emails is None else _get(emails, "0")
        email = _or(first, _get(payload, "email"), _get(payload, "upn"))
        if not _truthy(email):
            return UserinfoAnswer(400, {"error": "No email in token"})
        body = {"sub": _or(_get(payload, "oid"), _get(payload, "sub")), "email": email,
                "name": _get(payload, "name"), "given_name": _get(payload, "given_name"),
                "family_name": _get(payload, "family_name")}
        return UserinfoAnswer(200, {k: v for k, v in body.items() if v is not _UNDEFINED})
    except JsError as exc:
        return UserinfoAnswer(401, {"error": "Invalid token", "detail": str(exc)})
