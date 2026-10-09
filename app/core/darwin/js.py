"""JavaScript value semantics the brand, admin and identity routes reproduce (beyond app/api/legacy.py).

Darwin's handlers lean on a few JavaScript conversions whose edge cases are part of the contract:

* `Number(x)` (`js_number`): `Number(null)` is 0, `Number("")` is 0, `Number(" 7 ")` is 7, `Number("0x10")`
  is 16, `Number("1e3")` is 1000, anything else is NaN. brand-asset reads `?index=` with it, so a missing
  index is layout 0 (contract api-brand-asset.json);
* `Number.isInteger(x)` (`js_is_integer`): true for 2 and 2.0, false for booleans, strings and NaN;
* `Buffer.from(s, "base64")` (`node_base64`): Node's lenient decoder. It skips characters outside the
  alphabet, accepts the URL-safe alphabet too, stops at the first "=", and drops a dangling 6-bit tail,
  so it never throws. brand-asset and brand-extract decode uploads with it;
* `String(n)` for a number (`js_number_text`), as a template literal writes it.
"""

from __future__ import annotations

import base64
import math
import re
from typing import Any

#: `Number(undefined)`: NaN. Pass it where Darwin read a missing property.
UNDEFINED: Any = object()

_DECIMAL = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_PREFIXED = re.compile(r"^0([xXoObB])([0-9a-fA-F]+)$")
_BASES = {"x": 16, "o": 8, "b": 2}
# The characters JavaScript's String.prototype.trim (and so Number()) strips.
_JS_SPACE = " \t\n\v\f\r             " \
            "    　﻿"
_B64_ALPHABET = re.compile(r"[^A-Za-z0-9+/\-_=]")


def js_number(value: Any) -> float:
    """`Number(value)`. NaN is `math.nan`."""
    if value is UNDEFINED:
        return math.nan
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip(_JS_SPACE)
        if not text:
            return 0.0
        if text in ("Infinity", "+Infinity"):
            return math.inf
        if text == "-Infinity":
            return -math.inf
        if _DECIMAL.match(text):
            return float(text)
        prefixed = _PREFIXED.match(text)
        if prefixed:
            base = _BASES[prefixed.group(1).lower()]
            try:
                return float(int(prefixed.group(2), base))
            except ValueError:
                return math.nan
        return math.nan
    if isinstance(value, list):  # [] -> "" -> 0; [x] -> Number(String(x)); longer -> "a,b" -> NaN
        if not value:
            return 0.0
        if len(value) == 1:
            item = value[0]
            if item is None or isinstance(item, (str, list)):
                return js_number("" if item is None else item)
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                return float(item)
        return math.nan
    return math.nan


def js_is_integer(value: Any) -> bool:
    """`Number.isInteger(value)`: a number (not a boolean) that is finite and whole."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and float(value).is_integer()


def js_number_text(value: float) -> str:
    """`String(n)` for a whole number below 1e21, as a template literal writes it (`-0` is "0")."""
    return str(int(value)) if value.is_integer() and abs(value) < 1e21 else repr(value)


def node_base64(text: str) -> bytes:
    """`Buffer.from(text, "base64")`: never raises; see the module docstring."""
    cleaned = _B64_ALPHABET.sub("", text).replace("-", "+").replace("_", "/")
    cleaned = cleaned.split("=", 1)[0]
    if len(cleaned) % 4 == 1:
        cleaned = cleaned[:-1]
    if not cleaned:
        return b""
    return base64.b64decode(cleaned + "=" * (-len(cleaned) % 4))
