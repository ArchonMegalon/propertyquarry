# `codexpure`

`codexpure` launches Codex with a private `CODEX_HOME`. It is intended for a
second terminal or an isolated automation run when another Codex process is
already using the canonical `~/.codex` SQLite databases.

The shim copies only current bootstrap files (`auth.json`, `config.toml`, the
installation identifier, and bounded caches). It does not copy `state_5.sqlite`,
`logs_2.sqlite`, transcripts, or session state. Skills, plugins, prompts, and
rules are linked from the canonical home. Vexp and repository instructions are
disabled for the pure process.

Install the checked-in shim:

```bash
install -m 0755 scripts/codexpure "$HOME/.local/bin/codexpure"
```

Then run:

```bash
codexpure
```

Interactive terminals receive one private home per TTY. Non-interactive
launches receive process-unique homes. An explicit stable instance can be used
when desired:

```bash
CODEX_PURE_INSTANCE=review-a codexpure
```

Only one process may use a given explicit or TTY-backed instance. A collision
exits with status 75 before Codex opens SQLite. The shim never kills another
Codex process and never deletes or repairs either process's database.

`CODEX_PURE_RUNTIME_WAIT_SECONDS` can wait for a busy explicit instance instead
of failing immediately. Its value must be a non-negative integer.
