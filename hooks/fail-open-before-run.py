#!/usr/bin/env python3
"""The Agnostic Harness — the gate, before the error is swallowed.

A Claude Code `PreToolUse` hook for `Bash`, `Edit`, `Write` and `MultiEdit`.
Before the tool runs, it hands that ONE tool call to the fail-open gate
(`gate/fail_open.py`) — the same checkers, the same `fail-open-ok` marker, the
same allowlist — and, if the change swallows an error (`except: pass`,
`catch {}`, `|| true`, `continue-on-error: true`, a default that means "all is
well" for data that never arrived), tells the agent so in the tool result. It
warns; it does not block. See hooks/README.md for the wiring and the README
for why.

    "hooks": {"PreToolUse": [{"matcher": "Bash|Edit|Write|MultiEdit", "hooks": [
        {"type": "command", "command": "python3 /abs/path/to/agnostic-harness/hooks/fail-open-before-run.py",
         "timeout": 10}]}]}

WHY A HOOK AND NOT ONLY THE GATE
--------------------------------
The gate reads the added lines of a diff, in CI or before a commit. By then the
`except: pass` has been written, the test that depended on the error has been
run against it, and the agent has reported the green it produced. The
Sentinel's third rule — refuse the cheap rescue — is broken at the moment the
line is written, not at the moment it is diffed; and the cheapest rescue of all
is typed straight into a shell (`npm test || true`) and never reaches a diff at
all. This hook is the same gate at that moment, on the only change that
matters: the one about to happen.

WHAT IT DOES
------------
1. Reads the hook payload from stdin: `tool_name`, `tool_input`, `cwd`,
   `session_id`. Ignores every tool but the four above. `NotebookEdit` is left
   out on purpose: a notebook is JSON, and the gate judges none of it.
2. Imports the gate with `HARNESS_ROOT` set to the session's working directory,
   so `.conduct/fail-open-allow.txt` is the working repository's, not this one's.
3. Builds what the gate would see AFTER the tool ran, and which lines are new:
   * `Bash` — the command text, judged with the shell checker, every line added.
   * `Edit` — the edit is SIMULATED. The file is read from disk, `old_string` is
     replaced by `new_string` (every occurrence with `replace_all`), and the
     lines of the result that come from `new_string` are the added ones. The
     whole file goes to the checker, because the Python checker parses a module
     (`ast.parse`) and a fragment is not a module: an indented handler judged on
     its own is an `IndentationError`, not a finding. When `old_string` is not
     in the file, `new_string` is judged alone with every line added, and the
     receipt says so (`edit: old-string-not-found`).
   * `MultiEdit` — the edits applied in sequence, the same way.
   * `Write` — the content; added = the lines not present in the old file, by
     exact text, or every line when the file does not exist yet.
4. Dispatches by the file's suffix exactly as the gate does (Python, JS/TS,
   shell, YAML; an extensionless file whose first line is a shell shebang is
   shell). A file the gate does not judge is not judged here either: the
   receipt says `verdict: skipped`, which is not the same word as `ok`.
5. Runs the gate's checker, drops the lines a `fail-open-ok` comment accepts
   (`marked_lines`) and the findings the allowlist exempts (`_allowed`) — the
   gate's own functions, not a copy of them.
6. If anything is left: prints `{"hookSpecificOutput": {"hookEventName":
   "PreToolUse", "additionalContext": "..."}}` and exits 0. Claude Code adds
   that text to the agent's context alongside the tool result. The permission
   flow is not touched. At most four findings are shown.
7. Appends one receipt line per run to `FAIL_OPEN_RECEIPTS` (default
   `~/.local/state/agnostic-harness/fail-open-receipts.jsonl`; `off` disables):
   `{ts, session, tool, path, verdict, checks, findings, lines, ms}`. Never
   file contents, never the command. The receipts are how the false-positive
   rate gets measured on real sessions — the number this hook needs before
   anyone should let it block.

`judge(tool, tool_input, root)` is importable and returns `(findings,
judged_lines)`, so that rate can be measured over recorded sessions without a
process per tool call.

WHAT IT DOES NOT SEE (inherited from the gate, stated so they stay decisions)
----------------------------------------------------------------------------
* Four languages only — Python, JavaScript/TypeScript, shell, CI YAML. An Edit
  to a Go, Rust or Swift file is `skipped`, not cleared.
* `swallowed-stderr` on a command typed into the Bash tool. The gate's premise
  for that check is a script nobody is watching; an interactive call has the
  agent reading the output and the status is the tool result. Measured over
  27,549 real Bash calls it flagged 22.5 % of them, so the hook does not run
  it there. It still runs on every shell file the agent writes.
* A Bash command is judged as shell, line by line, heredocs included. A script
  fed to an interpreter through a heredoc (`python3 - <<'PY'`) is judged as the
  shell lines it arrives in, not as the language it is written in; and a
  heredoc that writes prose into a file is judged as if it were a script. The
  gate has no heredoc awareness, and this hook adds none: the receipts will
  say whether that costs anything.
* A Python file that does not parse — before or after the edit — is reported
  as `unparseable` on every edit, as the gate reports it on every diff. A file
  that cannot be checked is a finding, not a pass.
* Whether the error handling that IS there is any good. Only that some exists.
* Any runtime other than Claude Code. Its payload is the only shape that was
  run, so it is the only one this hook reads.

MODES AND FAIL-OPEN
-------------------
`FAIL_OPEN_HOOK_MODE=warn` (default) injects the text and exits 0.
`FAIL_OPEN_HOOK_MODE=block` writes it to stderr and exits 2, which Claude Code
treats as a denial. Block is shipped so the switch exists; it is not the
default, because a guard whose false-positive rate nobody has measured on real
sessions is switched off by the first person it wrongly stops.

Any error of the hook's own is a receipt with `verdict: error` and exit 0. Yes:
the hook that hunts code which fails open, fails open. The gate can say "I
broke" with exit 2 because exit 2 means nothing else there; here exit 2 means
"deny the tool call", and a hook that denies an agent's edit because of its own
bug is the hook that gets uninstalled, after which it catches nothing. So it
stays out of the way and leaves a receipt. The gate's README draws the line
between failing open and failing LOUDLY, and this file lives on that line by
choice: the failure is counted, never silent, and `verdict: error` in the
receipts is the first thing to grep when the warning goes quiet.

Tests: tests/test_fail_open_hook.py · mutants: tests/mutation_check_fail_open_hook.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
GATE = pathlib.Path(os.environ.get("FAIL_OPEN_GATE") or HERE.parent / "gate" / "fail_open.py")


def _default_receipts() -> str:
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "agnostic-harness", "fail-open-receipts.jsonl")


RECEIPTS = os.environ.get("FAIL_OPEN_RECEIPTS") or _default_receipts()
MODE = os.environ.get("FAIL_OPEN_HOOK_MODE", "warn")              # warn | block
TOOLS = {"Bash", "Edit", "Write", "MultiEdit"}
# Checks the gate runs on a shell FILE that this hook does not run on a command typed
# into the Bash tool. `swallowed-stderr` reads "2>/dev/null on a command whose exit
# status is never read": in a script nobody is watching that is a failure with no
# trace. In an interactive call the agent reads the output and the status is the
# tool result - `ls X 2>/dev/null`, `du -sh dist 2>/dev/null`, `f=$(find … 2>/dev/null)`
# are the ordinary shape of looking around. Measured over 27,549 real Bash calls
# before this line existed: 22.5 % carried that finding, and a hook that fires on one
# call in four is uninstalled by the end of the day. The check still runs on every
# .sh the agent WRITES, where the gate's premise holds.
INTERACTIVE_SKIP = frozenset({"swallowed-stderr"})
MAX_SHOWN = 4
MAX_MESSAGE = 260                # per finding; the runtime caps hook output at 10,000
COMMAND_REL = "<command>.sh"     # the shell checker is reached through the suffix, as in the gate
# The gate's own test for an extensionless file. Kept here because the gate reads the
# first line from DISK, and a Write that creates the file has nothing on disk yet.
SHEBANG = re.compile(r"^#!.*\b(?:ba|z|k|da)?sh\b")


# --- receipts ----------------------------------------------------------------

def receipt(**row: object) -> None:
    """One JSON line per run. Verdicts and counts only — never file contents, never the command."""
    if RECEIPTS == "off":
        return
    try:
        pathlib.Path(RECEIPTS).parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **row}
        with open(RECEIPTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — fail-open-ok: the receipt is the instrument, never the verdict; an unwritable state dir must not stop the agent
        pass


# --- the gate ----------------------------------------------------------------

def load_gate(root: str):
    """Import the gate by path with `HARNESS_ROOT` = the session's working directory.
    The gate fixes its root at import time, and the allowlist it loads is relative to it."""
    os.environ["HARNESS_ROOT"] = root
    spec = importlib.util.spec_from_file_location("agnostic_fail_open_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                        # the dataclass resolves its annotations here
    spec.loader.exec_module(mod)
    return mod


def language_for(ro, rel: str, text: str) -> str | None:
    """The gate's suffix dispatch, on the content the tool call will leave behind."""
    ext = pathlib.Path(rel).suffix.lower()
    if ext in ro.PY_EXT:
        return "python"
    if ext in ro.JS_EXT:
        return "javascript"
    if ext in ro.SH_EXT:
        return "shell"
    if ext in ro.YAML_EXT:
        return "yaml"
    if not ext and SHEBANG.match(text.split("\n", 1)[0]):
        return "shell"
    return None
# mutation-anchor: language_for


# --- the simulated change ----------------------------------------------------

def real_lines(text: str) -> int:
    """Lines a text really has: the "\\n"-separated segments, not counting an empty last one."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _occurrences(text: str, needle: str, everywhere: bool) -> list[int]:
    """Offsets of `needle` in `text`: the first, or all non-overlapping ones (`str.replace`)."""
    out: list[int] = []
    pos = text.find(needle)
    while pos != -1:
        out.append(pos)
        if not everywhere:
            break
        pos = text.find(needle, pos + len(needle))
    return out


def simulate_edits(text: str, edits: list[dict]) -> tuple[str, bytearray, str | None]:
    """Apply the edits in order. Returns the result, a per-character origin map (1 = written
    by this tool call, 0 = already there) and a note for the receipt.

    An `old_string` that is not in the text is what the tool itself will refuse; the hook
    does not guess where it would have gone. That edit's `new_string` is judged alone, every
    character new, and the edits after it are not applied — the tool would not have applied
    them either."""
    origin = bytearray(len(text))
    for e in edits:
        old = str(e.get("old_string") or "")
        new = str(e.get("new_string") or "")
        hits = _occurrences(text, old, bool(e.get("replace_all"))) if old else []
        if not hits:
            return new, bytearray(b"\x01") * len(new), "old-string-not-found"
        for s in reversed(hits):                 # back to front, so earlier offsets hold
            text = text[:s] + new + text[s + len(old):]
            origin = origin[:s] + bytearray(b"\x01") * len(new) + origin[s + len(old):]
    return text, origin, None
# mutation-anchor: simulate_edits


def added_from_origin(text: str, origin: bytearray) -> set[int]:
    """1-based lines of `text` that hold at least one character written by this call."""
    out: set[int] = set()
    pos = 0
    for n, line in enumerate(text.split("\n"), 1):
        end = pos + len(line) + 1                # the newline belongs to the line it ends
        if origin.find(b"\x01", pos, end) != -1:
            out.add(n)
        pos = end
    return out
# mutation-anchor: added_from_origin


def added_by_write(old: str | None, new: str) -> set[int]:
    """Lines of `new` that the old file did not hold, by exact text; all of them if there was
    no old file. The same reading the gate gives an untracked file: 100 % new code."""
    total = real_lines(new)
    if old is None:
        return set(range(1, total + 1))
    seen = set(old.split("\n"))
    return {n for n, line in enumerate(new.split("\n"), 1) if n <= total and line not in seen}
# mutation-anchor: added_by_write


def _disk(target: str) -> tuple[str | None, bool]:
    """(content, unreadable). None with False: the file does not exist. None with True: it
    exists and is not UTF-8 text — the gate reports that, never skips it."""
    p = pathlib.Path(target)
    if not p.is_file():
        return None, False
    try:
        return p.read_text(encoding="utf-8"), False
    except (OSError, UnicodeDecodeError):
        return None, True


def _rel(target: str, root: str) -> str:
    """The path as the gate would spell it: relative to the root when it is under the root
    (that is what allowlist patterns like `^vendor/` are written against), absolute otherwise."""
    try:
        return str(pathlib.Path(target).resolve().relative_to(pathlib.Path(root).resolve()))
    except ValueError:
        return target


def prepare(ro, tool: str, given: dict, root: str) -> tuple[str, str, str | None, set[int], dict]:
    """(target, rel, text, added, meta): what the gate would see after this tool call.
    `text` is None only when the file exists and cannot be read as UTF-8."""
    if tool == "Bash":
        command = str(given.get("command") or "")
        return "<command>", COMMAND_REL, command, set(range(1, real_lines(command) + 1)), {}
    target = str(given.get("file_path") or "")
    if not os.path.isabs(target):
        target = os.path.join(root, target)
    target = os.path.normpath(target)
    rel = _rel(target, root)
    current, unreadable = _disk(target)
    if unreadable:
        return target, rel, None, set(), {}
    if tool == "Write":
        content = str(given.get("content") or "")
        return target, rel, content, added_by_write(current, content), {}
    if tool == "Edit":
        edits = [given]
    elif tool == "MultiEdit":
        edits = [e for e in (given.get("edits") or []) if isinstance(e, dict)]
    else:
        return target, rel, "", set(), {"verdict": "skipped"}
    text, origin, note = simulate_edits(current or "", edits)
    meta = {"edit": note} if note else {}
    return target, rel, text, added_from_origin(text, origin), meta


# --- the predicate -----------------------------------------------------------

def _judge(tool: str, given: dict, root: str) -> tuple[list, int, dict]:
    ro = load_gate(root)
    target, rel, text, added, meta = prepare(ro, tool, given, root)
    meta["path"] = target
    if meta.get("verdict") == "skipped":
        return [], 0, meta
    if text is None:
        finding = ro.Finding("unreadable", "exists but could not be read as UTF-8 text - "
                             "reported rather than skipped in silence", rel, 1)
        return [finding], 0, meta
    lang = language_for(ro, rel, text)
    if lang is None:
        meta["verdict"] = "skipped"
        return [], 0, meta
    findings: list = []
    allow = ro.load_allowlist(findings)              # a broken allowlist is a finding, as in the gate
    before = len(findings)
    ro.CHECKERS[lang](rel, text, added, findings)
    if tool == "Bash":
        findings[before:] = [f for f in findings[before:] if f.check not in INTERACTIVE_SKIP]
        # mutation-anchor: interactive
    marks = ro.marked_lines(ro.comment_view(lang, text), text.splitlines(), rel, findings)
    findings[before:] = [f for f in findings[before:] if f.line not in marks]
    # mutation-anchor: marks
    findings = [f for f in findings if not ro._allowed(f, allow)]
    # mutation-anchor: allowlist
    return findings, len(added), meta


def judge(tool: str, tool_input: dict, root: str) -> tuple[list, int]:
    """Findings the gate raises for ONE tool call judged from `root`, and how many lines it
    judged. Importable, so the false-positive rate can be measured over recorded sessions
    without spawning a process per call. A tool or file the gate does not judge is `([], 0)`."""
    findings, judged, _ = _judge(tool, tool_input, root)
    return findings, judged


# --- the warning -------------------------------------------------------------

def safe_name(path: str) -> str:
    """The base name without what could break the warning's markdown or smuggle text shaped
    like an instruction (backticks, line breaks, control characters). The agent already saw
    the name in its own tool input; this is depth, not a boundary."""
    base = os.path.basename(path)
    return re.sub(r"[`\r\n\t\x00-\x1f\x7f]", "?", base)[:120]


def message(findings: list, tool: str) -> str:
    parts = []
    for f in findings[:MAX_SHOWN]:
        where = f"line {f.line}" if f.path == COMMAND_REL else f"`{safe_name(f.path)}`:{f.line}"
        parts.append(f"[{f.check}] {where}: {f.message[:MAX_MESSAGE]}")
    more = f" (+{len(findings) - MAX_SHOWN} more)" if len(findings) > MAX_SHOWN else ""
    what = "this command" if tool == "Bash" else "this change"
    return (f"fail-open: {what} swallows an error before anyone can see it. "
            + " ".join(parts) + more
            + " The Sentinel · 3: refuse the cheap rescue. What you do not control must fail "
            "loudly, so that the part you do control — the response — can happen at all; an "
            "error nobody sees is not an error anybody handled. Log it, re-raise it, count "
            "it, or return an explicit error value. If swallowing really is correct here, say "
            "so on the line with a `fail-open-ok` comment, or add a pattern to "
            ".conduct/fail-open-allow.txt. Warning mode: this change is NOT blocked.")


# --- main --------------------------------------------------------------------

def main() -> int:
    t0 = time.time()
    payload = json.loads(sys.stdin.read() or "{}")
    tool = payload.get("tool_name")
    if tool not in TOOLS:
        return 0
    # mutation-anchor: TOOLS
    given = payload.get("tool_input") or {}
    if not isinstance(given, dict):
        given = {}
    if tool == "Bash" and not str(given.get("command") or "").strip():
        return 0
    if tool != "Bash" and not given.get("file_path"):
        return 0
    root = str(payload.get("cwd") or os.getcwd())
    session = str(payload.get("session_id") or "")[:8]

    findings, judged, meta = _judge(tool, given, root)
    ms = int((time.time() - t0) * 1000)
    common = {"session": session, "tool": tool, "path": meta.get("path"), "mode": MODE,
              "lines": judged, "ms": ms}
    if "edit" in meta:
        common["edit"] = meta["edit"]
    if meta.get("verdict") == "skipped":
        receipt(verdict="skipped", **common)
        return 0
    if not findings:
        receipt(verdict="ok", **common)
        return 0
    receipt(verdict="finding", checks=sorted({f.check for f in findings}),
            findings=len(findings), **common)
    text = message(findings, tool)
    if MODE == "block":
        sys.stderr.write(text.replace("Warning mode: this change is NOT blocked.",
                                      "Block mode: this change was not applied.") + "\n")
        return 2
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "additionalContext": text}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open on purpose: the hook is never the blocker
        receipt(verdict="error", error=f"{type(exc).__name__}: {exc}"[:200])
        sys.exit(0)
