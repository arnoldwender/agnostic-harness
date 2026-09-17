"""Tests for the live hook, hooks/fail-open-before-run.py.

Same pair as the gate's own suite: the change that swallows an error must be
WARNED about, and the same change with the error actually handled must pass in
silence. The hook is run as a PreToolUse subprocess with the payload on stdin,
inside a small git repo, and the exit code, the stdout JSON and the receipt are
what is asserted.

Every defect below lives inside a Python string. The gate that guards this
repository parses Python with `ast`, so a fixture is data to it - which is also
why the hook simulates a whole file for an Edit: the fragment an agent types is
not a module, and a fragment judged alone is an IndentationError, not a finding.

    python3 -m pytest tests/test_fail_open_hook.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# The mutation runner points this at a mutated COPY; the real hook is never rewritten.
HOOK = Path(os.environ.get("FAIL_OPEN_HOOK_UNDER_TEST")
            or Path(__file__).resolve().parent.parent / "hooks" / "fail-open-before-run.py")


# --- fixtures: what is on disk before the tool call --------------------------

PY_CLEAN = '''\
import json
import logging

log = logging.getLogger(__name__)


def parse(raw):
    return json.loads(raw)


def size(path):
    return 1
'''

PY_LOGGED = '''\
import json
import logging

log = logging.getLogger(__name__)


def parse(raw):
    try:
        return json.loads(raw)
    except Exception:
        log.exception("the payload did not parse")
        return None
'''

PY_OLD_SWALLOW = '''\
def quiet(path):
    try:
        return open(path).read()
    except OSError:
        pass


def size(path):
    return 1
'''

PY_TWO_LOGGED = '''\
import logging

log = logging.getLogger(__name__)


def parse_all(items):
    out = []
    for raw in items:
        try:
            out.append(int(raw))
        except ValueError:
            log.warning("skipping %r", raw)
            continue
    return out


def parse_some(items):
    out = []
    for raw in items:
        try:
            out.append(float(raw))
        except ValueError:
            log.warning("skipping %r", raw)
            continue
    return out
'''

PY_EXCEPT_EXCEPTION_PASS = '''\
import json


def parse(raw):
    try:
        return json.loads(raw)
    except Exception:
        pass
'''

JS_CLEAN = """\
export function ping(url) {
  return fetch(url);
}
"""

JS_EMPTY_CATCH = """\
export async function load(url) {
  try {
    return await fetch(url);
  } catch {
  }
}
"""

JS_HANDLED_CATCH = """\
export function parse(raw, report) {
  try {
    return JSON.parse(raw);
  } catch (err) {
    report(err);
    throw err;
  }
}
"""

YML_CONTINUE_ON_ERROR = """\
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Tests
        continue-on-error: true
        run: pytest -q
"""

YML_NO_CONTINUE_ON_ERROR = """\
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Tests
        continue-on-error: false
        run: pytest -q
"""

MD_WITH_A_CODE_BLOCK = """\
# Notes

Never write this:

```python
try:
    risky()
except Exception:
    pass
```

Nor this:

```bash
npm run build || true
```
"""


# --- the runner --------------------------------------------------------------

def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo with one clean commit: the working directory the hook is told about."""
    (tmp_path / "NOTES.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "init.templateDir=",
                    "init", "-q", str(tmp_path)], check=True, capture_output=True)
    git(tmp_path, "config", "user.email", "gate@example.invalid")
    git(tmp_path, "config", "user.name", "Gate Test")
    git(tmp_path, "config", "commit.gpgsign", "false")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "baseline")
    return tmp_path


def put(root: Path, rel: str, content: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return str(p)


def hook(root: Path, tool: str, tool_input: dict, env: dict[str, str] | None = None
         ) -> tuple[int, dict | None, str, dict | None]:
    receipts = root / "receipts.jsonl"
    payload = {"session_id": "test-session", "cwd": str(root), "hook_event_name": "PreToolUse",
               "tool_name": tool, "tool_input": tool_input, "tool_use_id": "toolu_x"}
    run_env = {**os.environ, "FAIL_OPEN_RECEIPTS": str(receipts)}
    run_env.pop("FAIL_OPEN_HOOK_MODE", None)
    run_env.update(env or {})
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, env=run_env, cwd=str(root),
                       check=False, timeout=60)
    out = json.loads(r.stdout) if r.stdout.strip() else None
    rec = None
    if receipts.is_file():
        rec = json.loads(receipts.read_text(encoding="utf-8").strip().split("\n")[-1])
    return r.returncode, out, r.stderr, rec


def bash(root: Path, command: str, **kw):
    return hook(root, "Bash", {"command": command}, **kw)


def edit(root: Path, path: str, old: str, new: str, replace_all: bool = False, **kw):
    given = {"file_path": path, "old_string": old, "new_string": new}
    if replace_all:
        given["replace_all"] = True
    return hook(root, "Edit", given, **kw)


def write(root: Path, path: str, content: str, **kw):
    return hook(root, "Write", {"file_path": path, "content": content}, **kw)


def warning(out: dict | None) -> str:
    return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""


# --- the control -------------------------------------------------------------

def test_a_clean_edit_is_silent(repo: Path) -> None:
    """Without this, every test below could pass because the hook always warns."""
    p = put(repo, "svc.py", PY_CLEAN)
    rc, out, _, rec = edit(repo, p, "def size(path):\n    return 1\n",
                           "def size(path):\n    return len(path)\n")
    assert rc == 0 and out is None, (rc, out)
    assert rec["verdict"] == "ok" and rec["tool"] == "Edit" and rec["lines"] == 2


def test_a_clean_bash_command_is_silent(repo: Path) -> None:
    rc, out, _, rec = bash(repo, "npm ci && npm test")
    assert rc == 0 and out is None, (rc, out)
    assert rec["verdict"] == "ok" and rec["lines"] == 1


# --- python, through a simulated edit ----------------------------------------

def test_except_pass_added_by_an_edit_warns(repo: Path) -> None:
    """The fragment is indented: on its own it is an IndentationError, not a module. Only
    the simulated whole file lets the Python checker see the handler for what it is."""
    p = put(repo, "svc.py", PY_CLEAN)
    rc, out, _, rec = edit(repo, p, "    return json.loads(raw)\n",
                           "    try:\n        return json.loads(raw)\n    except Exception:\n        pass\n")
    assert rc == 0
    assert "[swallowed-exception]" in warning(out) and "NOT blocked" in warning(out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in out["hookSpecificOutput"], "the permission flow is not touched"
    assert rec["verdict"] == "finding" and rec["checks"] == ["swallowed-exception"]
    assert "edit" not in rec, "old_string was found; the receipt must not say otherwise"


def test_the_same_handler_that_logs_the_error_is_silent(repo: Path) -> None:
    p = put(repo, "svc.py", PY_CLEAN)
    _, out, _, rec = edit(repo, p, "    return json.loads(raw)\n",
                          "    try:\n        return json.loads(raw)\n    except Exception:\n"
                          "        log.exception(\"the payload did not parse\")\n        return None\n")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok" and rec["lines"] == 5


def test_a_pre_existing_swallow_elsewhere_is_not_reported_when_the_edit_touches_other_lines(
        repo: Path) -> None:
    """The gate reads added lines only; so does the hook. The handler at the top of this
    file was there before, and this edit never went near it."""
    p = put(repo, "svc.py", PY_OLD_SWALLOW)
    _, out, _, rec = edit(repo, p, "def size(path):\n    return 1\n",
                          "def size(path):\n    return len(path)\n")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok" and rec["lines"] == 2


def test_turning_a_logged_handler_into_a_swallow_warns(repo: Path) -> None:
    p = put(repo, "svc.py", PY_LOGGED)
    _, out, _, rec = edit(repo, p, "        log.exception(\"the payload did not parse\")\n        return None\n",
                          "        pass\n")
    assert "[swallowed-exception]" in warning(out)
    assert rec["checks"] == ["swallowed-exception"] and rec["lines"] == 1


def test_replace_all_is_honoured(repo: Path) -> None:
    """Two handlers, one edit. With `replace_all` both lose their log line and both are
    reported; without it only the first does."""
    p = put(repo, "svc.py", PY_TWO_LOGGED)
    _, out, _, rec = edit(repo, p, "            log.warning(\"skipping %r\", raw)\n",
                          "            # skipped on purpose\n", replace_all=True)
    assert rec["findings"] == 2, warning(out)
    _, out, _, rec = edit(repo, p, "            log.warning(\"skipping %r\", raw)\n",
                          "            # skipped on purpose\n")
    assert rec["findings"] == 1, warning(out)


def test_a_green_default_added_by_an_edit_warns(repo: Path) -> None:
    """The gate's own expensive pattern: absent data defaulting to the value that means
    all is well."""
    p = put(repo, "svc.py", PY_CLEAN)
    _, out, _, rec = edit(repo, p, "    return json.loads(raw)\n",
                          "    data = json.loads(raw)\n    status = data.get(\"status\", \"ok\")\n"
                          "    return status == \"ok\"\n")
    assert "[green-default]" in warning(out) and rec["checks"] == ["green-default"]


def test_a_fragment_that_matches_nothing_is_judged_alone_and_reported_unparseable(repo: Path) -> None:
    """The tool will refuse this edit; the hook does not guess where the fragment would have
    gone. Judged alone it does not parse, and the gate's answer to that is a finding, not
    silence."""
    p = put(repo, "svc.py", PY_CLEAN)
    _, out, _, rec = edit(repo, p, "    return NOTHING_LIKE_THIS\n",
                          "    except Exception:\n        pass\n")
    assert "[unparseable]" in warning(out)
    assert rec["checks"] == ["unparseable"] and rec["edit"] == "old-string-not-found"


def test_a_fragment_that_matches_nothing_but_parses_is_judged_as_written(repo: Path) -> None:
    p = put(repo, "svc.py", PY_CLEAN)
    _, out, _, rec = edit(repo, p, "    return NOTHING_LIKE_THIS\n",
                          "def quiet():\n    try:\n        return 1\n    except Exception:\n        pass\n")
    assert "[swallowed-exception]" in warning(out)
    assert rec["edit"] == "old-string-not-found" and rec["lines"] == 5


def test_multiedit_is_simulated_in_sequence(repo: Path) -> None:
    p = put(repo, "svc.py", PY_CLEAN)
    edits = [
        {"old_string": "    return json.loads(raw)\n",
         "new_string": "    try:\n        return json.loads(raw)\n    except ValueError:\n"
                       "        log.exception(\"bad payload\")\n        raise\n"},
        {"old_string": "def size(path):\n    return 1\n",
         "new_string": "def size(path):\n    try:\n        return len(path)\n    except TypeError:\n"
                       "        pass\n"},
    ]
    _, out, _, rec = hook(repo, "MultiEdit", {"file_path": p, "edits": edits})
    # The first edit grows the file by four lines, so the second edit's handler lands on
    # line 18 of the SIMULATED file — the line the gate reports, not the line on disk.
    assert "[swallowed-exception]" in warning(out) and "`svc.py`:18" in warning(out)
    assert rec["tool"] == "MultiEdit" and rec["findings"] == 1 and rec["lines"] == 10


def test_a_relative_file_path_is_resolved_against_cwd(repo: Path) -> None:
    put(repo, "sub/svc.py", PY_CLEAN)
    _, out, _, rec = edit(repo, "sub/svc.py", "    return json.loads(raw)\n",
                          "    try:\n        return json.loads(raw)\n    except Exception:\n        pass\n")
    assert "[swallowed-exception]" in warning(out)
    assert rec["path"] == str(repo / "sub" / "svc.py")


# --- javascript / typescript -------------------------------------------------

def test_an_empty_catch_written_to_a_ts_file_warns(repo: Path) -> None:
    _, out, _, rec = write(repo, str(repo / "svc.ts"), JS_EMPTY_CATCH)
    assert "[empty-catch]" in warning(out) and rec["checks"] == ["empty-catch"]


def test_a_catch_that_reports_and_rethrows_is_silent(repo: Path) -> None:
    _, out, _, rec = write(repo, str(repo / "svc.js"), JS_HANDLED_CATCH)
    assert out is None, warning(out)
    assert rec["verdict"] == "ok"


def test_a_swallowed_promise_added_by_an_edit_warns(repo: Path) -> None:
    p = put(repo, "svc.js", JS_CLEAN)
    _, out, _, rec = edit(repo, p, "  return fetch(url);\n", "  return fetch(url).catch(() => null);\n")
    assert "[swallowed-promise]" in warning(out) and rec["checks"] == ["swallowed-promise"]


# --- shell, typed straight into the tool -------------------------------------

def test_or_true_in_a_bash_command_warns(repo: Path) -> None:
    rc, out, _, rec = bash(repo, "npm test || true")
    assert rc == 0
    assert "[ignored-failure]" in warning(out) and "line 1" in warning(out)
    assert rec["verdict"] == "finding" and rec["path"] == "<command>"


def test_or_colon_warns(repo: Path) -> None:
    _, out, _, rec = bash(repo, "npm test || :")
    assert "[ignored-failure]" in warning(out) and rec["checks"] == ["ignored-failure"]


def test_or_with_a_real_handler_is_silent(repo: Path) -> None:
    _, out, _, rec = bash(repo, "npm test || exit 1")
    assert out is None and rec["verdict"] == "ok"


def test_or_echo_is_a_handler_as_the_gate_defines_it(repo: Path) -> None:
    """`|| echo "..."` discards the exit status too, and the gate lets it through on
    purpose: it is the shell shape of `except: log.exception(...)`, the handler that SAYS
    something. The hook inherits that line exactly; this test pins where it is."""
    _, out, _, rec = bash(repo, "npm test || echo \"tests failed\"")
    assert out is None and rec["verdict"] == "ok"


def test_set_plus_e_never_re_armed_warns(repo: Path) -> None:
    _, out, _, rec = bash(repo, "set +e\nmake install")
    assert "[unguarded-set-plus-e]" in warning(out) and rec["lines"] == 2


def test_set_plus_e_re_armed_is_silent(repo: Path) -> None:
    _, out, _, rec = bash(repo, "set +e\nmake install\nset -e")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok" and rec["lines"] == 3


def test_stderr_dropped_in_a_written_script_warns(repo: Path) -> None:
    """In a script nobody is watching, a failure with no message and no status."""
    _, out, _, rec = write(repo, str(repo / "build.sh"),
                           "#!/bin/sh\ngrep -q TODO src/main.c 2>/dev/null\necho carrying on\n")
    assert "[swallowed-stderr]" in warning(out) and rec["checks"] == ["swallowed-stderr"]


def test_stderr_dropped_inside_a_condition_in_a_written_script_is_silent(repo: Path) -> None:
    _, out, _, rec = write(repo, str(repo / "build.sh"),
                           "#!/bin/sh\nif grep -q TODO src/main.c 2>/dev/null; then echo found; fi\n")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok"


def test_stderr_dropped_in_an_interactive_command_is_not_reported(repo: Path) -> None:
    """The decision the measurement forced: on 27,549 real Bash calls this check flagged
    22.5 % - `ls X 2>/dev/null`, `du -sh dist 2>/dev/null`, the ordinary shape of looking
    around, where the agent reads the output and the status is the tool result. The
    check stays on for the shell files the agent writes; it does not run on the tool."""
    _, out, _, rec = bash(repo, "grep -q TODO src/main.c 2>/dev/null\necho carrying on")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok" and rec["lines"] == 2


def test_the_interactive_exemption_does_not_reach_the_other_shell_checks(repo: Path) -> None:
    _, out, _, rec = bash(repo, "du -sh dist 2>/dev/null\nnpm test || true")
    assert "[ignored-failure]" in warning(out) and rec["checks"] == ["ignored-failure"]


def test_a_multiline_command_names_the_line(repo: Path) -> None:
    _, out, _, rec = bash(repo, "echo start\nnpm run build || true\necho end")
    assert "line 2" in warning(out) and rec["lines"] == 3


def test_a_marker_comment_on_the_line_is_honoured(repo: Path) -> None:
    """The gate's narrow escape hatch works at the shell too: the reason travels with the line."""
    _, out, _, rec = bash(repo, "rm -rf .cache || true  # fail-open-ok: the cache may not exist on a first run")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok"


# --- CI / YAML ---------------------------------------------------------------

def test_continue_on_error_in_a_written_workflow_warns(repo: Path) -> None:
    _, out, _, rec = write(repo, str(repo / ".github" / "workflows" / "ci.yml"), YML_CONTINUE_ON_ERROR)
    assert "[continue-on-error]" in warning(out) and rec["checks"] == ["continue-on-error"]


def test_continue_on_error_false_is_silent(repo: Path) -> None:
    _, out, _, rec = write(repo, str(repo / ".github" / "workflows" / "ci.yml"), YML_NO_CONTINUE_ON_ERROR)
    assert out is None and rec["verdict"] == "ok"


# --- write: only what is new is judged ---------------------------------------

def test_a_write_that_keeps_an_old_swallow_and_adds_clean_lines_is_silent(repo: Path) -> None:
    """New = a line the old file did not hold, by exact text. The two blank lines the
    appended text opens with already exist in the file, so only `def` and `return` are new."""
    p = put(repo, "svc.py", PY_OLD_SWALLOW)
    _, out, _, rec = write(repo, p, PY_OLD_SWALLOW + "\n\ndef name(path):\n    return path\n")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok" and rec["lines"] == 2


def test_a_write_that_creates_a_file_judges_every_line(repo: Path) -> None:
    _, out, _, rec = write(repo, str(repo / "svc.py"), PY_EXCEPT_EXCEPTION_PASS)
    assert "[swallowed-exception]" in warning(out)
    assert rec["lines"] == 8, "every line of a file that did not exist is new"


def test_a_file_the_gate_does_not_parse_is_skipped_not_cleared(repo: Path) -> None:
    """Prose about the defect is not the defect - and the receipt says `skipped`, which is
    not the same word as `ok`."""
    rc, out, _, rec = write(repo, str(repo / "NOTES.md"), MD_WITH_A_CODE_BLOCK)
    assert rc == 0 and out is None
    assert rec["verdict"] == "skipped" and rec["lines"] == 0


def test_an_extensionless_file_with_a_shell_shebang_is_judged(repo: Path) -> None:
    _, out, _, rec = write(repo, str(repo / "deploy"), "#!/bin/sh\nset -e\nnpm run build || true\n")
    assert "[ignored-failure]" in warning(out) and rec["verdict"] == "finding"


def test_an_extensionless_file_without_a_shebang_is_skipped(repo: Path) -> None:
    _, out, _, rec = write(repo, str(repo / "Procfile"), "web: npm start || true\n")
    assert out is None and rec["verdict"] == "skipped"


def test_a_file_that_is_not_utf8_is_reported_not_skipped(repo: Path) -> None:
    p = repo / "svc.py"
    p.write_bytes(b"\xff\xfe\x00 = 1\n")
    _, out, _, rec = edit(repo, str(p), "a", "b")
    assert "[unreadable]" in warning(out) and rec["checks"] == ["unreadable"]


# --- the allowlist -----------------------------------------------------------

def test_an_allowlisted_path_is_silent(repo: Path) -> None:
    put(repo, ".conduct/fail-open-allow.txt", "# the deploy script is best-effort by design\n^deploy\\.sh$\n")
    _, out, _, rec = write(repo, str(repo / "deploy.sh"), "#!/bin/sh\nnpm run build || true\n")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok"


def test_an_allowlisted_line_pattern_is_silent(repo: Path) -> None:
    put(repo, ".conduct/fail-open-allow.txt", "rm -rf \\.cache \\|\\| true\n")
    _, out, _, rec = bash(repo, "rm -rf .cache || true")
    assert out is None, warning(out)
    assert rec["verdict"] == "ok"


def test_the_allowlist_does_not_silence_everything(repo: Path) -> None:
    put(repo, ".conduct/fail-open-allow.txt", "^deploy\\.sh$\n")
    _, out, _, _ = bash(repo, "npm test || true")
    assert "[ignored-failure]" in warning(out)


def test_a_broken_allowlist_is_reported_not_ignored(repo: Path) -> None:
    """A malformed allowlist must not silently exempt nothing or everything - the gate's
    rule, and the hook inherits it."""
    put(repo, ".conduct/fail-open-allow.txt", "[unclosed\n")
    _, out, _, rec = bash(repo, "npm test")
    assert "[bad-allowlist]" in warning(out) and rec["checks"] == ["bad-allowlist"]


# --- fail-open and modes -----------------------------------------------------

def test_other_tools_are_ignored_without_a_receipt(repo: Path) -> None:
    p = put(repo, "svc.py", PY_EXCEPT_EXCEPTION_PASS)
    rc, out, _, rec = hook(repo, "Read", {"file_path": p})
    assert rc == 0 and out is None and rec is None


def test_notebook_edit_is_left_out(repo: Path) -> None:
    p = put(repo, "a.ipynb", "{}")
    rc, out, _, rec = hook(repo, "NotebookEdit", {"notebook_path": p, "new_source": "x"})
    assert rc == 0 and out is None and rec is None


def test_an_empty_command_is_ignored(repo: Path) -> None:
    rc, out, _, rec = bash(repo, "   ")
    assert rc == 0 and out is None and rec is None


def test_a_missing_gate_fails_open_with_a_receipt(repo: Path) -> None:
    rc, out, _, rec = bash(repo, "npm test || true", env={"FAIL_OPEN_GATE": str(repo / "none.py")})
    assert rc == 0 and out is None and rec["verdict"] == "error"


def test_block_mode_exits_2_with_the_text_on_stderr(repo: Path) -> None:
    rc, out, err, rec = bash(repo, "npm test || true", env={"FAIL_OPEN_HOOK_MODE": "block"})
    assert rc == 2 and out is None and "Block mode" in err and rec["mode"] == "block"


def test_receipts_can_be_switched_off(repo: Path) -> None:
    rc, out, _, _ = bash(repo, "npm test || true", env={"FAIL_OPEN_RECEIPTS": "off"})
    assert rc == 0 and warning(out)
    assert not (repo / "receipts.jsonl").exists()


# --- the text and the receipt ------------------------------------------------

def test_a_hostile_file_name_is_neutralised_in_the_warning(repo: Path) -> None:
    p = str(repo / "a`b\nc.py")
    _, out, _, _ = write(repo, p, PY_EXCEPT_EXCEPTION_PASS)
    w = warning(out)
    assert "`a?b?c.py`" in w and "\n" not in w


def test_the_warning_stays_far_below_the_runtime_cap(repo: Path) -> None:
    _, out, _, rec = bash(repo, "\n".join(f"step{i} || true" for i in range(30)))
    assert "(+26 more)" in warning(out) and rec["findings"] == 30
    assert 0 < len(warning(out)) < 2000                # the runtime caps hook output at 10,000


def test_the_receipt_carries_what_the_measurement_needs_and_never_the_file(repo: Path) -> None:
    p = put(repo, "svc.py", PY_CLEAN)
    edit(repo, p, "    return json.loads(raw)\n",
         "    try:\n        return json.loads(raw)\n    except Exception:\n        pass\n")
    raw = (repo / "receipts.jsonl").read_text(encoding="utf-8").strip().split("\n")[-1]
    rec = json.loads(raw)
    for key in ("ts", "session", "tool", "path", "verdict", "checks", "findings", "lines", "ms", "mode"):
        assert key in rec, (key, rec)
    assert "json.loads" not in raw and "import" not in raw, "a receipt never carries file contents"


def test_judge_is_importable_for_measurement(repo: Path) -> None:
    """The measurement over recorded sessions calls `judge` directly, one process for all."""
    spec = importlib.util.spec_from_file_location("fail_open_hook_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    findings, judged = mod.judge("Bash", {"command": "npm test || true"}, str(repo))
    assert judged == 1 and [f.check for f in findings] == ["ignored-failure"]
    p = put(repo, "svc.py", PY_OLD_SWALLOW)
    findings, judged = mod.judge("Edit", {"file_path": p, "old_string": "    return 1\n",
                                          "new_string": "    return len(path)\n"}, str(repo))
    assert (findings, judged) == ([], 1)
    assert mod.judge("Write", {"file_path": str(repo / "x.go"), "content": "x"}, str(repo)) == ([], 0)
