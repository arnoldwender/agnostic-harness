#!/usr/bin/env python3
"""Prove the live hook's tests actually defend it.

    python3 tests/mutation_check_fail_open_hook.py

For each mechanism in hooks/fail-open-before-run.py: neuter it in a scratch
COPY, run the hook's suite against the copy, and require the suite to go RED.
The real hook is never rewritten; its bytes are compared before and after
anyway. The gate's own checkers are defended by tests/mutation_check.py; this
runner covers only what the hook adds on top of the gate — which tools it
judges, how it simulates the change, which lines count as new, and the two
escape hatches it must honour.

Exit 0 when every mutant was killed; 1 when any survived; 2 when this script
itself could not run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "fail-open-before-run.py"
SUITE = ROOT / "tests" / "test_fail_open_hook.py"

# (name, exact text to replace, replacement). Each `old` is unique in the file.
MUTANTS = [
    ("TOOLS every tool is judged, not only the four",
     'TOOLS = {"Bash", "Edit", "Write", "MultiEdit"}',
     'TOOLS = {"Bash", "Edit", "Write", "MultiEdit", "Read", "NotebookEdit"}'),
    ("SIMULATE the edit is never simulated; new_string is judged on its own",
     '        hits = _occurrences(text, old, bool(e.get("replace_all"))) if old else []',
     "        hits = []"),
    ("REPLACE_ALL only the first occurrence is replaced",
     '        hits = _occurrences(text, old, bool(e.get("replace_all"))) if old else []',
     "        hits = _occurrences(text, old, False) if old else []"),
    ("ADDED the whole file is judged after an edit, not the lines the edit wrote",
     "    return target, rel, text, added_from_origin(text, origin), meta",
     "    return target, rel, text, set(range(1, real_lines(text) + 1)), meta"),
    ("WRITE every line of a rewritten file is judged, not only the new ones",
     "        return target, rel, content, added_by_write(current, content), {}",
     "        return target, rel, content, set(range(1, real_lines(content) + 1)), {}"),
    ("SCOPE a file the gate does not parse is judged as shell",
     '    if not ext and SHEBANG.match(text.split("\\n", 1)[0]):\n        return "shell"\n    return None',
     '    return "shell"'),
    ("INTERACTIVE swallowed-stderr fires on a command typed into the tool",
     "        findings[before:] = [f for f in findings[before:] if f.check not in INTERACTIVE_SKIP]\n"
     "        # mutation-anchor: interactive",
     "        pass\n        # mutation-anchor: interactive"),
    ("MARKER a fail-open-ok comment on the line is ignored",
     "    findings[before:] = [f for f in findings[before:] if f.line not in marks]",
     "    findings[before:] = findings[before:]"),
    ("ALLOWLIST the working directory's allowlist is ignored",
     "    findings = [f for f in findings if not ro._allowed(f, allow)]",
     "    findings = list(findings)"),
    ("WARNING a finding produces no text",
     '    if not findings:\n        receipt(verdict="ok", **common)\n        return 0',
     '    if True:\n        receipt(verdict="ok", **common)\n        return 0'),
]


def run_suite(hook: Path) -> bool:
    # The mutant lives in a scratch directory with no gate/ beside it: it must still
    # find the REAL gate, or every mutant dies of fail-open and this measures nothing.
    env = {**os.environ, "FAIL_OPEN_HOOK_UNDER_TEST": str(hook),
           "FAIL_OPEN_GATE": str(ROOT / "gate" / "fail_open.py")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(SUITE), "-q", "-x", "--no-header",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=ROOT, env=env, check=False)
    return result.returncode == 0


def main() -> int:
    original = HOOK.read_text(encoding="utf-8")
    if not run_suite(HOOK):
        print("the suite is RED before any mutation — fix that first", file=sys.stderr)
        return 2
    survivors: list[str] = []
    with tempfile.TemporaryDirectory() as scratch:
        mutant = Path(scratch) / "fail-open-before-run.py"
        for name, old, new in MUTANTS:
            if original.count(old) != 1:
                print(f"  ?? {name}: anchor appears {original.count(old)} times — the "
                      f"mutation list is stale, so this script is measuring nothing")
                survivors.append(f"{name} (stale)")
                continue
            mutant.write_text(original.replace(old, new, 1), encoding="utf-8")
            if run_suite(mutant):
                print(f"  SURVIVED  {name} — removed it and the suite stayed green")
                survivors.append(name)
            else:
                print(f"  killed    {name}")
    if HOOK.read_text(encoding="utf-8") != original:
        print("the real hook file changed during the run — it must never be touched",
              file=sys.stderr)
        return 2
    if survivors:
        print(f"\n{len(survivors)} mutant(s) survived: {', '.join(survivors)}")
        return 1
    print(f"\nall {len(MUTANTS)} mutants killed; the real hook was never rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
