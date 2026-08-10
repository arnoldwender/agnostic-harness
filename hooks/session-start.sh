#!/usr/bin/env sh
# The Agnostic Harness — session-start hook.
#
# Opens every session with the maxim and keeps the four conduct axes present.
# Its stdout is meant to be injected into the agent's context at the start of a
# session (e.g. a Claude Code `SessionStart` hook), and it is also just readable
# output for any harness that can run a startup command. See hooks/README.md.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# 1) The maxim — the first utterance (opening creed + the maxim of the day).
"$ROOT/bin/maxim"

# 2) The conduct axes — kept present in context, every session.
echo ""
cat "$ROOT/codex-block.md"
