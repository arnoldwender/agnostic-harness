#!/usr/bin/env python3
"""The Agnostic Harness gate: nothing that swallows an error gets in quietly.

    python3 gate/fail_open.py                    # diff against origin/main
    python3 gate/fail_open.py --base HEAD~1
    python3 gate/fail_open.py --files a.py b.ts
    python3 gate/fail_open.py --sarif out.json

Exit codes are the contract shared by the conduct-harness family:

    0   no findings
    1   findings - added code swallows an error
    2   the gate itself failed

The third one is this gate's own thesis turned on itself. A checker that
returns 1 when it crashed reads as "I found something"; one that returns 0
reads as "clean" and fails OPEN - which is the exact defect this gate exists to
catch. So it distinguishes its own failure from its verdict, and when it cannot
read its input it says so loudly instead of reporting a green run.

WHAT THIS GATE IS FOR
---------------------
The Sentinel's falsifier is "a gate passes only because a check was disabled,
skipped, or loosened". The cheapest way to disable a check is not to delete it -
it is to catch what it raises and carry on. `except Exception: pass`,
`catch {}`, `|| true`, `continue-on-error: true`, and the quietest of the
family, a default value that means "everything is fine" for data that never
arrived.

Every one of those inverts the dichotomy of control the codex rests on: what
you do not control must fail LOUDLY, so that the part you do control - the
response - can happen at all. An error nobody sees is not an error anybody
handled. The Sentinel calls this the cheap rescue, and it is cheap precisely
because it is invisible in a diff review: the line reads like error handling.

ONLY ADDED LINES
----------------
The gate reads the diff, never the whole tree. A repo adopting it should not
have to fix its history before its next commit can land, and a gate that fires
on code nobody touched gets ripped out in a week - and deserves to be. The same
reasoning drives the two escape hatches: a `fail-open-ok` marker comment for the
one line where swallowing really is correct, and `.conduct/fail-open-allow.txt`
for a path or pattern that is permanently exempt. A gate with no way to say
"yes, on purpose" is a gate people route around.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

# The root is overridable so the tests can point the gate at a scratch repo. A
# checker that can only ever run on itself cannot be shown to work: the only way
# to prove a check has teeth is to hand it a repo with the defect planted and
# watch it go red.
ROOT = Path(os.environ.get("HARNESS_ROOT") or Path(__file__).resolve().parent.parent)
ALLOWLIST = ROOT / ".conduct" / "fail-open-allow.txt"

# One token, understood in every language's comment syntax, because the gate
# looks at the raw line and not at a parsed comment node.
MARKER = "fail-open-ok"

PY_EXT = {".py", ".pyi"}
JS_EXT = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
SH_EXT = {".sh", ".bash", ".zsh", ".ksh"}
YAML_EXT = {".yml", ".yaml"}


@dataclass
class Finding:
    check: str
    message: str
    path: str
    line: int = 0
    text: str = ""


class GateError(RuntimeError):
    """The gate could not do its job. Never a verdict - always exit 2."""


# --- diff ---------------------------------------------------------------------

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _git(*args: str) -> str:
    proc = subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GateError(f"git {' '.join(args)} failed with {proc.returncode}: "
                        f"{proc.stderr.strip()[:200]}")
    return proc.stdout


def _rev(ref: str) -> str | None:
    """The commit a ref names, or None. Deliberately not an exception: callers
    need to tell 'this ref is absent' from 'git itself is broken'."""
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True, text=True, check=False)
    return proc.stdout.strip() or None


def _unquote(path: str) -> str:
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        return path[1:-1].encode("utf-8").decode("unicode_escape")
    return path


def added_from_diff(base: str) -> dict[str, set[int]]:
    """Line numbers ADDED on the new side of the diff, per path.

    The range is merge-base(base, HEAD) .. working tree, plus every untracked
    file counted whole. Two-dot against the working tree rather than three-dot
    against HEAD so that uncommitted work is judged too: a gate you can dodge by
    not committing yet is theatre.
    """
    if _rev("HEAD") is None:
        raise GateError("this repository has no commits - there is no diff to read")
    base_sha = _rev(base)
    if base_sha is None:
        raise GateError(
            f"base ref {base!r} does not resolve to a commit. In CI, fetch enough "
            f"history (actions/checkout with fetch-depth: 0) or pass --base <sha>")

    merge = subprocess.run(["git", "-C", str(ROOT), "merge-base", base_sha, "HEAD"],
                           capture_output=True, text=True, check=False)
    start = merge.stdout.strip() if merge.returncode == 0 else base_sha

    diff = _git("-c", "core.quotepath=false", "diff", "--unified=0", "--no-color",
                "--no-renames", "--diff-filter=d", start)

    added: dict[str, set[int]] = {}
    path: str | None = None
    lineno = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = _unquote(raw[4:].strip())
            path = None if target == "/dev/null" else re.sub(r"^b/", "", target)
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git"):
            continue
        if raw.startswith("@@"):
            m = HUNK.match(raw)
            if m:
                lineno = int(m.group(1))
            continue
        if path is None:
            continue
        if raw.startswith("+"):
            added.setdefault(path, set()).add(lineno)
            lineno += 1
        elif raw.startswith(" "):
            lineno += 1

    # An untracked file is 100% new code. `git diff` never shows it, so a gate
    # that only read the diff would wave through a whole new module.
    for rel in _git("ls-files", "--others", "--exclude-standard").splitlines():
        rel = _unquote(rel.strip())
        if not rel:
            continue
        if language_of(rel) is None:
            # Still listed, with no lines, so it shows up in the "skipped" count.
            # A file the gate silently never mentions is indistinguishable from a
            # file it checked and cleared.
            added.setdefault(rel, set())
            continue
        text = _try_read(rel)
        if text is None:
            added.setdefault(rel, set())
            continue
        added.setdefault(rel, set()).update(range(1, len(text.splitlines()) + 1))
    return added


# --- files --------------------------------------------------------------------

def language_of(rel: str) -> str | None:
    ext = Path(rel).suffix.lower()
    if ext in PY_EXT:
        return "python"
    if ext in JS_EXT:
        return "javascript"
    if ext in SH_EXT:
        return "shell"
    if ext in YAML_EXT:
        return "yaml"
    if not ext:
        head = _first_line(ROOT / rel)
        if re.match(r"^#!.*\b(?:ba|z|k|da)?sh\b", head):
            return "shell"
    return None


def _first_line(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.readline()
    except OSError:
        return ""


def _try_read(rel: str) -> str | None:
    path = ROOT / rel
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _relative(raw: str) -> str:
    p = Path(raw)
    if not p.is_absolute():
        return raw
    try:
        return str(p.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        raise GateError(f"{raw} is outside HARNESS_ROOT ({ROOT})") from None


def _line(lines: list[str], n: int) -> str:
    return lines[n - 1].strip() if 1 <= n <= len(lines) else ""


def _lineno(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _touches(added: set[int], start: int, end: int | None) -> bool:
    return any(n in added for n in range(start, (end or start) + 1))


# --- suppression --------------------------------------------------------------

def _comment_view_hash(lines: list[str]) -> list[str]:
    """Each line reduced to its `#` comment; everything else blanked out."""
    view = []
    for line in lines:
        start = _hash_comment_start(line)
        view.append(" " * len(line) if start is None else " " * start + line[start:])
    return view


def _comment_view_python(text: str) -> list[str] | None:
    """Each line reduced to its `#` comment, via the real tokenizer.

    None when the file cannot be tokenised, and the caller then suppresses
    nothing at all. Guessing here would be the wrong way to be wrong: an
    over-eager marker silences checks, which is the failure this gate is named
    for. When it cannot tell, it stays loud.
    """
    import io
    import tokenize

    view = [" " * len(line) for line in text.splitlines()]
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type != tokenize.COMMENT:
                continue
            row, col = tok.start
            if 1 <= row <= len(view):
                view[row - 1] = " " * col + tok.string
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return None
    return view


def comment_view(lang: str, text: str) -> list[str]:
    """The file with everything that is not a comment blanked out.

    The marker only counts as a directive when it is genuinely commented out.
    Two real defects made this necessary, both found by planting them in this
    repo: prose in this file's own documentation naming the region markers
    opened a region that ran to the end of the file, and a shell fixture inside
    a Python string in the test suite registered as a directive. A suppression
    mechanism you can trigger by *talking about it* is the same failure the gate
    is named for, one layer in.
    """
    if lang == "python":
        view = _comment_view_python(text)
        if view is not None:
            return view
        return [" " * len(line) for line in text.splitlines()]
    if lang == "javascript":
        return mask_code(text, keep_comments=True).splitlines()
    return _comment_view_hash(text.splitlines())


def _commented(view_line: str, token: str) -> bool:
    """True when `token` sits inside a comment on this line."""
    return re.search(r"(?:#|//|/\*|<!--|^\s*\*)[^\n]*?" + re.escape(token),
                     view_line) is not None


def marked_lines(view: list[str], lines: list[str], rel: str,
                 findings: list[Finding]) -> set[int]:
    """Lines the author has explicitly accepted, 1-based.

    Three shapes, so the escape hatch fits the code rather than the reverse:
    the marker on the offending line, the marker on the line above it, and a
    begin/end region for a block of cleanup where every failure really is
    optional. A line carrying BOTH region tokens is documentation about them,
    not a directive.

    An unclosed region is reported and suppresses nothing. Letting it run to
    the end of the file would be a silent kill switch for the whole gate, which
    is precisely the shape of defect this gate exists to catch.
    """
    out: set[int] = set()
    pending: set[int] = set()
    open_at: int | None = None
    for n, raw in enumerate(view, 1):
        begins = _commented(raw, f"{MARKER}:begin")
        ends = _commented(raw, f"{MARKER}:end")
        if begins and ends:
            continue
        if begins:
            if open_at is None:
                open_at = n
            pending.add(n)
            continue
        if ends:
            if open_at is not None:
                pending.add(n)
                out |= pending
                open_at, pending = None, set()
            continue
        if open_at is not None:
            pending.add(n)
            continue
        if _commented(raw, MARKER):
            out.add(n)
            out.add(n + 1)
    if open_at is not None:
        findings.append(Finding(
            "unclosed-marker",
            f"`{MARKER}:begin` is never closed with `{MARKER}:end` - an unbounded "
            f"suppression region would switch the gate off for the rest of the "
            f"file, so it suppresses nothing",
            rel, open_at, _line(lines, open_at)))
    return out


def load_allowlist(findings: list[Finding]) -> list[re.Pattern[str]]:
    """Regexes from .conduct/fail-open-allow.txt. One per line, `#` comments."""
    patterns: list[re.Pattern[str]] = []
    if not ALLOWLIST.is_file():
        return patterns
    rel = str(ALLOWLIST.relative_to(ROOT))
    for n, raw in enumerate(ALLOWLIST.read_text(encoding="utf-8").splitlines(), 1):
        line = re.sub(r"(?:^|\s)#.*$", "", raw).strip()
        if not line:
            continue
        try:
            patterns.append(re.compile(line))
        except re.error as exc:
            findings.append(Finding(
                "bad-allowlist",
                f"line {n} is not a valid regex ({exc}) - a broken allowlist is "
                f"reported, never silently ignored", rel, n, raw.strip()))
    return patterns


def _allowed(finding: Finding, patterns: Iterable[re.Pattern[str]]) -> bool:
    if finding.check == "bad-allowlist":
        return False
    return any(p.search(finding.path) or (finding.text and p.search(finding.text))
               for p in patterns)


# --- python -------------------------------------------------------------------

NOOP_STMT = (ast.Pass, ast.Continue, ast.Break)


def _is_noop_body(body: list[ast.stmt]) -> bool:
    """True when the block does nothing an observer could ever notice."""
    for st in body:
        if isinstance(st, NOOP_STMT):
            continue
        if (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)
                and (st.value.value is Ellipsis or isinstance(st.value.value, str))):
            continue        # `...` or a docstring: decoration, not handling
        return False
    return True


def _stmts_here(body: Iterable[ast.stmt]) -> Iterator[ast.stmt]:
    """Statements in this block and its nested blocks, but NOT inside a nested
    def or class - a `return` in an inner function belongs to that function."""
    for st in body:
        yield st
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody", "handlers"):
            yield from _stmts_here(getattr(st, field, None) or [])


def check_swallowed_exception(tree: ast.AST, rel: str, added: set[int],
                              findings: list[Finding], lines: list[str]) -> None:
    """`except: pass` and its family - the error is caught and then forgotten."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not _is_noop_body(node.body):
            continue
        if not _touches(added, node.lineno, node.end_lineno):
            continue
        caught = ast.unparse(node.type).strip() if node.type else ""
        findings.append(Finding(
            "swallowed-exception",
            f"`except {caught or '<bare>'}` has a body that does nothing - the "
            f"failure is caught and dropped. Log it, re-raise it, count it, or "
            f"return an explicit error value.",
            rel, node.lineno, _line(lines, node.lineno)))


def check_return_in_finally(tree: ast.AST, rel: str, added: set[int],
                            findings: list[Finding], lines: list[str]) -> None:
    """A `return` in `finally:` discards the exception that is still in flight."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Try, ast.TryStar)) or not node.finalbody:
            continue
        for st in _stmts_here(node.finalbody):
            if not isinstance(st, (ast.Return, ast.Break, ast.Continue)):
                continue
            if not _touches(added, st.lineno, st.end_lineno):
                continue
            kw = type(st).__name__.lower()
            findings.append(Finding(
                "return-in-finally",
                f"`{kw}` inside `finally:` throws away any exception still in "
                f"flight - the try block can blow up and the caller sees a normal "
                f"return instead",
                rel, st.lineno, _line(lines, st.lineno)))


# The most expensive pattern of the set, and the only one that looks like
# ordinary defensive code: when the data never arrived, the default must never
# be the value that means "all is well".
GREEN_WORDS = {
    "ok", "okay", "valid", "validated", "healthy", "health", "success",
    "successful", "succeeded", "passed", "passing", "verified", "allowed",
    "permitted", "authorized", "authorised", "authenticated", "approved",
    "safe", "secure", "clean", "status", "verdict", "result", "outcome",
}
ERROR_WORDS = {
    "error", "errors", "failure", "failures", "failed", "warning", "warnings",
    "violation", "violations", "issue", "issues", "problem", "problems",
    "finding", "findings", "defect", "defects", "blocker", "blockers",
    "exception", "exceptions",
}
GREEN_STRINGS = {
    "ok", "okay", "success", "successful", "succeeded", "pass", "passed",
    "passing", "valid", "healthy", "good", "clean", "green", "yes", "true",
    "fine", "up", "ready", "approved", "allowed", "safe", "200",
}


def _key_words(key: str) -> set[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key)
    return {w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w}


def _means_all_is_well(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant):
        v = node.value
        if v is True:
            return "True"
        if isinstance(v, str) and v.strip().lower() in GREEN_STRINGS:
            return repr(v)
    return None


def _means_no_problems(node: ast.AST) -> str | None:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and not node.elts:
        return "an empty collection"
    if isinstance(node, ast.Dict) and not node.keys:
        return "an empty mapping"
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool):
            return "False" if v is False else None
        if v is None:
            return "None"
        if isinstance(v, int) and v == 0:
            return "0"
        if v == "":
            return "an empty string"
    return None


def _verdict_default(key: str, default: ast.AST) -> tuple[str, str] | None:
    words = _key_words(key)
    if words & GREEN_WORDS:
        got = _means_all_is_well(default)
        if got:
            return got, "means the check passed"
    if words & ERROR_WORDS:
        got = _means_no_problems(default)
        if got:
            return got, "means there was nothing wrong"
    return None


def _lookup_with_default(node: ast.AST) -> tuple[str, ast.AST] | None:
    """(key, default) for the lookup shapes that carry a fallback value."""
    if isinstance(node, ast.Call):
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr in ("get", "pop")
                and len(node.args) == 2 and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            return node.args[0].value, node.args[1]
        if (isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) == 3
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            return node.args[1].value, node.args[2]
        if (isinstance(func, ast.Name) and func.id == "getenv" and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            return node.args[0].value, node.args[1]
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and len(node.values) == 2:
        left, right = node.values
        if (isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute)
                and left.func.attr == "get" and len(left.args) == 1
                and isinstance(left.args[0], ast.Constant)
                and isinstance(left.args[0].value, str)):
            return left.args[0].value, right       # x.get("errors") or []
    return None


def check_green_default_py(tree: ast.AST, rel: str, added: set[int],
                           findings: list[Finding], lines: list[str]) -> None:
    for node in ast.walk(tree):
        hit = _lookup_with_default(node)
        if hit is None:
            continue
        key, default = hit
        verdict = _verdict_default(key, default)
        if verdict is None:
            continue
        if not _touches(added, getattr(node, "lineno", 0), getattr(node, "end_lineno", 0)):
            continue
        got, why = verdict
        findings.append(Finding(
            "green-default",
            f"a missing `{key}` falls back to {got}, which {why} - absent data "
            f"must never default to the value that means all is well",
            rel, node.lineno, _line(lines, node.lineno)))


def report_unparseable(rel: str, exc: SyntaxError, findings: list[Finding],
                       lines: list[str]) -> None:
    """A file the gate cannot read is a finding, never a shrug.

    Skipping it in silence is the same failure the gate is named for: the check
    did not run, and the absence of a complaint reads as a pass.
    """
    findings.append(Finding(
        "unparseable",
        f"cannot be parsed as Python ({exc.msg}) - the gate reports what it "
        f"could not check rather than passing it as clean",
        rel, exc.lineno or 1, _line(lines, exc.lineno or 1)))


def check_python(rel: str, text: str, added: set[int], findings: list[Finding]) -> None:
    lines = text.splitlines()
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError as exc:
        report_unparseable(rel, exc, findings, lines)
        return
    check_swallowed_exception(tree, rel, added, findings, lines)
    check_return_in_finally(tree, rel, added, findings, lines)
    check_green_default_py(tree, rel, added, findings, lines)


# --- javascript / typescript --------------------------------------------------

def mask_code(text: str, keep_comments: bool = False) -> str:
    """Blank string literals and comments, preserving every offset and newline.

    An offset in the mask is the same offset in the source, so a construct that
    matches in the mask is real code and one that only matches in the source was
    inside a string or a comment. That is how `"catch {}"` in a log message stays
    out of the findings.

    `keep_comments` inverts half of it: strings still go, comments stay. That is
    what the marker scan needs, since a marker only counts inside a comment.
    """
    out = list(text)
    n = len(text)
    i = 0

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        ch = text[i]
        if ch in "\"'`":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == ch:
                    j += 1
                    break
                j += 1
            blank(i, j)
            i = j
            continue
        if ch == "/" and i + 1 < n:
            if text[i + 1] == "/":
                j = text.find("\n", i)
                j = n if j < 0 else j
                if not keep_comments:
                    blank(i, j)
                i = j
                continue
            if text[i + 1] == "*":
                j = text.find("*/", i + 2)
                j = n if j < 0 else j + 2
                if not keep_comments:
                    blank(i, j)
                i = j
                continue
        i += 1
    return "".join(out)


# The lookbehind is load-bearing. Without it `.catch(function (e) { use(e) })`
# parses as a `catch` clause whose "bound variable" is the word `function`, and
# the gate then reports a false positive on correct code. A promise handler is
# never a catch clause; it has its own check below.
CATCH = re.compile(r"(?<![.\w$])catch\b\s*(?:\(\s*([A-Za-z_$][\w$]*)[^)]*\))?\s*\{")
PROMISE_CATCH = re.compile(
    r"\.catch\(\s*(?:function\s*\([^)]*\)|\(\s*[\w$,\s]*\)|[A-Za-z_$][\w$]*)"
    r"\s*(?:=>)?\s*(\{\s*\}|null|undefined|void\s+0|false|true|\[\s*\]|0)\s*\)")
JS_DEFAULT = re.compile(
    r"(?:\.|\b)([A-Za-z_$][\w$]*)\s*(?:\?\?|\|\|)\s*"
    r"(true|false|null|undefined|0|\[\s*\]|\{\s*\}|\"[^\"\n]*\"|'[^'\n]*'|`[^`\n]*`)")


def _block_end(mask: str, open_brace_end: int) -> int:
    """Offset just past the `}` that closes the block opened before this point."""
    depth, j = 1, open_brace_end
    while j < len(mask) and depth:
        if mask[j] == "{":
            depth += 1
        elif mask[j] == "}":
            depth -= 1
        j += 1
    return j


def check_empty_catch(rel: str, text: str, mask: str, added: set[int],
                      findings: list[Finding], lines: list[str]) -> None:
    for m in CATCH.finditer(mask):
        var = m.group(1)
        end = _block_end(mask, m.end())
        body = mask[m.end():max(end - 1, m.end())]
        if not body.strip():
            why = ("`catch` block is empty - the throw is caught and nothing at all "
                   "happens next")
        elif var and not re.search(rf"\b{re.escape(var)}\b", body):
            why = (f"`catch ({var})` never mentions `{var}` - the error object is "
                   f"bound and then never looked at")
        else:
            continue
        start_line, end_line = _lineno(text, m.start()), _lineno(text, end)
        if not _touches(added, start_line, end_line):
            continue
        findings.append(Finding("empty-catch", why, rel, start_line,
                                _line(lines, start_line)))


def check_swallowed_promise(rel: str, text: str, mask: str, added: set[int],
                            findings: list[Finding], lines: list[str]) -> None:
    for m in PROMISE_CATCH.finditer(mask):
        line = _lineno(text, m.start())
        if not _touches(added, line, _lineno(text, m.end())):
            continue
        findings.append(Finding(
            "swallowed-promise",
            f"`.catch()` handler evaluates to `{m.group(1).strip()}` - the rejected "
            f"promise resolves as if the call had worked, and the caller downstream "
            f"cannot tell the difference",
            rel, line, _line(lines, line)))


def check_green_default_js(rel: str, text: str, mask: str, added: set[int],
                           findings: list[Finding], lines: list[str]) -> None:
    for m in JS_DEFAULT.finditer(text):
        if mask[m.start(1)] != text[m.start(1)]:
            continue                                # inside a string or a comment
        key, literal = m.group(1), m.group(2).strip()
        words = _key_words(key)
        verdict: tuple[str, str] | None = None
        if words & GREEN_WORDS:
            unquoted = literal[1:-1] if literal[:1] in "\"'`" else literal
            if literal == "true" or unquoted.strip().lower() in GREEN_STRINGS:
                verdict = (literal, "means the check passed")
        if verdict is None and words & ERROR_WORDS:
            if re.fullmatch(r"null|undefined|false|0|\[\s*\]|\{\s*\}|\"\"|''|``", literal):
                verdict = (literal, "means there was nothing wrong")
        if verdict is None:
            continue
        line = _lineno(text, m.start())
        if line not in added:
            continue
        got, why = verdict
        findings.append(Finding(
            "green-default",
            f"a missing `{key}` falls back to `{got}`, which {why} - absent data "
            f"must never default to the value that means all is well",
            rel, line, _line(lines, line)))


def check_javascript(rel: str, text: str, added: set[int],
                     findings: list[Finding]) -> None:
    lines = text.splitlines()
    mask = mask_code(text)
    check_empty_catch(rel, text, mask, added, findings, lines)
    check_swallowed_promise(rel, text, mask, added, findings, lines)
    check_green_default_js(rel, text, mask, added, findings, lines)


# --- shell --------------------------------------------------------------------

IGNORED_STATUS = re.compile(
    r"\|\|\s*(?:/(?:usr/)?bin/)?(true|:)\s*(?=[;&|)}]|$)")
STDERR_GONE = re.compile(
    r"2>\s*/dev/null|2>&-|&>\s*/dev/null|>\s*/dev/null\s+2>&1")
COND_HEAD = re.compile(r"^\s*(?:!\s*)?(?:if|elif|while|until|case)\b")
REAL_HANDLER = re.compile(
    r"\|\|\s*(?!(?:/(?:usr/)?bin/)?(?:true|:)\s*(?:[;&|)}]|$))\S")
SET_PLUS_E = re.compile(r"^\s*set\s+(?:-[A-Za-z]+\s+)*\+[A-Za-z]*e[A-Za-z]*\b")
SET_MINUS_E = re.compile(r"^\s*set\s+(?:\+[A-Za-z]+\s+)*-[A-Za-z]*e[A-Za-z]*\b")


def _hash_comment_start(line: str) -> int | None:
    """Where a `#` comment opens on this line, quote-aware.

    `#` only opens a comment at the start of a word, so `${x#y}` and `$#` are
    left alone.
    """
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            return i
    return None


def mask_shell_line(line: str) -> str:
    """The line with any trailing comment blanked out."""
    start = _hash_comment_start(line)
    return line if start is None else line[:start] + " " * (len(line) - start)


def check_ignored_failure(rel: str, lines: list[str], masked: list[str],
                          added: set[int], findings: list[Finding]) -> None:
    for n, ml in enumerate(masked, 1):
        if n not in added:
            continue
        m = IGNORED_STATUS.search(ml)
        if not m:
            continue
        findings.append(Finding(
            "ignored-failure",
            f"`|| {m.group(1)}` discards the exit status - this line reports success "
            f"whatever happened. If the failure really is acceptable, say so with a "
            f"`{MARKER}` comment or an entry in {ALLOWLIST.name}",
            rel, n, lines[n - 1].strip()))


def _status_read_next(masked: list[str], index: int) -> bool:
    """True when one of the next couple of live lines actually reads `$?`."""
    seen = 0
    for j in range(index, len(masked)):
        body = masked[j].strip()
        if not body:
            continue
        if "$?" in body:
            return True
        seen += 1
        if seen >= 2:
            break
    return False


def check_swallowed_stderr(rel: str, lines: list[str], masked: list[str],
                           added: set[int], findings: list[Finding]) -> None:
    for n, ml in enumerate(masked, 1):
        if n not in added or not STDERR_GONE.search(ml):
            continue
        if COND_HEAD.search(ml):
            continue                       # the status IS the condition
        if REAL_HANDLER.search(ml):
            continue                       # `|| return 1`, `|| die ...`
        if IGNORED_STATUS.search(ml):
            continue                       # already reported as ignored-failure
        if _status_read_next(masked, n):
            continue
        findings.append(Finding(
            "swallowed-stderr",
            "stderr goes to /dev/null on a command whose exit status is never read - "
            "the failure leaves no message and no status behind",
            rel, n, lines[n - 1].strip()))


def check_unguarded_set_e(rel: str, lines: list[str], masked: list[str],
                          added: set[int], findings: list[Finding]) -> None:
    for n, ml in enumerate(masked, 1):
        if n not in added or not SET_PLUS_E.match(ml):
            continue
        if any(SET_MINUS_E.match(later) for later in masked[n:]):
            continue
        findings.append(Finding(
            "unguarded-set-plus-e",
            "`set +e` is never re-armed with `set -e` - every command after this "
            "line may fail without stopping the script",
            rel, n, lines[n - 1].strip()))


def check_shell(rel: str, text: str, added: set[int], findings: list[Finding]) -> None:
    lines = text.splitlines()
    masked = [mask_shell_line(l) for l in lines]
    check_ignored_failure(rel, lines, masked, added, findings)
    check_swallowed_stderr(rel, lines, masked, added, findings)
    check_unguarded_set_e(rel, lines, masked, added, findings)


# --- CI / YAML ----------------------------------------------------------------

CONTINUE_ON_ERROR = re.compile(
    r"^\s*continue-on-error:\s*(?:true|yes|'true'|\"true\")\s*$", re.I)
ALWAYS_IF = re.compile(r"^\s*if:\s*(?:\$\{\{\s*)?always\(\)")
VERDICT_RUN = re.compile(
    r"\b(pytest|jest|vitest|mocha|tsc|eslint|ruff|flake8|mypy|phpunit|rspec|"
    r"cargo\s+(?:test|clippy)|go\s+test|gradlew?\s+(?:test|check)|"
    r"lint|audit|gate|check|verify|test)\b", re.I)


def _yaml_item_block(lines: list[str], idx: int) -> list[str]:
    """The list item (a workflow step) that owns the key on line `idx`."""
    key_indent = len(lines[idx]) - len(lines[idx].lstrip())
    start, dash = idx, None
    while start >= 0:
        m = re.match(r"^(\s*)-\s+\S", lines[start])
        if m and len(m.group(1)) < key_indent:
            dash = len(m.group(1))
            break
        start -= 1
    if dash is None:
        return [lines[idx]]
    end = start + 1
    while end < len(lines):
        body = lines[end]
        if body.strip():
            indent = len(body) - len(body.lstrip())
            if indent <= dash:
                break
        end += 1
    return lines[start:end]


def check_continue_on_error(rel: str, lines: list[str], added: set[int],
                            findings: list[Finding]) -> None:
    for n, raw in enumerate(lines, 1):
        if n in added and CONTINUE_ON_ERROR.match(raw):
            findings.append(Finding(
                "continue-on-error",
                "`continue-on-error: true` turns this step's verdict into a "
                "suggestion - the job goes green with the step red",
                rel, n, raw.strip()))


def check_always_on_verdict(rel: str, lines: list[str], added: set[int],
                            findings: list[Finding]) -> None:
    for n, raw in enumerate(lines, 1):
        if n not in added or not ALWAYS_IF.match(raw):
            continue
        block = "\n".join(_yaml_item_block(lines, n - 1))
        if not re.search(r"^\s*run:", block, re.M):
            continue                       # uploading an artifact always() is fine
        if not VERDICT_RUN.search(block):
            continue
        findings.append(Finding(
            "always-on-verdict",
            "`if: always()` on a step that runs a build, test or lint command - the "
            "step runs after an earlier failure and its own result is what the job "
            "reports, so an earlier red can end up green",
            rel, n, raw.strip()))


def check_yaml(rel: str, text: str, added: set[int], findings: list[Finding]) -> None:
    lines = text.splitlines()
    check_continue_on_error(rel, lines, added, findings)
    check_always_on_verdict(rel, lines, added, findings)


# --- driver -------------------------------------------------------------------

CHECKERS = {
    "python": check_python,
    "javascript": check_javascript,
    "shell": check_shell,
    "yaml": check_yaml,
}


def scan(targets: dict[str, set[int]], findings: list[Finding]) -> tuple[int, list[str]]:
    scanned, skipped = 0, []
    for rel in sorted(targets):
        lang = language_of(rel)
        if lang is None:
            skipped.append(rel)
            continue
        text = _try_read(rel)
        if text is None:
            findings.append(Finding(
                "unreadable",
                "is named in the diff but could not be read as UTF-8 text - "
                "reported rather than skipped in silence", rel, 1))
            continue
        before = len(findings)
        CHECKERS[lang](rel, text, targets[rel], findings)
        scanned += 1
        marks = marked_lines(comment_view(lang, text), text.splitlines(), rel, findings)
        findings[before:] = [f for f in findings[before:] if f.line not in marks]
    return scanned, skipped


def to_sarif(findings: list[Finding]) -> dict[str, Any]:
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "agnostic-harness-fail-open",
                "informationUri": "https://github.com/arnoldwender/agnostic-harness",
                "rules": [{"id": r} for r in sorted({f.check for f in findings})],
            }},
            "results": [{
                "ruleId": f.check,
                "level": "error",
                "message": {"text": f.message},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": f.path},
                    "region": {"startLine": max(f.line, 1)},
                }}],
            } for f in findings],
        }],
    }


def _targets_from_files(names: list[str], findings: list[Finding]) -> dict[str, set[int]]:
    """--files names the files outright, so every line counts as new.

    This is the pre-commit shape (`git diff --cached --name-only | xargs ...`) and
    the "just check this file" shape. It deliberately does NOT consult the diff:
    a caller who names a file is asking about the file.
    """
    targets: dict[str, set[int]] = {}
    for name in names:
        rel = _relative(name)
        text = _try_read(rel)
        if text is None:
            findings.append(Finding(
                "missing-file",
                "was named on the command line and could not be read - a gate that "
                "shrugs at a missing input is the failure this gate is about",
                rel, 1))
            continue
        targets[rel] = set(range(1, len(text.splitlines()) + 1))
    return targets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--base", default="origin/main", metavar="REF",
                    help="diff against this ref (default: origin/main)")
    ap.add_argument("--files", nargs="+", metavar="PATH",
                    help="check these files whole, instead of reading the diff")
    ap.add_argument("--sarif", metavar="PATH", help="write SARIF 2.1.0 to PATH")
    args = ap.parse_args(argv)

    findings: list[Finding] = []
    try:
        allow = load_allowlist(findings)
        if args.files:
            targets = _targets_from_files(args.files, findings)
            source = f"{len(args.files)} file(s) named on the command line"
        else:
            targets = added_from_diff(args.base)
            source = f"lines added since {args.base}"
        scanned, skipped = scan(targets, findings)
    except GateError as exc:
        print(f"gate failure: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:                      # noqa: BLE001 - reported, never hidden
        print(f"gate failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    findings = [f for f in findings if not _allowed(f, allow)]

    if args.sarif:
        Path(args.sarif).write_text(json.dumps(to_sarif(findings), indent=2),
                                    encoding="utf-8")

    note = f", {len(skipped)} in no language this gate parses" if skipped else ""
    print(f"fail-open: {source}; {scanned} file(s) scanned{note}")
    for f in sorted(findings, key=lambda x: (x.path, x.line)):
        print(f"  FAIL [{f.check}] {f.path}:{f.line}: {f.message}")
        if f.text:
            print(f"         {f.text[:110]}")
    if findings:
        print(f"\n{len(findings)} finding(s)")
        return 1
    print("  no added line swallows an error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
