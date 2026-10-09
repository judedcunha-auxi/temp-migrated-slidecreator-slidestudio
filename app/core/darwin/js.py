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

The generation routes and Darwin's image prompt add (merged from feature/darwin-generation):

* `js_round(x)`: `Math.round`, which rounds halves up (Python's `round` rounds them to even);
* `js_str(x)`: `String(n)` / `${n}` for any number (12.0 is "12", 1e21 is "1e+21", 0.00001 is
  "0.00001"), and true/false/null as JavaScript spells them;
* `js_trim(s)` and `js_length(s)`: `String.prototype.trim` and `.length` (UTF-16 code units, so an emoji
  counts 2); `is_js_integer` is `js_is_integer`.
"""

from __future__ import annotations

import base64
import math
import re
from decimal import Decimal
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


# --- the generation batch: Math.round, String(n), trim, length ---------------------------------
is_js_integer = js_is_integer


def js_trim(text: str) -> str:
    return text.strip(_JS_SPACE)


def js_length(text: str) -> int:
    """`text.length`: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def js_round(x: float) -> int:
    """`Math.round(x)`: the nearest integer, halves toward +infinity."""
    return math.floor(x + 0.5)


def js_str(value: Any) -> str:
    """`String(value)` for what a prompt interpolates: numbers as JavaScript prints them, strings as they
    are, true/false/null as JavaScript spells them."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return js_str(float(value)) if abs(value) >= 10**21 else str(value)
    if isinstance(value, float):
        return _number_to_string(value)
    return str(value)


def _number_to_string(x: float) -> str:
    """ECMA-262 Number::toString(10), from Python's shortest round-trip digits (the same digits JavaScript
    picks)."""
    if math.isnan(x):
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0 else "-Infinity"
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    dec = Decimal(repr(abs(x))).normalize()
    _, digit_tuple, exponent = dec.as_tuple()
    if not isinstance(exponent, int):  # only NaN/Infinity have a non-integer exponent: handled above
        return repr(x)
    digits = "".join(str(d) for d in digit_tuple)
    k = len(digits)
    n = k + exponent  # the decimal point sits after n digits
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
        out = f"{mantissa}e{'+' if e >= 0 else '-'}{abs(e)}"
    return sign + out
