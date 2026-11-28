# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Compute kernel worker: runs as an isolated subprocess of ``compute()`` (ADR-039 D2/D3).

It is launched BY FILE PATH (``python -I compute_worker.py``), imports nothing from axiom, and
therefore resolves the same interpreter and the same SymPy no matter which checkout or install the
caller came from. The caller sends one JSON request on stdin and reads one JSON line on stdout.
Nothing is pickled in either direction: the parent never deserializes output from a process that
evaluates user-influenced math.

Defence in depth, because the input is an LLM's or a person's expression:

1. **Syntax whitelist before evaluation.** SymPy-syntax input is parsed to a Python AST and checked
   node by node. Only arithmetic, a fixed set of math functions, numbers, tuples (for limits and
   sums), and symbol names are allowed. No attribute access, subscripts, lambdas, comprehensions,
   keywords, or names that shadow a builtin. A name is a symbol or a known function, never a lookup.
2. **Bounds.** Input length, node count, integer size, and a power-tower estimate are refused before
   SymPy sees them, so ``9**9**9**9`` is a validation error and not a memory exhaustion.
3. **No builtins in the evaluation namespace**, and LaTeX goes through SymPy's parser, which does
   not evaluate Python.
4. **OS limits:** CPU seconds, no file writes, no core dumps, bounded descriptors, and an address-space
   cap where the platform enforces one. The parent also enforces wall-clock and kills the process group.

Modes: ``symbolic`` (simplify), ``numeric`` (double precision), ``arbitrary`` (mpmath digits).
"""

from __future__ import annotations

import ast
import builtins
import json
import math
import re
import sys
import traceback

MAX_REQUEST_BYTES = 64 * 1024
MAX_EXPR_CHARS = 2000
MAX_AST_NODES = 600
MAX_INT_DIGITS = 100
MAX_LOG10_RESULT = 5000.0  # a numeric power estimated beyond 10**5000 is refused
MAX_PRECISION = 1000
MAX_VALUE_CHARS = 20000

_SYMBOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")

#: Commands that legitimately become symbols (Greek letters and a few physics letters).
SYMBOL_MACROS = frozenset(
    [
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "varepsilon",
        "zeta",
        "eta",
        "theta",
        "vartheta",
        "iota",
        "kappa",
        "lambda",
        "mu",
        "nu",
        "xi",
        "omicron",
        "pi",
        "rho",
        "varrho",
        "sigma",
        "varsigma",
        "tau",
        "upsilon",
        "phi",
        "varphi",
        "chi",
        "psi",
        "omega",
        "Gamma",
        "Delta",
        "Theta",
        "Lambda",
        "Xi",
        "Pi",
        "Sigma",
        "Upsilon",
        "Phi",
        "Psi",
        "Omega",
        "hbar",
        "ell",
    ]
)
_MACRO = re.compile(r"\\([A-Za-z]+)")


def unknown_macros(text: str, expr) -> list[str]:
    """Commands the LaTeX backend did not understand but turned into a variable of the same name.

    The ANTLR grammar reads an unknown backslash command as a symbol, so an unrecognised command parses
    to a variable of that name. A checked parse must not call that a success. Shared with the
    axiom-side parser (it imports this), so the worker and the parent can never disagree."""
    try:
        symbols = {str(s) for s in expr.free_symbols}
    except Exception:  # noqa: BLE001
        return []
    return sorted({m for m in _MACRO.findall(text) if m in symbols and m not in SYMBOL_MACROS})


_BUILTIN_NAMES = frozenset(dir(builtins)) | {"__builtins__"}

_FUNCTION_NAMES = [
    "sin",
    "cos",
    "tan",
    "cot",
    "sec",
    "csc",
    "asin",
    "acos",
    "atan",
    "atan2",
    "sinh",
    "cosh",
    "tanh",
    "asinh",
    "acosh",
    "atanh",
    "sqrt",
    "cbrt",
    "exp",
    "log",
    "ln",
    "Abs",
    "floor",
    "ceiling",
    "sign",
    "factorial",
    "gamma",
    "binomial",
    "gcd",
    "lcm",
    "re",
    "im",
    "conjugate",
    "simplify",
    "expand",
    "factor",
    "diff",
    "integrate",
    "limit",
    "series",
    "summation",
    "product",
    "Min",
    "Max",
]
_CONSTANT_NAMES = ("pi", "E", "I", "oo")
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
_ALLOWED_UNARY = (ast.UAdd, ast.USub)


class Refusal(Exception):
    """The input is not acceptable. ``kind`` is the error class reported to the caller."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


# --------------------------------------------------------------------------- limits


def _apply_limits(cpu_seconds: int) -> list[str]:
    """Apply OS resource limits; return the names of those the platform refused."""
    refused: list[str] = []
    try:
        import resource
    except ImportError:  # Windows: wall-clock in the parent is the only bound
        return ["all"]

    def cap(name: str, soft: int, hard: int | None = None) -> None:
        try:
            limit = getattr(resource, name)
            resource.setrlimit(limit, (soft, soft if hard is None else hard))
        except (ValueError, OSError, AttributeError):
            refused.append(name)

    cap("RLIMIT_CPU", cpu_seconds, cpu_seconds + 1)
    cap("RLIMIT_CORE", 0)
    cap("RLIMIT_FSIZE", 0)  # no file may be written
    cap("RLIMIT_NOFILE", 256)
    if sys.platform.startswith("linux"):
        cap("RLIMIT_AS", 3 * 1024**3)
    return refused


# --------------------------------------------------------------------------- validation


def _numeric_log10(node: ast.AST) -> float | None:
    """log10 of a purely numeric subtree's magnitude, or None when it is not purely numeric.

    This is an estimate with floats, used only to refuse an expression whose result could not be
    honest input (a tower of powers of literals)."""

    def val(n: ast.AST) -> float | None:
        if (
            isinstance(n, ast.Constant)
            and isinstance(n.value, (int, float))
            and not isinstance(n.value, bool)
        ):
            return float(n.value)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            v = val(n.operand)
            return None if v is None else (-v if isinstance(n.op, ast.USub) else v)
        if isinstance(n, ast.BinOp) and isinstance(n.op, _ALLOWED_BINOPS):
            a, b = val(n.left), val(n.right)
            if a is None or b is None:
                return None
            try:
                if isinstance(n.op, ast.Add):
                    return a + b
                if isinstance(n.op, ast.Sub):
                    return a - b
                if isinstance(n.op, ast.Mult):
                    return a * b
                if isinstance(n.op, ast.Div):
                    return a / b if b else math.inf
                return math.inf if (a != 0 and b * math.log10(abs(a)) > MAX_LOG10_RESULT) else a**b
            except (OverflowError, ValueError, ZeroDivisionError):
                return math.inf
        return None

    v = val(node)
    if v is None:
        return None
    if v == 0:
        return 0.0
    return math.inf if math.isinf(v) or math.isnan(v) else math.log10(abs(v))


def validate_sympy_syntax(src: str) -> ast.Expression:
    """Parse and whitelist ``src``. Raises ``Refusal('ValidationError', ...)`` on anything else."""
    if len(src) > MAX_EXPR_CHARS:
        raise Refusal("ValidationError", f"expression longer than {MAX_EXPR_CHARS} characters")
    try:
        tree = ast.parse(src.strip(), mode="eval")
    except SyntaxError as exc:
        raise Refusal("ParseError", f"could not parse {src[:80]!r}: {exc.msg}") from exc
    except (ValueError, RecursionError, MemoryError) as exc:
        raise Refusal("ValidationError", f"expression refused: {type(exc).__name__}") from exc

    nodes = 0
    functions = set(_FUNCTION_NAMES)
    constants = set(_CONSTANT_NAMES)
    call_funcs: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            call_funcs.add(id(node.func))

    for node in ast.walk(tree):
        nodes += 1
        if nodes > MAX_AST_NODES:
            raise Refusal("ValidationError", f"expression has more than {MAX_AST_NODES} parts")
        if isinstance(node, (ast.Expression, ast.Load, ast.Tuple)):
            continue
        if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
            continue
        if isinstance(node, (*_ALLOWED_BINOPS, *_ALLOWED_UNARY)):
            continue
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, _ALLOWED_UNARY):
            continue
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise Refusal("ValidationError", f"constant {v!r} is not a number")
            if isinstance(v, int) and len(str(abs(v))) > MAX_INT_DIGITS:
                raise Refusal("ValidationError", f"integer longer than {MAX_INT_DIGITS} digits")
            if isinstance(v, float) and not math.isfinite(v):
                raise Refusal("ValidationError", "non-finite number")
            continue
        if isinstance(node, ast.Name):
            name = node.id
            if name in _BUILTIN_NAMES or name.startswith("_"):
                raise Refusal(
                    "ValidationError", f"name {name!r} is not allowed (it shadows a builtin)"
                )
            if id(node) in call_funcs:
                if name not in functions:
                    raise Refusal("ValidationError", f"function {name!r} is not allowed")
            elif name in constants:
                pass
            elif name in functions:
                raise Refusal(
                    "ValidationError",
                    f"{name!r} is a function; call it, or choose another symbol name",
                )
            elif not _SYMBOL_RE.match(name):
                raise Refusal("ValidationError", f"{name!r} is not a valid symbol name")
            continue
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise Refusal("ValidationError", "only named math functions may be called")
            if node.keywords:
                raise Refusal("ValidationError", "keyword arguments are not allowed")
            continue
        raise Refusal("ValidationError", f"{type(node).__name__} is not allowed in an expression")

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            est = _numeric_log10(node)
            if est is not None and est > MAX_LOG10_RESULT:
                raise Refusal(
                    "ValidationError", "a power of literals that large is not honest input"
                )
    return tree


# --------------------------------------------------------------------------- parsing


def _looks_like_latex(text: str) -> bool:
    return "\\" in text or "^{" in text or "_{" in text


def parse_expression(text: str):
    """Return ``(sympy_expr, parser_name)`` or raise ``Refusal``."""
    if len(text) > MAX_EXPR_CHARS:
        raise Refusal("ValidationError", f"expression longer than {MAX_EXPR_CHARS} characters")
    if _looks_like_latex(text):
        try:
            from sympy.parsing.latex import parse_latex
        except Exception as exc:  # noqa: BLE001
            raise Refusal(
                "ParserUnavailable", f"sympy's LaTeX parser could not be imported: {exc}"
            ) from exc
        try:
            expr = parse_latex(text, backend="antlr")
            unknown = unknown_macros(text, expr)
            if unknown:
                raise Refusal(
                    "ParseError", "unknown command(s): " + ", ".join("\\" + m for m in unknown)
                )
            return expr, "latex/antlr"
        except Refusal:
            raise
        except ImportError as exc:
            raise Refusal(
                "ParserUnavailable",
                "the LaTeX parser needs antlr4-python3-runtime==4.11.*; install the math extra "
                f"(pip install 'axiom-os-lm[math]'): {exc}",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise Refusal(
                "ParseError", f"could not parse LaTeX {text[:80]!r}: {type(exc).__name__}"
            ) from exc

    validate_sympy_syntax(text)
    import sympy
    from sympy.parsing.sympy_parser import parse_expr, standard_transformations

    namespace = {
        n: getattr(sympy, n) for n in _FUNCTION_NAMES + list(_CONSTANT_NAMES) if hasattr(sympy, n)
    }
    namespace["ln"] = sympy.log
    namespace["Abs"] = sympy.Abs
    namespace["cbrt"] = sympy.cbrt
    namespace["Min"], namespace["Max"] = sympy.Min, sympy.Max
    scope = {
        "__builtins__": {},
        "Integer": sympy.Integer,
        "Float": sympy.Float,
        "Rational": sympy.Rational,
        "Symbol": sympy.Symbol,
    }
    try:
        expr = parse_expr(
            text.strip(),
            local_dict=namespace,
            global_dict=scope,
            transformations=standard_transformations,
            evaluate=True,
        )
    except Refusal:
        raise
    except Exception as exc:  # noqa: BLE001
        raise Refusal(
            "ParseError", f"could not evaluate {text[:80]!r}: {type(exc).__name__}: {exc}"
        ) from exc
    return expr, "sympy-syntax"


# --------------------------------------------------------------------------- compute


def run(request: dict) -> dict:
    import sympy

    mode = request.get("mode", "symbolic")
    precision = max(1, min(int(request.get("precision", 50)), MAX_PRECISION))
    expr, parser = parse_expression(str(request.get("latex", "")))

    if mode == "symbolic":
        out = sympy.simplify(expr)
        value, trail = str(out), sympy.srepr(out)
    elif mode == "numeric":
        out = expr.evalf()
        try:
            f = float(out)
            value, trail = repr(f), f"float(({sympy.srepr(expr)}).evalf())"
        except (TypeError, ValueError):
            value, trail = str(out), f"({sympy.srepr(expr)}).evalf()"
    elif mode == "arbitrary":
        out = expr.evalf(precision)
        value, trail = str(out), f"({sympy.srepr(expr)}).evalf({precision})"
    else:
        raise Refusal("UnknownMode", f"unknown mode {mode!r}")
    if len(value) > MAX_VALUE_CHARS:
        raise Refusal("ResourceLimit", f"the result is longer than {MAX_VALUE_CHARS} characters")
    return {
        "success": True,
        "value_repr": value,
        "ast_trail": trail,
        "extra": {
            "parser": parser,
            "sympy": sympy.__version__,
            "free_symbols": sorted(str(s) for s in getattr(expr, "free_symbols", ())),
        },
    }


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        payload = {
            "success": False,
            "error_class": "ValidationError",
            "error_message": "request too large",
        }
        sys.stdout.write(json.dumps(payload) + "\n")
        return 0
    try:
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict):
            raise ValueError("request is not an object")
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(
            json.dumps(
                {"success": False, "error_class": "InputDecodeError", "error_message": str(exc)}
            )
            + "\n"
        )
        return 0

    cpu = max(1, min(int(request.get("cpu_seconds", 30)), 600)) + 1
    try:
        import sympy  # noqa: F401  (import before limits: loading SymPy is not the user's computation)
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(
            json.dumps(
                {
                    "success": False,
                    "error_class": "DependencyMissing",
                    "error_message": f"sympy is not importable: {exc}",
                }
            )
            + "\n"
        )
        return 0
    refused = _apply_limits(cpu)
    try:
        result = run(request)
        if refused:
            result["extra"]["limits_not_enforced"] = refused
    except Refusal as exc:
        result = {"success": False, "error_class": exc.kind, "error_message": str(exc)}
    except MemoryError:
        result = {
            "success": False,
            "error_class": "ResourceLimit",
            "error_message": "memory limit reached",
        }
    except RecursionError:
        result = {
            "success": False,
            "error_class": "ResourceLimit",
            "error_message": "expression too deeply nested",
        }
    except Exception as exc:  # noqa: BLE001
        result = {
            "success": False,
            "error_class": type(exc).__name__,
            "error_message": str(exc)[:500],
            "extra": {"traceback": traceback.format_exc()[-500:]},
        }
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
