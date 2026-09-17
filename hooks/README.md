# Hooks — keeping the axes present

The Codex only works if it's *in context* when the agent acts. A one-time paste into
`AGENTS.md` works; a hook makes it automatic, every session, and opens each run with the
maxim.

## `session-start.sh`

Emits, to stdout:

1. The **opening maxim** + a rotating **maxim of the day** (`bin/maxim`, drawn from
   `maxims.txt`).
2. The **conduct block** — the four axes, precedence, and the gate limit (`codex-block.md`).

It's harness-agnostic: any harness that can run a command at session start can use it, and
its stdout is plain readable text.

## Wiring it into Claude Code

Claude Code injects a `SessionStart` hook's stdout into the session context. Add to your
`settings.json` (use the **absolute** path, and check your Claude Code version's hook docs —
the schema evolves):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "/abs/path/to/agnostic-harness/hooks/session-start.sh" }
        ]
      }
    ]
  }
}
```

## Wiring it into any other harness

Run `hooks/session-start.sh` as the first step of your session bootstrap and prepend its
output to the system prompt. The maxim goes first, the axes stay present.

## `fail-open-before-run.py` — the gate, before the error is swallowed

A Claude Code `PreToolUse` hook for `Bash`, `Edit`, `Write` and `MultiEdit`. Before the tool
runs, it hands that one change to [`gate/fail_open.py`](../gate/fail_open.py) — the same
checkers, the same `fail-open-ok` marker, the same allowlist — and, if the change swallows an
error, tells the agent so in the tool result. It **warns**; it does not block:

> fail-open: this command swallows an error before anyone can see it. [ignored-failure] line 1:
> `|| true` discards the exit status - this line reports success whatever happened. If the
> failure really is acceptable, say so with a `fail-open-ok` comment or an entry in
> fail-open-allow.txt The Sentinel · 3: refuse the cheap rescue. What you do not control must
> fail loudly, so that the part you do control — the response — can happen at all; an error
> nobody sees is not an error anybody handled. Log it, re-raise it, count it, or return an
> explicit error value. If swallowing really is correct here, say so on the line with a
> `fail-open-ok` comment, or add a pattern to .conduct/fail-open-allow.txt. Warning mode: this
> change is NOT blocked.

Why a hook when the gate exists: the gate reads the added lines of a diff, in CI or before a
commit. By then the `except: pass` has been written, the test that depended on the error has
been run against it, and the green it produced has been reported — and the cheapest rescue of
all is typed straight into a shell (`npm test || true`) and never reaches a diff. This is the
same gate at the moment the line is written.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Edit|Write|MultiEdit",
        "hooks": [
          { "type": "command",
            "command": "python3 /abs/path/to/agnostic-harness/hooks/fail-open-before-run.py",
            "timeout": 10 }
        ]
      }
    ]
  }
}
```

What it hands the gate: a `Bash` command judged as shell, line by line. An `Edit` or
`MultiEdit` **simulated** — the file read from disk, `old_string` replaced by `new_string`
(`replace_all` honoured), the whole result parsed, and only the lines the edit wrote counted as
new. The whole file, because the Python checker parses a module and a fragment is not one: an
indented handler judged on its own is an `IndentationError`, not a finding. A `Write` the same
way, with the lines the old file did not hold counted as new — every line when the file is new.
Dispatch by suffix exactly as the gate does (an extensionless file with a shell shebang is
shell); a file the gate does not judge is `skipped`, which is not the same word as `ok`.

The gate is imported with the session's working directory as its root, so
`.conduct/fail-open-allow.txt` is the repository the agent is working in, not this one. A
`fail-open-ok` comment on the line is honoured as the gate honours it, and a broken allowlist is
a finding, as in the gate — never a silent exemption of nothing or of everything.

Every run leaves a receipt in `~/.local/state/agnostic-harness/fail-open-receipts.jsonl`
(`FAIL_OPEN_RECEIPTS=…` to move it, `off` to disable) — verdict, checks, counts, never a line of
the file or of the command. `FAIL_OPEN_HOOK_MODE=block` makes it deny the change instead (exit
2); shipped so the switch exists, not the default. Any error of its own is a receipt with
`verdict: error` and exit 0. Yes: the hook that hunts code which fails open, fails open. A hook
that denies an agent's edit because of its own bug is the hook that gets uninstalled, after
which it catches nothing — so the failure is counted, never silent, and `verdict: error` in the
receipts is the first thing to grep when the warning goes quiet.

One check the gate runs on a shell file is **not** run on a command typed into the Bash tool:
`swallowed-stderr`. Its premise is a script nobody is watching; in an interactive call the agent
reads the output and the status is the tool result, and `ls X 2>/dev/null` is the ordinary shape
of looking around. Measured over 27,549 real Bash calls it flagged 22.5 % of them — a hook that
fires on one call in four is uninstalled by the end of the day — so the hook leaves it out there
and keeps it on for every `.sh` the agent writes.

Other limits, inherited from the gate and stated in the file's header so they stay decisions:
four languages only — an Edit to a Go, Rust or Swift file is `skipped`, not cleared; a script fed
to an interpreter through a heredoc is judged as the shell lines it arrives in, not as the
language it is written in; a Python file that does not parse is `unparseable` on every edit, as
the gate reports it on every diff; and nothing about whether the error handling that *is* there
is any good.

Tests: [`tests/test_fail_open_hook.py`](../tests/test_fail_open_hook.py) ·
mutants: [`tests/mutation_check_fail_open_hook.py`](../tests/mutation_check_fail_open_hook.py) ·
once, the runtime's own payload shape: [`tests/live_hook_smoke.py`](../tests/live_hook_smoke.py).

## Just want to see it?

```sh
./hooks/session-start.sh
```
