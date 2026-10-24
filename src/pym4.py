# pym4 - a GNU m4 compatible macro processor in pure Python
# Copyright (C) 2026 Peter Jan Randewijk
# Expansion model, builtins and eval follow GNU m4 1.4, Copyright (C) Free Software Foundation, Inc.
# SPDX-License-Identifier: GPL-3.0-or-later
"""pym4: a pure-Python macro processor compatible with GNU m4 1.4 (no external programs).

It follows GNU m4's expansion model: expansions are pushed back onto the input
and rescanned, macro arguments are expanded while they are collected, and the
GNU builtins, quoting, comments and diversions behave as in GNU m4 1.4.21.
The shell builtins syscmd and esyscmd are deliberately disabled.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

__all__ = ["M4Error", "M4Lite", "expand", "main", "read_source"]
__version__ = "1.0.0"

DEFAULT_LQUOTE = "`"
DEFAULT_RQUOTE = "'"
DEFAULT_BCOMM = "#"
DEFAULT_ECOMM = "\n"
NESTING_LIMIT = 10000

EOF, WORD, STRING, OPEN, COMMA, CLOSE, SIMPLE, MACDEF = range(8)

_SPACES = " \t\n\v\f\r"
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WORD_CONT_RE = re.compile(r"[A-Za-z0-9_]+")
_DOLLAR_RE = re.compile(r"\$(\d+|[#*@])")
_NUMERIC_RE = re.compile(r"[ \t\n\v\f\r]*([+-]?[0-9]+)\Z")
_CLASS_ESCAPES = {"\t": "\\t", "\n": "\\n", "\v": "\\v", "\f": "\\f", "\r": "\\r"}

sys.setrecursionlimit(max(sys.getrecursionlimit(), 50000))


class M4Error(Exception):
    """Fatal error that stops expansion, as it would stop GNU m4."""


class _Exit(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class Builtin:
    __slots__ = ("name", "func", "blind", "groks")

    def __init__(self, name: str, func, blind: bool, groks: bool) -> None:
        self.name = name
        self.func = func
        self.blind = blind
        self.groks = groks

    def __repr__(self) -> str:
        return f"<{self.name}>"


class _Frame:
    __slots__ = ("text", "pos", "file", "macro")

    def __init__(self, text: str = "", file: str | None = None, macro: Builtin | None = None):
        self.text = text
        self.pos = 0
        self.file = file
        self.macro = macro


def read_source(path) -> str:
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def _class_escape(chars) -> str:
    return "".join("\\" + c if c in "\\]^-[" else _CLASS_ESCAPES.get(c, c) for c in chars)


# --------------------------------------------------------------------------
# GNU m4 regular expressions (Emacs syntax) translated to Python syntax

_POSIX_CLASSES = {
    "alpha": "A-Za-z",
    "digit": "0-9",
    "alnum": "A-Za-z0-9",
    "upper": "A-Z",
    "lower": "a-z",
    "space": " \\t\\n\\r\\f\\v",
    "blank": " \\t",
    "punct": "".join("\\" + c for c in "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"),
    "xdigit": "0-9A-Fa-f",
    "cntrl": "\\x00-\\x1f\\x7f",
    "print": "\\x20-\\x7e",
    "graph": "\\x21-\\x7e",
}


def _bracket(pattern: str, start: int) -> tuple[int, str]:
    j = start + 1
    negate = pattern.startswith("^", j)
    if negate:
        j += 1
    items: list[str] = []
    first = True
    while True:
        if j >= len(pattern):
            raise re.error("unmatched [ or [^")
        c = pattern[j]
        if c == "]" and not first:
            j += 1
            break
        if c == "[" and pattern.startswith(":", j + 1):
            end = pattern.find(":]", j + 2)
            if end >= 0:
                items.append(_POSIX_CLASSES.get(pattern[j + 2 : end], ""))
                j = end + 2
                first = False
                continue
        if c == "-" and not first and not pattern.startswith("]", j + 1):
            items.append("-")
        elif c in "\\[]^-":
            items.append("\\" + c)
        else:
            items.append(c)
        first = False
        j += 1
    return j, "[" + ("^" if negate else "") + "".join(items) + "]"


def emacs_regex(pattern: str) -> str:
    """Translate a GNU m4 (Emacs syntax) regular expression to Python syntax."""
    out: list[str] = []
    i = 0
    n = len(pattern)
    at_start = True
    after_op = False
    while i < n:
        c = pattern[i]
        if c == "\\":
            if i + 1 >= n:
                raise re.error("trailing backslash")
            d = pattern[i + 1]
            i += 2
            after_op = False
            if d in "(|":
                out.append(d)
                at_start = True
                continue
            if d == ")":
                out.append(")")
            elif d == "{":
                end = pattern.find("\\}", i)
                if end < 0:
                    raise re.error("unmatched \\{")
                out.append("{" + pattern[i:end] + "}")
                i = end + 2
                after_op = True
            elif d in "wWbB":
                out.append("\\" + d)
            elif d in "<>":
                out.append(r"\b")
            elif d == "`":
                out.append(r"\A")
            elif d == "'":
                out.append(r"\Z")
            elif d in "123456789":
                out.append("\\" + d)
            elif d in "sS" and i < n:
                cls = pattern[i]
                i += 1
                if cls in "- ":
                    out.append("\\s" if d == "s" else "\\S")
                else:
                    out.append(re.escape(cls))
            else:
                out.append(re.escape(d))
            at_start = False
            continue
        if c == "[":
            i, cls = _bracket(pattern, i)
            out.append(cls)
            at_start = False
            after_op = False
            continue
        if c in "*+?":
            i += 1
            if at_start:
                out.append("\\" + c)
                at_start = False
            elif not after_op:
                out.append(c)
                after_op = True
            continue
        if c == "^":
            out.append("^" if at_start else r"\^")
            i += 1
            after_op = False
            continue
        if c == "$":
            rest = pattern[i + 1 :]
            anchor = rest == "" or rest.startswith("\\)") or rest.startswith("\\|")
            out.append("$" if anchor else r"\$")
        elif c == ".":
            out.append(".")
        else:
            out.append(re.escape(c))
        i += 1
        at_start = False
        after_op = False
    return "".join(out)


_REGEX_CACHE: dict[str, re.Pattern] = {}


def compile_regex(pattern: str) -> re.Pattern:
    compiled = _REGEX_CACHE.get(pattern)
    if compiled is None:
        compiled = re.compile(emacs_regex(pattern), re.M)
        _REGEX_CACHE[pattern] = compiled
    return compiled


def regex_search(pattern: re.Pattern, text: str, pos: int = 0):
    """Search like GNU regex: leftmost match, and the longest one at that position.

    Python alternation is first-match; GNU m4 (POSIX) prefers the longest, e.g.
    L(M|B|MB|BM)? on "LMBS" must give "LMB".
    """
    match = pattern.search(text, pos)
    if match is None or "|" not in pattern.pattern or "$" in pattern.pattern:
        return match
    start = match.start()
    for end in range(min(len(text), start + 4096), match.end(), -1):
        longer = pattern.fullmatch(text, start, end)
        if longer is not None:
            return longer
    return match


def substitute(match: re.Match, repl: str, warn) -> str:
    """Expand \\&, \\0 and \\1..\\9 in a regexp or patsubst replacement."""
    out: list[str] = []
    i = 0
    n = len(repl)
    while i < n:
        j = repl.find("\\", i)
        if j < 0:
            out.append(repl[i:])
            break
        out.append(repl[i:j])
        if j + 1 >= n:
            warn("Warning: trailing \\ ignored in replacement")
            break
        c = repl[j + 1]
        if c in "0&":
            out.append(match.group(0))
        elif c in "123456789":
            k = int(c)
            if k > match.re.groups:
                warn(f"Warning: sub-expression {k} not present")
            else:
                out.append(match.group(k) or "")
        else:
            out.append(c)
        i = j + 2
    return "".join(out)


# --------------------------------------------------------------------------
# eval: a port of GNU m4's eval.c (32-bit wrap-around arithmetic)

E_ERROR, E_BADNUM, E_BADOP, E_EOTEXT, E_LEFTP, E_RIGHTP, E_LNOT, E_NOT, E_NUMBER = range(9)
E_LOR, E_LAND, E_OR, E_XOR, E_AND = 10, 20, 30, 40, 50
E_ASSIGN, E_EQ, E_NOTEQ = 60, 61, 62
E_GT, E_GTEQ, E_LS, E_LSEQ = 70, 71, 72, 73
E_LSHIFT, E_RSHIFT = 80, 81
E_PLUS, E_MINUS = 90, 91
E_TIMES, E_DIVIDE, E_MODULO = 100, 101, 102
E_EXPONENT = 110

(NO_ERROR, DIVIDE_ZERO, MODULO_ZERO, NEGATIVE_EXPONENT, SYNTAX_ERROR, MISSING_RIGHT,
 UNKNOWN_INPUT, EXCESS_INPUT, INVALID_NUMBER, INVALID_OPERATOR) = range(10)

_EVAL_MESSAGES = {
    MISSING_RIGHT: "bad expression in eval (missing right parenthesis): ",
    SYNTAX_ERROR: "bad expression in eval: ",
    UNKNOWN_INPUT: "bad expression in eval (bad input): ",
    EXCESS_INPUT: "bad expression in eval (excess input): ",
    INVALID_NUMBER: "invalid number in eval: ",
    INVALID_OPERATOR: "invalid operator in eval: ",
    DIVIDE_ZERO: "divide by zero in eval: ",
    MODULO_ZERO: "modulo by zero in eval: ",
    NEGATIVE_EXPONENT: "negative exponent in eval: ",
}


def _i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _c_div(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


class _Evaluator:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.last = 0

    def undo(self) -> None:
        self.pos = self.last

    def lex(self) -> tuple[int, int]:
        text = self.text
        n = len(text)
        while self.pos < n and text[self.pos] in _SPACES:
            self.pos += 1
        self.last = self.pos
        if self.pos >= n:
            return E_EOTEXT, 0
        c = text[self.pos]
        if "0" <= c <= "9":
            seen_digit = False
            if c == "0":
                self.pos += 1
                d = text[self.pos : self.pos + 1]
                if d in ("x", "X"):
                    base = 16
                    self.pos += 1
                elif d in ("b", "B"):
                    base = 2
                    self.pos += 1
                elif d in ("r", "R"):
                    self.pos += 1
                    base = 0
                    while self.pos < n and "0" <= text[self.pos] <= "9" and base <= 36:
                        base = 10 * base + int(text[self.pos])
                        self.pos += 1
                    if base == 0 or base > 36 or text[self.pos : self.pos + 1] != ":":
                        return E_BADNUM, 0
                    self.pos += 1
                else:
                    base = 8
                    seen_digit = True
            else:
                base = 10
            value = 0
            while self.pos < n:
                ch = text[self.pos]
                if "0" <= ch <= "9":
                    digit = ord(ch) - 48
                elif "a" <= ch <= "z":
                    digit = ord(ch) - 87
                elif "A" <= ch <= "Z":
                    digit = ord(ch) - 55
                else:
                    break
                if base == 1:
                    if digit == 1:
                        value += 1
                    elif not (digit == 0 and value == 0):
                        return E_BADNUM, 0
                elif digit >= base:
                    return E_BADNUM, 0
                else:
                    value = (value * base + digit) & 0xFFFFFFFF
                self.pos += 1
                seen_digit = True
            if not seen_digit:
                return E_BADNUM, 0
            return E_NUMBER, _i32(value)
        self.pos += 1
        nxt = text[self.pos : self.pos + 1]
        if c == "+":
            return (E_BADOP, 0) if nxt in ("+", "=") else (E_PLUS, 0)
        if c == "-":
            return (E_BADOP, 0) if nxt in ("-", "=") else (E_MINUS, 0)
        if c == "*":
            if nxt == "*":
                self.pos += 1
                return E_EXPONENT, 0
            return (E_BADOP, 0) if nxt == "=" else (E_TIMES, 0)
        if c == "/":
            return (E_BADOP, 0) if nxt == "=" else (E_DIVIDE, 0)
        if c == "%":
            return (E_BADOP, 0) if nxt == "=" else (E_MODULO, 0)
        if c == "=":
            if nxt == "=":
                self.pos += 1
                return E_EQ, 0
            return E_ASSIGN, 0
        if c == "!":
            if nxt == "=":
                self.pos += 1
                return E_NOTEQ, 0
            return E_LNOT, 0
        if c == ">":
            if nxt == "=":
                self.pos += 1
                return E_GTEQ, 0
            if nxt == ">":
                self.pos += 1
                return (E_BADOP, 0) if text[self.pos : self.pos + 1] == "=" else (E_RSHIFT, 0)
            return E_GT, 0
        if c == "<":
            if nxt == "=":
                self.pos += 1
                return E_LSEQ, 0
            if nxt == "<":
                self.pos += 1
                return (E_BADOP, 0) if text[self.pos : self.pos + 1] == "=" else (E_LSHIFT, 0)
            return E_LS, 0
        if c == "^":
            return (E_BADOP, 0) if nxt == "=" else (E_XOR, 0)
        if c == "~":
            return E_NOT, 0
        if c == "&":
            if nxt == "&":
                self.pos += 1
                return E_LAND, 0
            return (E_BADOP, 0) if nxt == "=" else (E_AND, 0)
        if c == "|":
            if nxt == "|":
                self.pos += 1
                return E_LOR, 0
            return (E_BADOP, 0) if nxt == "=" else (E_OR, 0)
        if c == "(":
            return E_LEFTP, 0
        if c == ")":
            return E_RIGHTP, 0
        return E_ERROR, 0

    def primary(self) -> tuple[int, int]:
        tok, value = self.lex()
        if tok == E_NUMBER:
            return NO_ERROR, value
        if tok == E_LEFTP:
            er, value = self.primary()
            er, value = self.parse_expr(value, er, 1)
            if er >= SYNTAX_ERROR:
                return er, value
            tok2, _ = self.lex()
            if tok2 == E_ERROR:
                return UNKNOWN_INPUT, value
            if tok2 == E_BADNUM:
                return INVALID_NUMBER, value
            if tok2 == E_BADOP:
                return INVALID_OPERATOR, value
            if tok2 == E_RIGHTP:
                return er, value
            return MISSING_RIGHT, value
        if tok == E_PLUS:
            return self.primary()
        if tok == E_MINUS:
            er, value = self.primary()
            return er, _i32(-value)
        if tok == E_NOT:
            er, value = self.primary()
            return er, _i32(~value)
        if tok == E_LNOT:
            er, value = self.primary()
            return er, 1 if value == 0 else 0
        if tok == E_ERROR:
            return UNKNOWN_INPUT, 0
        if tok == E_BADNUM:
            return INVALID_NUMBER, 0
        if tok == E_BADOP:
            return INVALID_OPERATOR, 0
        return SYNTAX_ERROR, 0

    def parse_expr(self, v1: int, er: int, min_prec: int) -> tuple[int, int]:
        if er >= SYNTAX_ERROR:
            return er, v1
        et, _ = self.lex()
        while et // 10 >= min_prec:
            er2, v2 = self.primary()
            if er2 >= SYNTAX_ERROR:
                return er2, v1
            et2, _ = self.lex()
            while et2 // 10 > et // 10 or et2 == E_EXPONENT:
                self.undo()
                er2, v2 = self.parse_expr(v2, er2, et2 // 10)
                if er2 >= SYNTAX_ERROR:
                    return er2, v1
                et2, _ = self.lex()
            if et == E_EXPONENT:
                if v2 < 0:
                    er = NEGATIVE_EXPONENT
                elif v1 == 0 and v2 == 0:
                    er = DIVIDE_ZERO
                else:
                    v1 = _i32(pow(v1 & 0xFFFFFFFF, v2, 0x100000000))
            elif et == E_TIMES:
                v1 = _i32(v1 * v2)
            elif et == E_DIVIDE:
                if v2 == 0:
                    er = DIVIDE_ZERO
                else:
                    v1 = _i32(_c_div(v1, v2))
            elif et == E_MODULO:
                if v2 == 0:
                    er = MODULO_ZERO
                else:
                    v1 = _i32(v1 - _c_div(v1, v2) * v2)
            elif et == E_PLUS:
                v1 = _i32(v1 + v2)
            elif et == E_MINUS:
                v1 = _i32(v1 - v2)
            elif et == E_LSHIFT:
                v1 = _i32((v1 & 0xFFFFFFFF) << (v2 & 0x1F))
            elif et == E_RSHIFT:
                v1 = v1 >> (v2 & 0x1F)
            elif et == E_GT:
                v1 = int(v1 > v2)
            elif et == E_GTEQ:
                v1 = int(v1 >= v2)
            elif et == E_LS:
                v1 = int(v1 < v2)
            elif et == E_LSEQ:
                v1 = int(v1 <= v2)
            elif et in (E_ASSIGN, E_EQ):
                v1 = int(v1 == v2)
            elif et == E_NOTEQ:
                v1 = int(v1 != v2)
            elif et == E_AND:
                v1 = _i32(v1 & v2)
            elif et == E_XOR:
                v1 = _i32(v1 ^ v2)
            elif et == E_OR:
                v1 = _i32(v1 | v2)
            elif et == E_LAND:
                if not v1:
                    er2 = NO_ERROR
                v1 = int(bool(v1) and bool(v2))
            elif et == E_LOR:
                if v1:
                    er2 = NO_ERROR
                v1 = int(bool(v1) or bool(v2))
            if er == NO_ERROR:
                er = er2
            et = et2
        self.undo()
        return er, v1


def evaluate(expr: str) -> tuple[int, int]:
    """Return (error code, value) for a GNU m4 eval expression."""
    ev = _Evaluator(expr)
    er, value = ev.primary()
    er, value = ev.parse_expr(value, er, 1)
    if er == NO_ERROR and ev.pos < len(expr):
        tok, _ = ev.lex()
        if tok == E_BADNUM:
            er = INVALID_NUMBER
        elif tok == E_BADOP:
            er = INVALID_OPERATOR
        elif tok != E_EOTEXT:
            er = EXCESS_INPUT
    return er, value


def _ntoa(value: int, radix: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    negative = value < 0
    u = (-value) & 0xFFFFFFFF if negative else value
    out = []
    while True:
        out.append(digits[u % radix])
        u //= radix
        if u == 0:
            break
    return ("-" if negative else "") + "".join(reversed(out))


def _expand_ranges(s: str) -> str:
    out: list[str] = []
    prev = ""
    i = 0
    while i < len(s):
        c = s[i]
        if c == "-" and prev:
            if i + 1 >= len(s):
                out.append("-")
                break
            to = s[i + 1]
            a, b = ord(prev), ord(to)
            if a <= b:
                out.extend(chr(x) for x in range(a + 1, b + 1))
            else:
                out.extend(chr(x) for x in range(a - 1, b - 1, -1))
            prev = to
            i += 2
        else:
            out.append(c)
            prev = c
            i += 1
    return "".join(out)


_FORMAT_RE = re.compile(r"%([-+ #0']*)(\*|\d+)?(?:\.(\*|\d*))?(hh|h|ll|l|L|q|j|z|t)?(.)?", re.S)


def _c_long(text: str) -> int:
    m = re.match(r"\s*([+-]?)(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)", text)
    if not m:
        return 0
    digits = m.group(2)
    if digits[:2] in ("0x", "0X"):
        value = int(digits, 16)
    elif digits.startswith("0") and len(digits) > 1:
        value = int(digits, 8)
    else:
        value = int(digits)
    return -value if m.group(1) == "-" else value


def _c_double(text: str) -> float:
    m = re.match(r"\s*[+-]?(?:\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|inf|nan)", text, re.I)
    try:
        return float(m.group(0)) if m else 0.0
    except ValueError:
        return 0.0


def format_args(fmt: str, args: list[str]) -> str:
    """The GNU m4 format builtin (printf-like)."""
    out: list[str] = []
    queue = list(args)

    def take() -> str:
        return queue.pop(0) if queue else ""

    i = 0
    while i < len(fmt):
        j = fmt.find("%", i)
        if j < 0:
            out.append(fmt[i:])
            break
        out.append(fmt[i:j])
        if fmt.startswith("%%", j):
            out.append("%")
            i = j + 2
            continue
        m = _FORMAT_RE.match(fmt, j)
        flags, width, prec, _, conv = m.groups()
        i = m.end()
        if conv is None:
            break
        flags = (flags or "").replace("'", "")
        if width == "*":
            width = str(_c_long(take()))
        if prec == "*":
            prec = str(_c_long(take()))
        spec = "%" + flags + (width or "") + ("." + prec if prec is not None else "")
        try:
            if conv in "di":
                out.append((spec + "d") % _c_long(take()))
            elif conv in "uxX":
                out.append((spec + ("d" if conv == "u" else conv)) % (_c_long(take()) & 0xFFFFFFFF))
            elif conv == "o":
                out.append((spec + "o") % (_c_long(take()) & 0xFFFFFFFF))
            elif conv == "c":
                out.append((spec + "c") % chr(_c_long(take()) & 0xFF))
            elif conv == "s":
                out.append((spec + "s") % take())
            elif conv in "eEfFgG":
                out.append((spec + conv) % _c_double(take()))
            elif conv in "aA":
                out.append(float.hex(_c_double(take())))
            else:
                out.append(m.group(0))
        except (ValueError, TypeError, OverflowError):
            pass
    return "".join(out)


# --------------------------------------------------------------------------


class M4Lite:
    """A GNU m4 1.4 compatible macro processor."""

    def __init__(self, include_dirs=None, *, warn=None, max_expansions: int = 20_000_000,
                 deadline: float | None = None) -> None:
        self.include_dirs = [Path(d) for d in (include_dirs or [])]
        for entry in os.environ.get("M4PATH", "").split(os.pathsep):
            if entry:
                self.include_dirs.append(Path(entry))
        self.warnings: list[str] = []
        self._warn_cb = warn
        self.symtab: dict[str, list] = {}
        self.lquote, self.rquote = DEFAULT_LQUOTE, DEFAULT_RQUOTE
        self.bcomm, self.ecomm = DEFAULT_BCOMM, DEFAULT_ECOMM
        self._update_syntax()
        self.diversions: dict[int, list[str]] = {0: []}
        self.current_diversion = 0
        self.wrapups: list[str] = []
        self.frames: list[_Frame] = []
        self.expansion_level = 0
        self.expansions = 0
        self.max_expansions = max_expansions
        self.deadline = deadline
        self.base_dir = Path.cwd()
        self.sysval = 0
        self.exit_code = 0
        self._shell_warned = False
        self._builtins: dict[str, Builtin] = {}
        self._install_builtins()

    # ---------------------------------------------------------------- setup

    def _install_builtins(self) -> None:
        table = [
            ("__file__", self._b_file, False, False),
            ("__line__", self._b_line, False, False),
            ("__program__", self._b_program, False, False),
            ("builtin", self._b_builtin, True, True),
            ("changecom", self._b_changecom, False, False),
            ("changequote", self._b_changequote, False, False),
            ("debugmode", self._b_noop, False, False),
            ("debugfile", self._b_noop, False, False),
            ("decr", self._b_decr, True, False),
            ("define", self._b_define, True, True),
            ("defn", self._b_defn, True, False),
            ("divert", self._b_divert, False, False),
            ("divnum", self._b_divnum, False, False),
            ("dnl", self._b_dnl, False, False),
            ("dumpdef", self._b_dumpdef, False, False),
            ("errprint", self._b_errprint, True, False),
            ("esyscmd", self._b_esyscmd, True, False),
            ("eval", self._b_eval, True, False),
            ("format", self._b_format, True, False),
            ("ifdef", self._b_ifdef, True, False),
            ("ifelse", self._b_ifelse, True, False),
            ("include", self._b_include, True, False),
            ("incr", self._b_incr, True, False),
            ("index", self._b_index, True, False),
            ("indir", self._b_indir, True, True),
            ("len", self._b_len, True, False),
            ("m4exit", self._b_m4exit, False, False),
            ("m4wrap", self._b_m4wrap, True, False),
            ("maketemp", self._b_maketemp, True, False),
            ("mkstemp", self._b_maketemp, True, False),
            ("patsubst", self._b_patsubst, True, False),
            ("popdef", self._b_popdef, True, False),
            ("pushdef", self._b_pushdef, True, True),
            ("regexp", self._b_regexp, True, False),
            ("shift", self._b_shift, True, False),
            ("sinclude", self._b_sinclude, True, False),
            ("substr", self._b_substr, True, False),
            ("syscmd", self._b_syscmd, True, False),
            ("sysval", self._b_sysval, False, False),
            ("traceoff", self._b_noop, False, False),
            ("traceon", self._b_noop, False, False),
            ("translit", self._b_translit, True, False),
            ("undefine", self._b_undefine, True, False),
            ("undivert", self._b_undivert, False, False),
        ]
        for name, func, blind, groks in table:
            builtin = Builtin(name, func, blind, groks)
            self._builtins[name] = builtin
            self.symtab[name] = [builtin]
        self.symtab["__gnu__"] = [""]
        self.symtab["__unix__"] = [""]

    def _update_syntax(self) -> None:
        special = {"(", ")", ","}
        if self.lquote:
            special.add(self.lquote[0])
        if self.bcomm:
            special.add(self.bcomm[0])
        spaces = [c for c in _SPACES if c not in special]
        self._space_re = re.compile("[" + _class_escape(spaces) + "]+") if spaces else None
        self._other_re = re.compile(
            "[^A-Za-z_" + _class_escape(_SPACES) + _class_escape(sorted(special)) + "]+")

    def warn(self, message: str) -> None:
        text = f"pym4: {self._current_file() or 'stdin'}: {message}"
        self.warnings.append(text)
        if self._warn_cb is not None:
            self._warn_cb(text)

    def define(self, name: str, value: str = "") -> None:
        self.symtab[name] = [value]

    def undefine(self, name: str) -> None:
        self.symtab.pop(name, None)

    # ---------------------------------------------------------------- input

    def _current_file(self) -> str:
        for frame in reversed(self.frames):
            if frame.file is not None:
                return frame.file
        return ""

    def _peek_char(self):
        for frame in reversed(self.frames):
            if frame.macro is not None:
                return frame.macro
            if frame.pos < len(frame.text):
                return frame.text[frame.pos]
        return None

    def _next_char(self):
        frames = self.frames
        while frames:
            frame = frames[-1]
            if frame.macro is not None:
                frames.pop()
                return frame.macro
            if frame.pos < len(frame.text):
                c = frame.text[frame.pos]
                frame.pos += 1
                return c
            frames.pop()
        return None

    def _read_comment(self) -> str:
        parts = [self.bcomm]
        ec = self.ecomm
        while True:
            if not self.frames:
                raise M4Error("ERROR: end of file in comment")
            frame = self.frames[-1]
            if frame.macro is not None:
                self.frames.pop()
                continue
            idx = frame.text.find(ec, frame.pos) if ec else -1
            if idx >= 0:
                parts.append(frame.text[frame.pos : idx + len(ec)])
                frame.pos = idx + len(ec)
                return "".join(parts)
            parts.append(frame.text[frame.pos :])
            frame.pos = len(frame.text)
            self.frames.pop()

    def _read_quoted(self) -> str:
        lq, rq = self.lquote, self.rquote
        depth = 1
        parts: list[str] = []
        while True:
            if not self.frames:
                raise M4Error("ERROR: end of file in string")
            frame = self.frames[-1]
            if frame.macro is not None:
                self.frames.pop()
                continue
            text = frame.text
            pos = frame.pos
            while True:
                ir = text.find(rq, pos)
                il = text.find(lq, pos) if lq else -1
                if ir < 0 and il < 0:
                    parts.append(text[pos:])
                    frame.pos = len(text)
                    break
                if ir >= 0 and (il < 0 or ir <= il):
                    depth -= 1
                    if depth == 0:
                        parts.append(text[pos:ir])
                        frame.pos = ir + len(rq)
                        return "".join(parts)
                    parts.append(text[pos : ir + len(rq)])
                    pos = ir + len(rq)
                else:
                    depth += 1
                    parts.append(text[pos : il + len(lq)])
                    pos = il + len(lq)
            self.frames.pop()

    def _next_token(self):
        frames = self.frames
        while frames:
            frame = frames[-1]
            if frame.macro is not None:
                frames.pop()
                return MACDEF, frame.macro
            text = frame.text
            pos = frame.pos
            if pos >= len(text):
                frames.pop()
                continue
            c = text[pos]
            bc = self.bcomm
            if bc and c == bc[0] and text.startswith(bc, pos):
                frame.pos = pos + len(bc)
                return STRING, self._read_comment()
            if ("a" <= c <= "z") or ("A" <= c <= "Z") or c == "_":
                m = _WORD_RE.match(text, pos)
                frame.pos = m.end()
                word = m.group()
                if frame.pos >= len(text):
                    word += self._continue_word()
                return WORD, word
            lq = self.lquote
            if lq and c == lq[0] and text.startswith(lq, pos):
                frame.pos = pos + len(lq)
                return STRING, self._read_quoted()
            if c == "(":
                frame.pos = pos + 1
                return OPEN, "("
            if c == ",":
                frame.pos = pos + 1
                return COMMA, ","
            if c == ")":
                frame.pos = pos + 1
                return CLOSE, ")"
            if self._space_re is not None:
                m = self._space_re.match(text, pos)
                if m:
                    frame.pos = m.end()
                    return SIMPLE, m.group()
            m = self._other_re.match(text, pos)
            if m:
                frame.pos = m.end()
                return SIMPLE, m.group()
            frame.pos = pos + 1
            return SIMPLE, c
        return EOF, ""

    def _continue_word(self) -> str:
        parts = []
        while self.frames:
            frame = self.frames[-1]
            if frame.macro is not None:
                break
            if frame.pos >= len(frame.text):
                self.frames.pop()
                continue
            m = _WORD_CONT_RE.match(frame.text, frame.pos)
            if not m:
                break
            parts.append(m.group())
            frame.pos = m.end()
            if frame.pos < len(frame.text):
                break
        return "".join(parts)

    def _skip_line(self) -> None:
        while self.frames:
            frame = self.frames[-1]
            if frame.macro is not None:
                self.frames.pop()
                continue
            idx = frame.text.find("\n", frame.pos)
            if idx >= 0:
                frame.pos = idx + 1
                return
            frame.pos = len(frame.text)
            self.frames.pop()
        self.warn("Warning: end of file treated as newline")

    # ---------------------------------------------------------------- output

    def _output(self, text: str) -> None:
        if self.current_diversion >= 0:
            self.diversions.setdefault(self.current_diversion, []).append(text)

    def _shipout(self, text: str, obs) -> None:
        if obs is not None:
            obs.append(text)
        elif self.current_diversion >= 0:
            self.diversions.setdefault(self.current_diversion, []).append(text)

    # ---------------------------------------------------------------- expansion

    def _lookup(self, name: str):
        stack = self.symtab.get(name)
        return stack[-1] if stack else None

    def _expand_word(self, word: str, obs) -> None:
        defn = self._lookup(word)
        if defn is None or (isinstance(defn, Builtin) and defn.blind and self._peek_char() != "("):
            self._shipout(word, obs)
        else:
            self._expand_macro(word, defn)

    def _expand_macro(self, name: str, defn) -> None:
        self.expansions += 1
        if self.expansions > self.max_expansions:
            raise M4Error("expansion limit exceeded (infinite recursion?)")
        if self.deadline is not None and not (self.expansions & 1023) and time.monotonic() > self.deadline:
            raise M4Error("time limit exceeded")
        self.expansion_level += 1
        if self.expansion_level > NESTING_LIMIT:
            raise M4Error(f"recursion limit of {NESTING_LIMIT} exceeded")
        try:
            argv: list = [name]
            groks = isinstance(defn, Builtin) and defn.groks
            if self._peek_char() == "(" and not self.lquote.startswith("(") and not self.bcomm.startswith("("):
                self._next_char()
                while True:
                    arg, more = self._expand_argument(groks)
                    argv.append(arg)
                    if not more:
                        break
            result = self._call(name, defn, argv)
        finally:
            self.expansion_level -= 1
        if result:
            self.frames.append(_Frame(result))

    def _expand_argument(self, groks: bool):
        obs: list[str] = []
        builtin_arg = None
        kind, value = self._next_token()
        while kind == SIMPLE and value[0] in _SPACES:
            kind, value = self._next_token()
        level = 0
        while True:
            if kind == COMMA or kind == CLOSE:
                if level == 0:
                    text = "".join(obs)
                    if builtin_arg is not None and text:
                        self.warn("Warning: cannot concatenate builtin tokens")
                        builtin_arg = None
                    return (builtin_arg if builtin_arg is not None else text), kind == COMMA
                if kind == CLOSE:
                    level -= 1
                obs.append(value)
            elif kind == OPEN:
                level += 1
                obs.append(value)
            elif kind == WORD:
                self._expand_word(value, obs)
            elif kind == STRING or kind == SIMPLE:
                obs.append(value)
            elif kind == MACDEF:
                if groks and not obs and builtin_arg is None:
                    builtin_arg = value
                else:
                    self.warn("Warning: cannot concatenate builtin tokens")
                    builtin_arg = None
            else:
                raise M4Error("ERROR: end of file in argument list")
            kind, value = self._next_token()

    def _call(self, name: str, defn, argv: list):
        if isinstance(defn, Builtin):
            if not defn.groks:
                argv = [a if isinstance(a, str) else "" for a in argv]
            return defn.func(argv)
        return self._expand_user(defn, argv)

    def _expand_user(self, body: str, argv: list) -> str:
        if "$" not in body:
            return body
        args = [a if isinstance(a, str) else "" for a in argv]
        lq, rq = self.lquote, self.rquote

        def repl(m: re.Match) -> str:
            tok = m.group(1)
            if tok == "#":
                return str(len(args) - 1)
            if tok == "*":
                return ",".join(args[1:])
            if tok == "@":
                return ",".join(lq + a + rq for a in args[1:])
            i = int(tok)
            return args[i] if i < len(args) else ""

        return _DOLLAR_RE.sub(repl, body)

    def _expand_input(self) -> None:
        while True:
            kind, value = self._next_token()
            if kind == EOF:
                return
            if kind == WORD:
                self._expand_word(value, None)
            elif kind != MACDEF:
                self._output(value)

    # ---------------------------------------------------------------- helpers

    def _numeric(self, name: str, text: str):
        if text == "":
            self.warn(f"empty string treated as 0 in builtin `{name}'")
            return 0
        m = _NUMERIC_RE.match(text)
        if not m:
            self.warn(f"non-numeric argument to builtin `{name}'")
            return None
        if text[0] in _SPACES:
            self.warn(f"leading whitespace ignored in builtin `{name}'")
        return _i32(int(m.group(1)))

    def _bad_argc(self, argv: list, minimum: int, maximum: int) -> bool:
        argc = len(argv)
        if minimum > 0 and argc < minimum:
            self.warn(f"Warning: too few arguments to builtin `{argv[0]}'")
            return True
        if maximum > 0 and argc > maximum:
            self.warn(f"Warning: excess arguments to builtin `{argv[0]}' ignored")
        return False

    def find_file(self, name: str) -> Path | None:
        if not name:
            return None
        path = Path(name)
        if path.is_absolute():
            return path if path.is_file() else None
        for candidate in [self.base_dir / path] + [d / path for d in self.include_dirs]:
            if candidate.is_file():
                return candidate
        return None

    # ---------------------------------------------------------------- builtins

    def _b_noop(self, argv):
        return None

    def _b_file(self, argv):
        self._bad_argc(argv, 1, 1)
        return self.lquote + self._current_file() + self.rquote

    def _b_line(self, argv):
        return "0"

    def _b_program(self, argv):
        return self.lquote + "m4" + self.rquote

    def _b_define(self, argv, push: bool = False):
        if self._bad_argc(argv, 2, 3):
            return None
        name = argv[1]
        if not isinstance(name, str):
            self.warn(f"Warning: {argv[0]}: invalid macro name ignored")
            return None
        body = argv[2] if len(argv) > 2 else ""
        stack = self.symtab.get(name)
        if push or not stack:
            self.symtab.setdefault(name, []).append(body)
        else:
            stack[-1] = body
        return None

    def _b_pushdef(self, argv):
        return self._b_define(argv, push=True)

    def _b_undefine(self, argv):
        if self._bad_argc(argv, 2, -1):
            return None
        for name in argv[1:]:
            self.symtab.pop(name, None)
        return None

    def _b_popdef(self, argv):
        if self._bad_argc(argv, 2, -1):
            return None
        for name in argv[1:]:
            stack = self.symtab.get(name)
            if stack:
                stack.pop()
                if not stack:
                    del self.symtab[name]
        return None

    def _b_defn(self, argv):
        if self._bad_argc(argv, 2, -1):
            return None
        out = []
        for name in argv[1:]:
            defn = self._lookup(name)
            if defn is None:
                continue
            if isinstance(defn, Builtin):
                if len(argv) != 2:
                    self.warn(f"Warning: cannot concatenate builtin `{name}'")
                else:
                    self.frames.append(_Frame(macro=defn))
            else:
                out.append(self.lquote + defn + self.rquote)
        return "".join(out)

    def _b_ifdef(self, argv):
        if self._bad_argc(argv, 3, 4):
            return None
        if self._lookup(argv[1]) is not None:
            return argv[2]
        return argv[3] if len(argv) >= 4 else None

    def _b_ifelse(self, argv):
        if len(argv) == 2:
            return None
        if self._bad_argc(argv, 4, -1):
            return None
        if (len(argv) + 2) % 3 == 2:
            self.warn("Warning: excess arguments to builtin `ifelse' ignored")
        args = argv[1:]
        n = len(args)
        i = 0
        while True:
            if args[i] == args[i + 1]:
                return args[i + 2]
            remaining = n - i
            if remaining == 3:
                return None
            if remaining in (4, 5):
                return args[i + 3]
            i += 3

    def _b_shift(self, argv):
        if self._bad_argc(argv, 2, -1):
            return None
        return ",".join(self.lquote + a + self.rquote for a in argv[2:])

    def _b_changequote(self, argv):
        self._bad_argc(argv, 1, 3)
        lq = argv[1] if len(argv) >= 2 else None
        rq = argv[2] if len(argv) >= 3 else None
        if lq is None:
            lq, rq = DEFAULT_LQUOTE, DEFAULT_RQUOTE
        elif rq is None or (lq and not rq):
            rq = DEFAULT_RQUOTE
        self.lquote, self.rquote = lq, rq
        self._update_syntax()
        return None

    def _b_changecom(self, argv):
        self._bad_argc(argv, 1, 3)
        bc = argv[1] if len(argv) >= 2 else None
        ec = argv[2] if len(argv) >= 3 else None
        if bc is None:
            bc = ec = ""
        elif ec is None or (bc and not ec):
            ec = DEFAULT_ECOMM
        self.bcomm, self.ecomm = bc, ec
        self._update_syntax()
        return None

    def _b_divert(self, argv):
        if self._bad_argc(argv, 1, 2):
            return None
        number = 0
        if len(argv) >= 2:
            number = self._numeric(argv[0], argv[1])
            if number is None:
                return None
        self.current_diversion = number
        return None

    def _b_divnum(self, argv):
        self._bad_argc(argv, 1, 1)
        return str(self.current_diversion)

    def _insert_diversion(self, number: int) -> None:
        if number <= 0 or number == self.current_diversion:
            return
        parts = self.diversions.pop(number, None)
        if parts and self.current_diversion >= 0:
            self.diversions.setdefault(self.current_diversion, []).extend(parts)

    def _b_undivert(self, argv):
        if len(argv) == 1:
            for number in sorted(k for k in self.diversions if k > 0):
                self._insert_diversion(number)
            return None
        for arg in argv[1:]:
            if re.fullmatch(r"[+-]?[0-9]+", arg):
                self._insert_diversion(int(arg))
                continue
            path = self.find_file(arg)
            if path is None:
                self.warn(f"cannot undivert `{arg}'")
            else:
                self._output(read_source(path))
        return None

    def _b_dnl(self, argv):
        self._bad_argc(argv, 1, 1)
        self._skip_line()
        return None

    def _b_dumpdef(self, argv):
        for name in argv[1:] or sorted(self.symtab):
            defn = self._lookup(name)
            if defn is None:
                self.warn(f"undefined macro `{name}'")
            else:
                shown = repr(defn) if isinstance(defn, Builtin) else defn
                sys.stderr.write(f"{name}:\t{shown}\n")
        return None

    def _b_errprint(self, argv):
        if self._bad_argc(argv, 2, -1):
            return None
        sys.stderr.write(" ".join(argv[1:]))
        return None

    def _shell_disabled(self, name: str) -> None:
        if not self._shell_warned:
            self.warn(f"`{name}' is disabled: pym4 does not run shell commands")
            self._shell_warned = True
        self.sysval = 127

    def _b_syscmd(self, argv):
        if not self._bad_argc(argv, 2, 2):
            self._shell_disabled(argv[0])
        return None

    def _b_esyscmd(self, argv):
        if not self._bad_argc(argv, 2, 2):
            self._shell_disabled(argv[0])
        return None

    def _b_sysval(self, argv):
        return str(self.sysval)

    def _b_eval(self, argv):
        if self._bad_argc(argv, 2, 4):
            return None
        expr = argv[1]
        radix = 10
        if len(argv) >= 3 and argv[2]:
            radix = self._numeric(argv[0], argv[2])
            if radix is None:
                return None
        if radix < 1 or radix > 36:
            self.warn(f"radix {radix} in builtin `{argv[0]}' out of range")
            return None
        width = 1
        if len(argv) >= 4:
            width = self._numeric(argv[0], argv[3])
            if width is None:
                return None
        if width < 0:
            self.warn(f"negative width to builtin `{argv[0]}'")
            return None
        value = 0
        if not expr:
            self.warn(f"empty string treated as 0 in builtin `{argv[0]}'")
        else:
            er, value = evaluate(expr)
            if er != NO_ERROR:
                self.warn(_EVAL_MESSAGES.get(er, "bad expression in eval: ") + expr)
                return None
        if radix == 1:
            sign = "-" if value < 0 else ""
            count = -value if value < 0 else value
            return sign + "0" * max(0, width - count) + "1" * count
        digits = _ntoa(value, radix)
        sign = ""
        if digits.startswith("-"):
            sign, digits = "-", digits[1:]
        return sign + "0" * max(0, width - len(digits)) + digits

    def _b_incr(self, argv):
        if self._bad_argc(argv, 2, 2):
            return None
        value = self._numeric(argv[0], argv[1])
        return None if value is None else str(_i32(value + 1))

    def _b_decr(self, argv):
        if self._bad_argc(argv, 2, 2):
            return None
        value = self._numeric(argv[0], argv[1])
        return None if value is None else str(_i32(value - 1))

    def _b_len(self, argv):
        if self._bad_argc(argv, 2, 2):
            return None
        return str(len(argv[1]))

    def _b_index(self, argv):
        if self._bad_argc(argv, 3, 3):
            return "0" if len(argv) == 2 else None
        return str(argv[1].find(argv[2]))

    def _b_substr(self, argv):
        if self._bad_argc(argv, 3, 4):
            return argv[1] if len(argv) == 2 else None
        text = argv[1]
        start = self._numeric(argv[0], argv[2])
        if start is None:
            return None
        length = len(text)
        if len(argv) >= 4:
            length = self._numeric(argv[0], argv[3])
            if length is None:
                return None
        if start < 0 or length <= 0 or start >= len(text):
            return None
        return text[start : start + length]

    def _b_translit(self, argv):
        data = argv[1] if len(argv) >= 2 else ""
        source = argv[2] if len(argv) >= 3 else ""
        if self._bad_argc(argv, 3, 4) or not data or not source:
            return data if len(argv) >= 2 else None
        target = argv[3] if len(argv) >= 4 else ""
        if "-" in target:
            target = _expand_ranges(target)
        if len(source) >= 3 and "-" in source:
            source = _expand_ranges(source)
        mapping: dict[str, str | None] = {}
        for i, ch in enumerate(source):
            if ch not in mapping:
                mapping[ch] = target[i] if i < len(target) else None
        out = []
        for ch in data:
            if ch not in mapping:
                out.append(ch)
            elif mapping[ch] is not None:
                out.append(mapping[ch])
        return "".join(out)

    def _regex(self, pattern: str):
        try:
            return compile_regex(pattern)
        except re.error as error:
            self.warn(f"bad regular expression: `{pattern}': {error}")
            return None

    def _b_regexp(self, argv):
        if self._bad_argc(argv, 3, 4):
            return "0" if len(argv) == 2 else None
        pattern = self._regex(argv[2])
        if pattern is None:
            return None
        match = regex_search(pattern, argv[1])
        if len(argv) == 3:
            return str(match.start() if match else -1)
        if match is None:
            return None
        return substitute(match, argv[3], self.warn)

    def _b_patsubst(self, argv):
        if self._bad_argc(argv, 3, 4):
            return argv[1] if len(argv) == 2 else None
        pattern = self._regex(argv[2])
        if pattern is None:
            return None
        victim = argv[1]
        repl = argv[3] if len(argv) >= 4 else ""
        out: list[str] = []
        offset = 0
        length = len(victim)
        while offset <= length:
            match = regex_search(pattern, victim, offset)
            if match is None:
                out.append(victim[offset:])
                break
            out.append(victim[offset : match.start()])
            out.append(substitute(match, repl, self.warn))
            offset = match.end()
            if match.start() == match.end():
                if offset < length:
                    out.append(victim[offset])
                offset += 1
        return "".join(out)

    def _b_format(self, argv):
        if self._bad_argc(argv, 2, -1):
            return None
        return format_args(argv[1], argv[2:])

    def _include(self, argv, silent: bool):
        if self._bad_argc(argv, 2, 2):
            return None
        path = self.find_file(argv[1])
        if path is None:
            if not silent:
                self.warn(f"cannot open `{argv[1]}': No such file or directory")
                self.exit_code = 1
            return None
        self.frames.append(_Frame(read_source(path), path.as_posix()))
        return None

    def _b_include(self, argv):
        return self._include(argv, False)

    def _b_sinclude(self, argv):
        return self._include(argv, True)

    def _b_m4exit(self, argv):
        self._bad_argc(argv, 1, 2)
        code = 0
        if len(argv) >= 2:
            value = self._numeric(argv[0], argv[1])
            code = 1 if value is None else value
        if code < 0 or code > 255:
            self.warn(f"exit status out of range: `{code}'")
            code = 1
        raise _Exit(code)

    def _b_m4wrap(self, argv):
        if self._bad_argc(argv, 2, -1):
            return None
        self.wrapups.append(" ".join(argv[1:]))
        return None

    def _b_maketemp(self, argv):
        if self._bad_argc(argv, 2, 2):
            return None
        return self.lquote + argv[1] + self.rquote

    def _b_builtin(self, argv):
        if self._bad_argc(argv, 2, -1):
            return None
        name = argv[1]
        if not isinstance(name, str):
            self.warn(f"Warning: {argv[0]}: invalid macro name ignored")
            return None
        builtin = self._builtins.get(name)
        if builtin is None:
            self.warn(f"undefined builtin `{name}'")
            return None
        args = [name] + argv[2:]
        if not builtin.groks:
            args = [a if isinstance(a, str) else "" for a in args]
        return builtin.func(args)

    def _b_indir(self, argv):
        if self._bad_argc(argv, 2, -1):
            return None
        name = argv[1]
        if not isinstance(name, str):
            self.warn(f"Warning: {argv[0]}: invalid macro name ignored")
            return None
        defn = self._lookup(name)
        if defn is None:
            self.warn(f"undefined macro `{name}'")
            return None
        return self._call(name, defn, [name] + argv[2:])

    # ---------------------------------------------------------------- driver

    def run(self, sources) -> str:
        """Expand a sequence of (text, filename-or-None) inputs as one m4 run."""
        try:
            for text, filename in sources:
                file = Path(filename).as_posix() if filename is not None else None
                self.frames.append(_Frame(text, file))
                self._expand_input()
            while self.wrapups:
                pending, self.wrapups = self.wrapups, []
                for text in reversed(pending):
                    self.frames.append(_Frame(text))
                self._expand_input()
        except _Exit as done:
            self.exit_code = done.code
        out = list(self.diversions.get(0, []))
        for number in sorted(k for k in self.diversions if k > 0):
            out.extend(self.diversions[number])
        return "".join(out)

    def expand(self, source: str, filename=None) -> str:
        if filename is not None:
            self.base_dir = Path(filename).resolve().parent
        return self.run([(source, filename)])

    def expand_file(self, filename) -> str:
        path = Path(filename)
        try:
            text = read_source(path)
        except OSError as error:
            raise M4Error(f"cannot open `{path}': {error}") from error
        return self.expand(text, path)


def expand(source: str, include_dirs=None, filename=None) -> str:
    return M4Lite(include_dirs).expand(source, filename)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pym4", description="GNU m4 compatible macro processor written in Python.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-I", "--include", dest="include_dirs", action="append", type=Path, default=[])
    parser.add_argument("-D", "--define", dest="defines", action="append", default=[])
    parser.add_argument("-U", "--undefine", dest="undefines", action="append", default=[])
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("-Q", "--quiet", action="store_true", help="suppress warnings")
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args(argv)

    m4 = M4Lite(args.include_dirs, warn=None if args.quiet else (lambda m: sys.stderr.write(m + "\n")))
    for item in args.defines:
        name, _, value = item.partition("=")
        m4.define(name, value)
    for name in args.undefines:
        m4.undefine(name)
    try:
        sources = []
        for path in args.files:
            if str(path) == "-":
                sources.append((sys.stdin.read(), None))
            else:
                sources.append((read_source(path), path))
        if not args.files:
            sources.append((sys.stdin.read(), None))
        else:
            m4.base_dir = Path(args.files[0]).resolve().parent
        output = m4.run(sources)
    except (OSError, M4Error) as error:
        sys.stderr.write(f"pym4: {error}\n")
        return 1
    # write the expansion as is: Windows newline translation would turn CRLF input into CR CR LF
    if args.output:
        args.output.write_text(output, encoding="utf-8", newline="")
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(newline="")
        sys.stdout.write(output)
    return m4.exit_code


if __name__ == "__main__":
    raise SystemExit(main())









