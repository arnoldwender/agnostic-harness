#!/usr/bin/env python3
"""Prove the citation gate's tests actually defend it.

    python3 tests/mutation_check_citations.py

For each mechanism in gate/citations.py: neuter it, run the suite, and require
the suite to go RED. A test that still passes with the mechanism removed is not
testing the mechanism — it is decoration that reports green forever, which is
the same fail-open defect the codex forbids, one layer up.

Kept separate from tests/mutation_check.py on purpose. That file defends this
repo's OWN signature gate; this one defends the file the conduct-harness family
shares. Two gates, two mutation runners, so a stale anchor in one cannot quietly
disable the other.

THREE OF THE MUTANTS ARE SHAPES, NOT CHECKS, and they are the important ones.
A deleted check reports a finding it should have caught — bad, and visible. A
blind extractor reports "0 attributed quotations — clean" and hands out a green
badge over a pool nobody ever read. That is not hypothetical: the older version
of this gate required every quotation to be wrapped in quotes or italics, and
the pools of the Ihsan and Angelical editions are written as bare bullets. On
those repos it would have found nothing and exited 0.

Exit 0 when every mutant was killed; 1 when any survived; 2 when this script
itself could not run (the same contract as the gate).

The gate file is restored from an in-memory copy in a `finally`, never with
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
GATE = ROOT / "gate" / "citations.py"

# (name, exact text to replace, replacement)
#
# Every anchor must appear EXACTLY ONCE in the gate. When one stops matching,
# this script says so and counts it as a survivor rather than skipping it —
# a mutation runner whose anchors have rotted measures nothing and must not be
# allowed to report success.
MUTANTS = [
    ("CHECK 1 unsourced-quote",
     "        checked = check_quotes_resolve(sources, findings)",
     "        checked = 0"),
    ("CHECK 2 incomplete-provenance",
     "        check_provenance_complete(sources, findings)",
     "        pass"),
    ("CHECK 3 anachronism",
     "        check_anachronism(sources, findings)",
     "        pass"),
    ("CHECK 4 pd-claim",
     "        check_pd_status(sources, findings)",
     "        pass"),
    # SHAPE B: stop recognising the bullet form, so pool bullets fall back into
    # the delimiter-requiring flowing path and vanish from the gate's view.
    ("SHAPE B undelimited bullet is a citation",
     '        bullet = body.startswith("- ")',
     "        bullet = False"),
    # SHAPE B, the other half: require the delimiter everywhere, which is the
    # exact state that made the gate blind to two whole pools.
    ("SHAPE B delimiter not required on a bullet",
     "        if not bullet and not _delimited(quote):",
     "        if not _delimited(quote):"),
    # SHAPE C: keep recognising the attribution line, but never emit the pair.
    ("SHAPE C attribution on its own line",
     '                    out.append((lineno, quote, body.lstrip("—– ").strip()))',
     "                    pass"),
    # The translator carries a copyright term of their own. Both of these exist
    # because reasoning only about the author gets the EU answer wrong.
    ("translator death year is required",
     '        if s.data.get("translator") and "translator_died" not in s.data:',
     "        if False:"),
    ("edition dated after the translator died",
     "        if isinstance(tdied, int) and isinstance(year, int) and year > tdied:",
     "        if False:"),
    # Not a check, a disclosure: an unverified source is an honest outcome, but
    # a count that only appeared on red runs would let the pile grow unwatched.
    ("the unverified count is printed on every run",
     "    if unverified:",
     "    if False:"),
]


def run_suite() -> bool:
    """True when the suite is green."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "tests" / "test_citation_gate.py"),
         "-q", "-x", "--no-header"],
        capture_output=True, text=True, cwd=ROOT, check=False)
    return result.returncode == 0


def main() -> int:
    if not GATE.is_file():
        print(f"no gate at {GATE}", file=sys.stderr)
        return 2
    original = GATE.read_text(encoding="utf-8")

    if not run_suite():
        print("the suite is RED before any mutation — fix that first", file=sys.stderr)
        return 2

    survivors: list[str] = []
    try:
        for name, old, new in MUTANTS:
            count = original.count(old)
            if count != 1:
                print(f"  ?? {name}: anchor appears {count} times in the gate — the "
                      f"mutation list is stale, so this script is measuring nothing")
                survivors.append(f"{name} (stale anchor)")
                continue
            GATE.write_text(original.replace(old, new, 1), encoding="utf-8")
            if run_suite():
                print(f"  SURVIVED  {name} — removed it and the suite stayed green")
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
