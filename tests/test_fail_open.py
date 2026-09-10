"""Tests for the fail-open gate.

Every check gets the same treatment: plant the defect the check exists to
catch and require exit 1, then write the same code with the error actually
handled and require exit 0. A test that only ever sees the defect proves the
gate is loud; a test that only ever sees clean code proves nothing at all.

    python3 -m pytest tests/ -q

The gate runs as a subprocess rather than as an import, because the exit code
is part of the contract the whole conduct-harness family shares (0 clean, 1
findings, 2 the gate itself broke). Importing would exercise the functions and
leave the contract untested - and for this gate in particular the exit code IS
the thesis.

Each fixture repo is a real git repository, because the gate's input is a real
diff. Global and system git config are pointed at /dev/null so a developer's
own settings (hooks, signing, excludesFile) cannot change the result.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "gate" / "fail_open.py"

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Gate Test",
    "GIT_AUTHOR_EMAIL": "gate@example.invalid",
    "GIT_COMMITTER_NAME": "Gate Test",
    "GIT_COMMITTER_EMAIL": "gate@example.invalid",
}


# --- harness -----------------------------------------------------------------

def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, env={**os.environ, **GIT_ENV}, check=False)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout


def write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **GIT_ENV, "HARNESS_ROOT": str(repo)}
    return subprocess.run([sys.executable, str(GATE), *args], capture_output=True,
                          text=True, env=env, cwd=str(repo), check=False)


def check(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(repo, "--base", "HEAD", *args)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo with one clean commit, so HEAD is a usable base."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, "NOTES.txt", "baseline\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "baseline")
    return root


# --- fixtures: python --------------------------------------------------------

PY_LOUD = '''\
def load(path):
    """Read a file. If it is not there, the caller finds out."""
    with open(path, encoding="utf-8") as fh:
        return fh.read()
'''

PY_BARE_EXCEPT = '''\
def parse(raw):
    try:
        return int(raw)
    except:
        pass
'''

PY_EXCEPT_EXCEPTION_PASS = '''\
import json


def parse(raw):
    try:
        return json.loads(raw)
    except Exception:
        pass
'''

PY_EXCEPT_LOGGED = '''\
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

PY_EXCEPT_RERAISES = '''\
import json


def parse(raw):
    try:
        return json.loads(raw)
    except ValueError:
        raise
'''

PY_EXCEPT_CONTINUE = '''\
def parse_all(items):
    out = []
    for raw in items:
        try:
            out.append(int(raw))
        except ValueError:
            continue
    return out
'''

PY_EXCEPT_CONTINUE_LOGGED = '''\
import logging

log = logging.getLogger(__name__)


def parse_all(items):
    out = []
    for raw in items:
        try:
            out.append(int(raw))
        except ValueError:
            log.warning("skipping an unparsable item: %r", raw)
            continue
    return out
'''

PY_RETURN_IN_FINALLY = '''\
def close(handle):
    try:
        handle.flush()
    finally:
        return True
'''

PY_FINALLY_CLEANS_UP = '''\
def close(handle):
    try:
        handle.flush()
    finally:
        handle.close()
    return True
'''

PY_GREEN_STATUS = '''\
def verdict(data):
    status = data.get("status", "ok")
    return status == "ok"
'''

PY_GREEN_VALID = '''\
def verdict(resp):
    is_valid = resp.get("valid", True)
    return is_valid
'''

PY_GREEN_ERRORS = '''\
SUCCESS = "SUCCESS"
FAILURE = "FAILURE"


def verdict(resp):
    errors = resp.get("errors") or []
    if not errors:
        return SUCCESS
    return FAILURE
'''

PY_LOUD_LOOKUP = '''\
def verdict(data):
    status = data.get("status", "missing")
    if status == "missing":
        raise KeyError("the response carried no status field")
    return status == "ok"
'''

PY_PATTERN_IN_A_STRING = '''\
BAD = "except Exception:\\n    pass"


def describe():
    return BAD
'''

PY_PATTERN_IN_A_DOCSTRING = '''\
def parse(raw):
    """Never write this:

    try:
        return int(raw)
    except Exception:
        pass
    """
    return int(raw)
'''

PY_MARKER_INSIDE_A_STRING = '''\
SNIPPET = """
# fail-open-ok:begin
rm -rf .cache || true
# fail-open-ok:end
"""


def parse(raw):
    try:
        return int(raw)
    except ValueError:
        pass
'''

PY_DELETABLE = '''\
def size(path):
    return 1


def quiet(path):
    try:
        return size(path)
    except OSError:
        pass
'''

PY_DELETABLE_TRIMMED = '''\
def size(path):
    return 1
'''

PY_INVALID_SYNTAX = '''\
def parse(:
    return 1
'''


# --- fixtures: javascript / typescript ---------------------------------------

JS_EMPTY_CATCH = """\
export async function load(url) {
  try {
    return await fetch(url);
  } catch {
  }
}
"""

JS_UNUSED_ERROR = """\
export function parse(raw) {
  try {
    return JSON.parse(raw);
  } catch (err) {
    return null;
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

JS_PROMISE_EMPTY = """\
export function ping(url) {
  return fetch(url).catch(() => {});
}
"""

JS_PROMISE_NULL = """\
export function ping(url) {
  return fetch(url).catch(() => null);
}
"""

JS_PROMISE_HANDLED = """\
export function ping(url, report) {
  return fetch(url).catch((err) => {
    report(err);
    throw err;
  });
}
"""

JS_GREEN_DEFAULT = """\
export function verdict(payload) {
  const status = payload.status ?? "ok";
  return status === "ok";
}
"""

JS_LOUD_DEFAULT = """\
export function verdict(payload) {
  const status = payload.status ?? "unknown";
  if (status === "unknown") {
    throw new Error("the payload carried no status field");
  }
  return status === "ok";
}
"""

JS_PATTERN_IN_A_STRING = """\
const HINT = "never ship an empty catch {} block";

export function describe() {
  return HINT;
}
"""


# --- fixtures: shell ---------------------------------------------------------

SH_OR_TRUE = """\
#!/bin/sh
set -e
npm run build || true
"""

SH_OR_COLON = """\
#!/bin/sh
set -e
npm run build || :
"""

SH_OR_TRUE_MARKED = """\
#!/bin/sh
set -e
# fail-open-ok: the cache may legitimately not exist on a first run
rm -rf .cache || true
"""

SH_OR_HANDLER = """\
#!/bin/sh
set -e
npm run build || exit 1
"""

SH_MARKED_REGION = """\
#!/bin/sh
set -e
# fail-open-ok:begin - best-effort cleanup, none of it decides anything
rm -rf .cache || true
rm -rf .tmp || :
# fail-open-ok:end
npm run build
"""

SH_UNCLOSED_REGION = """\
#!/bin/sh
set -e
# fail-open-ok:begin - best-effort cleanup
rm -rf .cache || true
npm run build || true
"""

SH_MARKER_ONLY_NAMED = """\
#!/bin/sh
set -e
echo "the marker for this gate is spelled fail-open-ok"
npm run build || true
"""

SH_STDERR_DROPPED = """\
#!/bin/sh
grep -q TODO src/main.c 2>/dev/null
echo "carrying on"
"""

SH_STDERR_IN_A_CONDITION = """\
#!/bin/sh
if grep -q TODO src/main.c 2>/dev/null; then
  echo "found one"
fi
"""

SH_SET_PLUS_E = """\
#!/bin/sh
set -e
set +e
make install
"""

SH_SET_PLUS_E_REARMED = """\
#!/bin/sh
set -e
set +e
make install
set -e
"""


# --- fixtures: CI / YAML -----------------------------------------------------

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

YML_ALWAYS_ON_A_VERDICT = """\
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Lint
        run: eslint .
      - name: Tests
        if: always()
        run: pytest -q
"""

YML_ALWAYS_ON_AN_UPLOAD = """\
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Tests
        run: pytest -q
      - name: Upload the report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: report
          path: report.json
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


# --- the control -------------------------------------------------------------

def test_untouched_repo_passes(repo: Path) -> None:
    """Without this, every test below could pass because the gate always fails."""
    r = check(repo)
    assert r.returncode == 0, r.stdout
    assert "no added line swallows an error" in r.stdout


def test_a_clean_added_file_passes_and_was_actually_scanned(repo: Path) -> None:
    """Green is only meaningful if the file was read. The count says it was."""
    write(repo, "loader.py", PY_LOUD)
    r = check(repo)
    assert r.returncode == 0, r.stdout
    assert "1 file(s) scanned" in r.stdout


# --- python: the swallowed exception -----------------------------------------

def test_bare_except_pass_is_caught(repo: Path) -> None:
    write(repo, "svc.py", PY_BARE_EXCEPT)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "swallowed-exception" in r.stdout


def test_except_exception_pass_is_caught(repo: Path) -> None:
    write(repo, "svc.py", PY_EXCEPT_EXCEPTION_PASS)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "swallowed-exception" in r.stdout


def test_except_exception_that_logs_passes(repo: Path) -> None:
    write(repo, "svc.py", PY_EXCEPT_LOGGED)
    r = check(repo)
    assert r.returncode == 0, r.stdout


def test_except_that_reraises_passes(repo: Path) -> None:
    write(repo, "svc.py", PY_EXCEPT_RERAISES)
    r = check(repo)
    assert r.returncode == 0, r.stdout


def test_except_continue_without_a_log_is_caught(repo: Path) -> None:
    write(repo, "svc.py", PY_EXCEPT_CONTINUE)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "swallowed-exception" in r.stdout


def test_except_continue_with_a_log_passes(repo: Path) -> None:
    write(repo, "svc.py", PY_EXCEPT_CONTINUE_LOGGED)
    r = check(repo)
    assert r.returncode == 0, r.stdout


# --- python: return in finally -----------------------------------------------

def test_return_inside_finally_is_caught(repo: Path) -> None:
    write(repo, "svc.py", PY_RETURN_IN_FINALLY)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "return-in-finally" in r.stdout


def test_finally_that_only_cleans_up_passes(repo: Path) -> None:
    write(repo, "svc.py", PY_FINALLY_CLEANS_UP)
    r = check(repo)
    assert r.returncode == 0, r.stdout


# --- python: the default that means "all is well" ----------------------------

def test_status_defaulting_to_ok_is_caught(repo: Path) -> None:
    write(repo, "svc.py", PY_GREEN_STATUS)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "green-default" in r.stdout


def test_valid_defaulting_to_true_is_caught(repo: Path) -> None:
    write(repo, "svc.py", PY_GREEN_VALID)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "green-default" in r.stdout


def test_errors_defaulting_to_an_empty_list_is_caught(repo: Path) -> None:
    write(repo, "svc.py", PY_GREEN_ERRORS)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "green-default" in r.stdout


def test_a_default_that_does_not_mean_success_passes(repo: Path) -> None:
    write(repo, "svc.py", PY_LOUD_LOOKUP)
    r = check(repo)
    assert r.returncode == 0, r.stdout


# --- javascript / typescript -------------------------------------------------

def test_empty_catch_block_is_caught(repo: Path) -> None:
    write(repo, "svc.ts", JS_EMPTY_CATCH)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "empty-catch" in r.stdout


def test_catch_that_never_touches_the_error_is_caught(repo: Path) -> None:
    write(repo, "svc.js", JS_UNUSED_ERROR)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "empty-catch" in r.stdout


def test_catch_that_uses_the_error_passes(repo: Path) -> None:
    write(repo, "svc.js", JS_HANDLED_CATCH)
    r = check(repo)
    assert r.returncode == 0, r.stdout


def test_promise_catch_with_an_empty_body_is_caught(repo: Path) -> None:
    write(repo, "svc.js", JS_PROMISE_EMPTY)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "swallowed-promise" in r.stdout


def test_promise_catch_returning_null_is_caught(repo: Path) -> None:
    write(repo, "svc.js", JS_PROMISE_NULL)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "swallowed-promise" in r.stdout


def test_promise_catch_that_reports_and_rethrows_passes(repo: Path) -> None:
    write(repo, "svc.js", JS_PROMISE_HANDLED)
    r = check(repo)
    assert r.returncode == 0, r.stdout


def test_nullish_default_meaning_ok_is_caught(repo: Path) -> None:
    write(repo, "svc.ts", JS_GREEN_DEFAULT)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "green-default" in r.stdout


def test_nullish_default_that_forces_a_decision_passes(repo: Path) -> None:
    write(repo, "svc.ts", JS_LOUD_DEFAULT)
    r = check(repo)
    assert r.returncode == 0, r.stdout


# --- shell -------------------------------------------------------------------

def test_or_true_is_caught(repo: Path) -> None:
    write(repo, "build.sh", SH_OR_TRUE)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "ignored-failure" in r.stdout


def test_or_colon_is_caught(repo: Path) -> None:
    write(repo, "build.sh", SH_OR_COLON)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "ignored-failure" in r.stdout


def test_or_true_inside_a_marked_block_passes(repo: Path) -> None:
    """The escape hatch exists so the gate survives contact with real scripts."""
    write(repo, "build.sh", SH_OR_TRUE_MARKED)
    r = check(repo)
    assert r.returncode == 0, r.stdout


def test_or_with_a_real_handler_passes(repo: Path) -> None:
    write(repo, "build.sh", SH_OR_HANDLER)
    r = check(repo)
    assert r.returncode == 0, r.stdout


def test_a_closed_marked_region_suppresses_everything_inside_it(repo: Path) -> None:
    write(repo, "build.sh", SH_MARKED_REGION)
    r = check(repo)
    assert r.returncode == 0, r.stdout


def test_an_unclosed_marked_region_is_reported_and_suppresses_nothing(repo: Path) -> None:
    """An unbounded region would be a silent kill switch for the rest of the
    file - the exact defect this gate is named after, inside the gate."""
    write(repo, "build.sh", SH_UNCLOSED_REGION)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "unclosed-marker" in r.stdout
    assert "ignored-failure" in r.stdout, "the unclosed region silenced the checks"


def test_naming_the_marker_outside_a_comment_does_not_suppress(repo: Path) -> None:
    """Talking about the marker must not switch the gate off."""
    write(repo, "build.sh", SH_MARKER_ONLY_NAMED)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "ignored-failure" in r.stdout


def test_a_marker_inside_a_python_string_is_not_a_directive(repo: Path) -> None:
    """A shell snippet quoted inside Python is data, not a directive - and the
    real defect further down the same file must still be reported."""
    write(repo, "svc.py", PY_MARKER_INSIDE_A_STRING)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "swallowed-exception" in r.stdout
    assert "unclosed-marker" not in r.stdout


def test_stderr_dropped_on_an_unchecked_command_is_caught(repo: Path) -> None:
    write(repo, "build.sh", SH_STDERR_DROPPED)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "swallowed-stderr" in r.stdout


def test_stderr_dropped_inside_a_condition_passes(repo: Path) -> None:
    """There the exit status IS the condition, so nothing is being ignored."""
    write(repo, "build.sh", SH_STDERR_IN_A_CONDITION)
    r = check(repo)
    assert r.returncode == 0, r.stdout


def test_set_plus_e_that_is_never_re_armed_is_caught(repo: Path) -> None:
    write(repo, "build.sh", SH_SET_PLUS_E)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "unguarded-set-plus-e" in r.stdout


def test_set_plus_e_followed_by_set_minus_e_passes(repo: Path) -> None:
    write(repo, "build.sh", SH_SET_PLUS_E_REARMED)
    r = check(repo)
    assert r.returncode == 0, r.stdout


# --- CI / YAML ---------------------------------------------------------------

def test_continue_on_error_true_is_caught(repo: Path) -> None:
    write(repo, "ci.yml", YML_CONTINUE_ON_ERROR)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "continue-on-error" in r.stdout


def test_continue_on_error_false_passes(repo: Path) -> None:
    write(repo, "ci.yml", YML_NO_CONTINUE_ON_ERROR)
    r = check(repo)
    assert r.returncode == 0, r.stdout


def test_always_on_a_step_that_decides_the_verdict_is_caught(repo: Path) -> None:
    write(repo, "ci.yml", YML_ALWAYS_ON_A_VERDICT)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "always-on-verdict" in r.stdout


def test_always_on_an_artifact_upload_passes(repo: Path) -> None:
    """Uploading a report after a failure is the point of `always()`."""
    write(repo, "ci.yml", YML_ALWAYS_ON_AN_UPLOAD)
    r = check(repo)
    assert r.returncode == 0, r.stdout


# --- false positives: the reason a gate survives contact with users ----------

def test_pattern_inside_a_python_string_passes(repo: Path) -> None:
    write(repo, "svc.py", PY_PATTERN_IN_A_STRING)
    r = check(repo)
    assert r.returncode == 0, r.stdout


def test_pattern_inside_a_docstring_passes(repo: Path) -> None:
    write(repo, "svc.py", PY_PATTERN_IN_A_DOCSTRING)
    r = check(repo)
    assert r.returncode == 0, r.stdout


def test_pattern_inside_a_javascript_string_passes(repo: Path) -> None:
    write(repo, "svc.js", JS_PATTERN_IN_A_STRING)
    r = check(repo)
    assert r.returncode == 0, r.stdout


def test_pattern_inside_a_markdown_code_block_passes(repo: Path) -> None:
    """Prose about the defect is not the defect. Markdown is documentation."""
    write(repo, "NOTES.md", MD_WITH_A_CODE_BLOCK)
    r = check(repo)
    assert r.returncode == 0, r.stdout
    assert "no language this gate parses" in r.stdout


def test_deleted_lines_carrying_the_pattern_pass(repo: Path) -> None:
    """The gate reads added lines. Removing the defect must never report it."""
    write(repo, "quiet.py", PY_DELETABLE)
    assert check(repo).returncode == 1, "the fixture must be a real finding first"
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "history carries the defect")
    write(repo, "quiet.py", PY_DELETABLE_TRIMMED)
    r = check(repo)
    assert r.returncode == 0, r.stdout


# --- the contract: the gate's own failure is never a verdict -----------------

def test_invalid_python_is_reported_and_the_other_files_still_checked(repo: Path) -> None:
    """A file the gate cannot parse is a finding, not a shrug - and it must not
    stop the run, or one bad file would hide every defect behind it."""
    write(repo, "broken.py", PY_INVALID_SYNTAX)
    write(repo, "svc.py", PY_EXCEPT_EXCEPTION_PASS)
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "unparseable" in r.stdout
    assert "swallowed-exception" in r.stdout, "the second file was never checked"


def test_an_unresolvable_base_ref_is_exit_2_not_exit_0(repo: Path) -> None:
    """The whole point: when the gate cannot read its input it says so. Exit 0
    would read as 'clean' and exit 1 as 'I found something'. Both would lie."""
    r = run(repo, "--base", "refs/heads/no-such-branch")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "does not resolve" in r.stderr


# --- the allowlist -----------------------------------------------------------

def test_allowlist_entry_suppresses_a_finding(repo: Path) -> None:
    write(repo, "deploy.sh", SH_OR_TRUE)
    assert check(repo).returncode == 1
    write(repo, ".conduct/fail-open-allow.txt", "# on purpose\n^deploy\\.sh$\n")
    r = check(repo)
    assert r.returncode == 0, r.stdout


def test_a_broken_allowlist_regex_is_reported(repo: Path) -> None:
    """A malformed allowlist must not silently exempt nothing or everything."""
    write(repo, ".conduct/fail-open-allow.txt", "[unclosed\n")
    r = check(repo)
    assert r.returncode == 1, r.stdout
    assert "bad-allowlist" in r.stdout


# --- --files mode ------------------------------------------------------------

def test_files_mode_checks_the_named_file_whole(repo: Path) -> None:
    write(repo, "svc.py", PY_EXCEPT_EXCEPTION_PASS)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "committed, so the diff is empty")
    assert check(repo).returncode == 0, "the diff really is empty"
    r = run(repo, "--files", "svc.py")
    assert r.returncode == 1, r.stdout
    assert "swallowed-exception" in r.stdout


def test_files_mode_reports_a_file_it_cannot_read(repo: Path) -> None:
    r = run(repo, "--files", "no-such-file.py")
    assert r.returncode == 1, r.stdout
    assert "missing-file" in r.stdout


# --- SARIF -------------------------------------------------------------------

def test_sarif_is_written_and_well_formed(repo: Path, tmp_path: Path) -> None:
    write(repo, "svc.py", PY_EXCEPT_EXCEPTION_PASS)
    out = tmp_path / "out.sarif"
    r = check(repo, "--sarif", str(out))
    assert r.returncode == 1, r.stdout
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["version"] == "2.1.0"
    assert doc["runs"][0]["results"], "SARIF carries no results for a failing run"
    assert doc["runs"][0]["results"][0]["ruleId"] == "swallowed-exception"
    assert doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"][
        "artifactLocation"]["uri"] == "svc.py"
