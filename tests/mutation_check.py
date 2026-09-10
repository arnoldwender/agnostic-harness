#!/usr/bin/env python3
"""Prove the fail-open gate's tests actually defend it.

    python3 tests/mutation_check.py

For each check in gate/fail_open.py: delete it, run the suite, and require the
suite to go RED. A test that still passes with the mechanism removed is not
testing the mechanism - it is decoration that reports green forever, which is
the same fail-open defect the gate itself hunts, one layer up.

Exit 0 when every mutant was killed; 1 when any survived; 2 when this script
itself could not run (the same contract as the gate).

The file is restored from an in-memory copy in a `finally`, never with
`git checkout`: this repo may hold uncommitted work, and a checkout to undo a
mutation would take that work with it. The restore is then verified, because a
mutation runner that leaves the gate mutated has done more damage than the bug
it was hunting.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "gate" / "fail_open.py"

# (name, the call to neuter, what to put in its place)
MUTANTS = [
    ("python swallowed-exception",
     "check_swallowed_exception(tree, rel, added, findings, lines)", ""),
    ("python return-in-finally",
     "check_return_in_finally(tree, rel, added, findings, lines)", ""),
    ("python green-default",
     "check_green_default_py(tree, rel, added, findings, lines)", ""),
    ("python unparseable report",
     "report_unparseable(rel, exc, findings, lines)", ""),
    ("js empty-catch",
     "check_empty_catch(rel, text, mask, added, findings, lines)", ""),
    ("js swallowed-promise",
     "check_swallowed_promise(rel, text, mask, added, findings, lines)", ""),
    ("js green-default",
     "check_green_default_js(rel, text, mask, added, findings, lines)", ""),
    ("shell ignored-failure",
     "check_ignored_failure(rel, lines, masked, added, findings)", ""),
    ("shell swallowed-stderr",
     "check_swallowed_stderr(rel, lines, masked, added, findings)", ""),
    ("shell unguarded-set-plus-e",
     "check_unguarded_set_e(rel, lines, masked, added, findings)", ""),
    ("ci continue-on-error",
     "check_continue_on_error(rel, lines, added, findings)", ""),
    ("ci always-on-verdict",
     "check_always_on_verdict(rel, lines, added, findings)", ""),
    # The two escape hatches are mechanisms too. Remove them and the gate starts
    # firing on lines an author deliberately accepted - a different failure, and
    # one that gets the gate deleted rather than fixed.
    ("the fail-open-ok marker",
     "findings[before:] = [f for f in findings[before:] if f.line not in marks]",
     "findings[before:] = list(findings[before:])"),
    ("the marker scan",
     "marks = marked_lines(comment_view(lang, text), text.splitlines(), rel, findings)",
     "marks = set()"),
    ("the allowlist",
     "findings = [f for f in findings if not _allowed(f, allow)]",
     "findings = list(findings)"),
]


def run_suite() -> bool:
    """True when the suite is green."""
    proc = subprocess.run([sys.executable, "-m", "pytest", str(ROOT / "tests"), "-q",
                           "-x", "--no-header"],
                          capture_output=True, text=True, cwd=ROOT, check=False)
    return proc.returncode == 0


def main() -> int:
    original = GATE.read_text(encoding="utf-8")

    if not run_suite():
        print("the suite is RED before any mutation - fix that first", file=sys.stderr)
        return 2

    survivors: list[str] = []
    try:
        for name, call, replacement in MUTANTS:
            if original.count(call) != 1:
                print(f"  ?? {name}: the call appears {original.count(call)} times in "
                      f"the gate - the mutation list is stale")
                survivors.append(f"{name} (stale)")
                continue
            GATE.write_text(original.replace(call, replacement or "pass", 1),
                            encoding="utf-8")
            if run_suite():
                print(f"  SURVIVED  {name} - removed it and the suite stayed green")
                survivors.append(name)
            else:
                print(f"  killed    {name}")
    finally:
        GATE.write_text(original, encoding="utf-8")

    if GATE.read_text(encoding="utf-8") != original:
        print("the gate file was NOT restored cleanly", file=sys.stderr)
        return 2

    if survivors:
        print(f"\n{len(survivors)} mutant(s) survived: {', '.join(survivors)}")
        return 1
    print(f"\nall {len(MUTANTS)} mutants killed; gate restored and verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
