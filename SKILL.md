---
name: sparring
description: Let Claude Code and OpenAI Codex debate a technical question about the current project. Codex runs in a read-only sandbox, explores the whole project on its own, and never edits files; both sides' turns go into one Markdown log the user can watch live. Use when the user asks to "spar with Codex", "discuss this with Codex", "get Codex's opinion", "have Codex review or check this", "get a second opinion from Codex", or runs /sparring, in any language. Every round spends the user's ChatGPT/Codex quota, so start only on an explicit request and never decide on your own to consult Codex.
compatibility: Requires the Codex CLI (npm install -g @openai/codex, signed in with codex login), Node.js, and Python 3.11+ (uses the built-in tomllib). Tested on Windows 11 with codex-cli 0.154.0; macOS and Linux are untested.
user-invocable: true
argument-hint: "[topic] [— project: folder] [— rounds: max rounds] [— effort: reasoning effort] [— language: reply language]"
---

# Claude × Codex Sparring

One script, `scripts/sparring.py`, turns "Claude speaks → Codex replies" into one command per round.

Roles:

- **Claude leads**: decides what to ask, verifies Codex's claims, and decides when to stop.
- **Codex is read-only**: explores the project inside `--sandbox read-only`, answers, and never writes a file.
- **The script owns the log**: only the script writes the log, so the two sides can never collide.
- **The thread continues**: round 1 starts a thread with `codex exec`; later rounds use `codex exec resume <thread_id>`, so Codex remembers earlier rounds.

## Running the script

`<SKILL_DIR>` is the folder that contains this SKILL.md (usually `~/.claude/skills/sparring` for a user-level install, or `<project>/.claude/skills/sparring` for a project-level one).

```bash
PY=python3   # on Windows usually python or py; if python opens the Microsoft Store stub, use the interpreter's full path
SPAR="<SKILL_DIR>/scripts/sparring.py"

"$PY" "$SPAR" start  --log "<log.md>" --project "<project folder>" --topic "<one-line topic>" [--effort high] [--reply-language "<language>"]
"$PY" "$SPAR" ask    --log "<log.md>" --message-file "<this-round.md>"
"$PY" "$SPAR" note   --log "<log.md>" --message-file "<summary.md>" --title "Summary"
"$PY" "$SPAR" status --log "<log.md>"
```

`ask` also accepts `--dry-run` (print the command and the prompt without running anything) and `--timeout <seconds>`.

## Workflow

### 0. Settle the parameters

Ask the user only about what is unclear; otherwise use these defaults:

| Parameter | Default |
| --- | --- |
| Topic | A one-line summary of the user's request |
| Project folder | The current working directory |
| Max rounds | 10. This is a ceiling, not a target: stop as soon as both sides agree, or when the remaining points are minor or need the user's call |
| Reasoning effort | Unset: inherited from `~/.codex/config.toml` (`start` prints the effective value). With `xhigh`, one round can take several minutes; `--effort high` suits most discussions |
| Reply language | The language the user is speaking with you, e.g. `--reply-language "Traditional Chinese"`; English when unclear |
| Log file | `<project>/sparring/YYYY-MM-DD-<kebab-slug>.md`. If the project's CLAUDE.md prescribes an output location, follow it; ask the user when unsure |

### 1. Safety check (first use in a project)

Codex sends whatever it reads to OpenAI. Glob the project for `**/.env*`, `**/*.pem`, `**/*.key`, `**/*credential*`, and `**/*secret*`.
If anything turns up, show the list to the user and wait for confirmation before continuing.

### 2. Create the log

Run `start`. The default is **isolated mode** (`--ignore-user-config`):

- **Not loaded:** MCP servers, plugins, and the notify program from the Codex config. The sandbox restricts file writes and network access, but not MCP tools or plugins (browser control, for example), and a read-only sparring partner should not have them.
- **Still applied:** the model, reasoning effort, and Windows sandbox mode from the config. The script reads them and passes them explicitly.

Add `--use-user-config` only when the user explicitly wants Codex to have those tools.

### 3. Write this round's message

Read the relevant code yourself first, then use the **Write tool** to save your message to a scratch file (for example `<scratchpad>/sparring/turn-1.md`).
Do not write it with a heredoc (`cat > ... <<EOF`): long content can be truncated in transit, and shells can mangle quotes and backslashes.

A good message contains:

- **Background**: the problem and its constraints.
- **The current idea or approach**, with `path:line` references.
- **Specific points for Codex to check** (for example "is this lock safe under concurrent access?"), not just "what do you think?".
- **From round 2 on**: a point-by-point response to Codex's previous reply: what you accept, what you dispute, and your evidence.

### 4. Send and wait

**Always run `ask` in the background** (Bash tool with `run_in_background: true`). A round can take minutes, and a foreground command can wait at most 10 minutes.
You are notified when it finishes, so **do not poll and do not sleep**. While waiting, you can tell the user what is being discussed.

Output: stdout prints Codex's reply and the parsed STATUS.
Exit codes: `0` success, `1` invalid arguments or lock conflict, `2` Codex failed this round (details on stderr).

### 5. Read the reply and decide

**Verify first**: open the `path:line` references Codex cites and confirm they say what Codex claims.
Do not accept everything at face value, and do not agree just to end the discussion.

| STATUS | Next step |
| --- | --- |
| `CONTINUE` | Write the next message (back to step 3) |
| `AGREE` | If you agree too, wrap up; if not, write another round explaining why |
| `NEED_USER` | Stop, ask the user with AskUserQuestion, and include the answer in the next round |
| `UNKNOWN` (no STATUS line) | Treat it as `CONTINUE` and remind Codex to end with a STATUS line |
| `FAILED` | Read stderr. Retry once for a transient error; if the same error happens twice, stop and tell the user |

Stop at the round limit even without consensus.

### 6. Wrap up

1. Use Write to draft a summary (consensus, open disagreements, recommended next steps) and append it with `note --title "Summary"`.
2. Report to the user: the summary, remaining disagreements, the number of rounds, and the full path of the log.
3. If the conclusion calls for code changes, Claude makes them after the user agrees. Codex never edits files in this workflow.

## Generated files

| Path | Contents |
| --- | --- |
| `<log>.md` | The discussion log; the user can open it live in any editor (VS Code, Obsidian, ...) |
| `<log folder>/.sparring/<name>.state.json` | Round count, Codex thread id, settings |
| `<log folder>/.sparring/<name>.lock` | Exists only while a round is running |
| `<log folder>/.sparring/<name>.round-<N>.jsonl` | Raw Codex events for round N, including the commands it ran (for debugging) |
| `<log folder>/.sparring/<name>.round-<N>.stderr.txt` | Codex's stderr for round N (for debugging) |

## Pitfalls

- **How Codex is located**: on Windows the script first looks for `%APPDATA%\npm\node_modules\@openai\codex\bin\codex.js` and runs it with node, so it works even when the npm global folder is not on PATH. Do not switch to `codex.cmd`: cmd.exe rewrites quotes inside arguments. Elsewhere it uses `codex` from PATH. The `CODEX_BIN` environment variable overrides both.
- **`codex exec resume` rejects `--sandbox` and `--cd`**: the script passes `-c sandbox_mode="read-only"` and sets the child's working directory instead. Keep this in mind when running commands by hand.
- **Never add `--ephemeral`**: without saved sessions, the thread cannot be resumed.
- **Never switch the sandbox to `workspace-write` or `danger-full-access`.** If the user wants Codex to edit files, that is outside this skill; say so.
- **Stale lock file**: an interrupted round can leave a `.lock` behind. Confirm with `status` and the process list that no Codex process is running before deleting it.
- **Do not hand-edit the log during a discussion**: the script locates the reply through a placeholder; if the placeholder is changed, the reply is appended to the end of the file instead.
- **Paths**: on Windows both `C:/...` and Git Bash style `/c/...` work; the script converts them.
- **Non-ASCII text in the Windows sandbox**: PowerShell runs in Constrained Language Mode and cannot change its output encoding, so `Get-Content` garbles CJK text (with or without `-Encoding UTF8`). The round-1 prompt tells Codex to use `cmd /c type <file>` instead, which prints UTF-8 correctly (verified).
- **Reasoning effort values**: `minimal`, `low`, `medium`, `high`, `xhigh` (`xhigh` depends on the model).

## Maintenance

- Update the Codex CLI with `npm install -g @openai/codex@latest`. Afterwards, run `ask --dry-run` once to check that the command still builds, then run one real round.
- The flags follow `codex exec --help` from codex-cli 0.154.0. If `ask` breaks after an upgrade, compare against `codex exec --help` (on Windows, when `codex` is not on PATH: `node "%APPDATA%/npm/node_modules/@openai/codex/bin/codex.js" exec --help`).
- Verify that the sandbox blocks writes (Windows, no quota used): in Git Bash, first `export MSYS_NO_PATHCONV=1` (otherwise the `/c` in `cmd /c` becomes `C:/`), then run `codex sandbox -P :read-only -C "<project>" -- cmd /c "echo x> t.txt"`. Expected: exit code 1, an "Access is denied" message (possibly garbled on non-English consoles), and no `t.txt`. With `-P :workspace` the file is created, which serves as a control.
- Offline tests: `python tests/test_offline.py`. They use `tests/fake_codex.py` to simulate the event stream of `codex exec --json`; no sign-in or quota is needed.
