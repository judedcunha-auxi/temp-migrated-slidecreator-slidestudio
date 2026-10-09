"""JavaScript value semantics the generation routes reproduce (Darwin's handlers are TypeScript).

`app/api/legacy.py` holds the request-level ones every route uses (`js_truthy`, `js_parse_int`); these are
the ones the generation, refine and media routes and the image prompt need on top:

* `js_number(text)`: `Number(searchParams.get(x))` for a query string (`Number(null)` is 0, `Number("")`
  is 0, `" 2 "` is 2, `"0x10"` is 16, `"abc"` is NaN);
* `is_js_integer(value)`: `Number.isInteger(value)` for a parsed JSON value (2.0 is an integer, true is not);
* `js_round(x)`: `Math.round`, which rounds halves up (Python's `round` rounds them to even);
* `js_str(x)`: `String(n)` / `${n}` for a number (12.0 is "12", 1e21 is "1e+21", 0.00001 is "0.00001");
* `js_trim(s)` and `js_length(s)`: `String.prototype.trim` (JavaScript's whitespace set) and `.length`
  (UTF-16 code units, so an emoji counts 2).
"""

from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Any

#: `String.prototype.trim`'s set: WhiteSpace and LineTerminator (ECMA-262 §12.2, §12.3).
JS_WHITESPACE = "\t\n\v\f\r              " \
                "    　﻿"

_DECIMAL = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_RADIX = re.compile(r"^0([xXoObB])([0-9a-fA-F]+)$")
_RADIX_BASE = {"x": 16, "o": 8, "b": 2}


def js_trim(text: str) -> str:
    return text.strip(JS_WHITESPACE)


def js_length(text: str) -> int:
    """`text.length`: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def js_number(text: str | None) -> float:
    """`Number(text)` for a query parameter (`None` is a missing parameter, which JavaScript reads as null)."""
    if text is None:
        return 0.0
    s = js_trim(text)
    if s == "":
        return 0.0
    if s in ("Infinity", "+Infinity"):
        return math.inf
    if s == "-Infinity":
        return -math.inf
    radix = _RADIX.match(s)
    if radix:
        base = _RADIX_BASE[radix.group(1).lower()]
        try:
            return float(int(radix.group(2), base))
        except ValueError:
            return math.nan
    if _DECIMAL.match(s):
        return float(s)
    return math.nan


def is_js_integer(value: Any) -> bool:
    """`Number.isInteger(value)` for a JSON value: a finite number with no fraction; booleans are not."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value) and value.is_integer()


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
