#!/usr/bin/env python3
"""sparring: let Claude Code and OpenAI Codex take turns discussing a question in one Markdown log.

- Only this script writes the log. Codex always runs in a read-only sandbox: it reads, it never writes.
- Round 1 starts a new thread with `codex exec`; later rounds continue it with
  `codex exec resume <thread_id>`, so Codex remembers earlier rounds.
- State (thread id, round count, project path) lives next to the log in a `.sparring/` folder.

Subcommands:
  start   create the log and its state file
  ask     send Claude's message for one round, wait for Codex, and append both to the log
  note    append a section to the log (e.g. a summary) without calling Codex
  status  show progress
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

STATE_VERSION = 1
SIDE_DIR = ".sparring"
# Also accept a full-width colon, which models tend to emit when replying in CJK languages
STATUS_RE = re.compile(r"STATUS\s*[:\uff1a]\s*[`*_]*\s*(AGREE|CONTINUE|NEED_USER)\b", re.IGNORECASE)

FIRST_PROMPT = """You are in a technical discussion with another AI assistant, Claude (Claude Code). The user will read the full discussion log.

## Your role and permissions
- You are an independent second opinion. Do not agree just to be agreeable; if you see a problem, say so directly.
- You run in a read-only sandbox: you may read and search project files and run commands that change nothing. Do not try to create, modify, or delete files.{platform_hint}
- Project folder (your working directory): {project}
- Full discussion log: {log} (this round's message is included below; read the log only if you need earlier context)

## Reply rules
1. Back every claim with evidence. When citing code or docs, give `path:line`. Never pretend to have read a file you did not open.
2. List what you agree with and what you disagree with separately. When you disagree, explain why and propose an alternative.
3. If you are unsure, say so and explain how it could be verified.
4. Reply in {reply_language}. Keep technical terms, code, and paths in their original form.
5. End your reply with exactly one of the following, alone on the final line:
   STATUS: AGREE (you believe consensus has been reached and no further discussion is needed)
   STATUS: CONTINUE (there are still points to discuss)
   STATUS: NEED_USER (the user must provide information or make a decision)

## Topic
{topic}

## Claude, round {round}
{message}
"""

FOLLOWUP_PROMPT = """Here is Claude's message for round {round} (the full log is still at {log}). Follow the reply rules from the start of this conversation, and end with exactly one of STATUS: AGREE, STATUS: CONTINUE, or STATUS: NEED_USER alone on the final line.

{message}
"""

# Inside the Windows sandbox, PowerShell runs in Constrained Language Mode and cannot change its output
# encoding, so Get-Content garbles non-ASCII text. cmd's `type` copies file bytes unchanged, which keeps
# UTF-8 intact (verified with `codex sandbox` on Windows 11).
WINDOWS_HINT = ("\n- This machine runs Windows: PowerShell inside the sandbox cannot change its output encoding, "
                "so Get-Content garbles non-ASCII text (for example CJK). To read such files, use "
                "`cmd /c type <file>`, which prints UTF-8 bytes unchanged.")


# ─── helpers ────────────────────────────────────────────────────────────────

def now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def die(message: str, code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def one_line(text: Optional[str], limit: int = 300) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "..."


def norm_path(raw: str) -> Path:
    """Accept Git Bash style /c/Users/... paths and turn them into C:/Users/... on Windows."""
    match = re.match(r"^/([a-zA-Z])(/.*)?$", raw)
    if os.name == "nt" and match:
        raw = f"{match.group(1).upper()}:{match.group(2) or '/'}"
    return Path(raw).expanduser().resolve()


def side_paths(log: Path, round_no: Optional[int] = None) -> Dict[str, Path]:
    side = log.parent / SIDE_DIR
    paths = {
        "dir": side,
        "state": side / f"{log.stem}.state.json",
        "lock": side / f"{log.stem}.lock",
    }
    if round_no is not None:
        # Keep raw events and stderr for every round so earlier rounds can still be debugged
        paths["events"] = side / f"{log.stem}.round-{round_no}.jsonl"
        paths["stderr"] = side / f"{log.stem}.round-{round_no}.stderr.txt"
    return paths


def read_message(raw: str) -> str:
    path = norm_path(raw)
    if not path.is_file():
        die(f"message file not found: {path}")
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        die(f"message file is empty: {path}")
    return text


# ─── state file and log ─────────────────────────────────────────────────────

def load_state(log: Path) -> Dict[str, Any]:
    path = side_paths(log)["state"]
    if not path.exists():
        die(f"state file not found: {path}. Run `start` first to create this discussion.")
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(log: Path, state: Dict[str, Any]) -> None:
    path = side_paths(log)["state"]
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated"] = now().isoformat()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_log(log: Path, text: str) -> None:
    with log.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def replace_in_log(log: Path, placeholder: str, replacement: str) -> None:
    content = log.read_text(encoding="utf-8-sig")
    if content.count(placeholder) == 1:
        with log.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(content.replace(placeholder, replacement))
    else:
        # The placeholder was edited: do not guess a position, append to the end instead
        append_log(log, "\n" + replacement + "\n")


def acquire_lock(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        info = path.read_text(encoding="utf-8", errors="replace").strip()
        die(f"a round is already running for this discussion (lock file {path}: {info}). "
            "If you are sure Codex is not running (for example, the last round was interrupted), "
            "delete the lock file and try again.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"pid": os.getpid(), "started": now().isoformat()}))


# ─── calling Codex ──────────────────────────────────────────────────────────

def require_node() -> str:
    node = shutil.which("node")
    if not node:
        die("node not found. The Codex CLI needs Node.js.")
    return node


def resolve_codex(override: Optional[str]) -> List[str]:
    candidate = override or os.environ.get("CODEX_BIN")
    if candidate:
        path = norm_path(candidate)
        if path.suffix == ".js":
            return [require_node(), str(path)]
        if path.suffix == ".py":
            return [sys.executable, str(path)]
        return [str(path)]
    appdata = os.environ.get("APPDATA")
    if appdata:
        js = Path(appdata) / "npm" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if js.exists():
            # Run codex.js with node directly, bypassing codex.cmd: cmd.exe rewrites quotes inside arguments
            return [require_node(), str(js)]
    found = shutil.which("codex")
    if found:
        return [found]
    die("Codex CLI not found. Install it with `npm install -g @openai/codex` and sign in with `codex login`.")
    return []


def user_config_defaults() -> Dict[str, Optional[str]]:
    """Read the three values that isolated mode still carries over from ~/.codex/config.toml."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        import tomllib
        with (home / "config.toml").open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError, ImportError):
        return {}
    effort = data.get("model_reasoning_effort")
    sandbox = (data.get("windows") or {}).get("sandbox")
    return {
        "model": data.get("model") if isinstance(data.get("model"), str) else None,
        "effort": effort if isinstance(effort, str) and re.fullmatch(r"[A-Za-z_-]+", effort) else None,
        "windows_sandbox": sandbox if sandbox in ("elevated", "unelevated") else None,
    }


def effective_settings(state: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Model and reasoning effort actually passed to Codex: values given to `start` win, then the user config."""
    defaults = user_config_defaults()
    return {
        "model": state.get("model") or defaults.get("model"),
        "effort": state.get("effort") or defaults.get("effort"),
        "windows_sandbox": defaults.get("windows_sandbox"),
    }


def build_argv(codex: List[str], state: Dict[str, Any]) -> List[str]:
    argv = list(codex) + ["exec"]
    if state.get("thread_id"):
        # `resume` has no --sandbox / --cd: pass the sandbox with -c and the working directory as the child's cwd
        argv += ["resume", state["thread_id"], "--json", "--skip-git-repo-check",
                 "-c", 'sandbox_mode="read-only"']
    else:
        argv += ["--json", "--skip-git-repo-check", "--sandbox", "read-only", "--cd", state["project"]]

    if state.get("use_user_config"):
        model, effort = state.get("model"), state.get("effort")
    else:
        # Isolated mode: skip config.toml so MCP servers, plugins, and the notify program never start.
        # The sandbox governs files and network, not MCP tools, so a read-only sparring partner gets none of them.
        settings = effective_settings(state)
        model, effort = settings["model"], settings["effort"]
        argv.append("--ignore-user-config")
        if os.name == "nt" and settings["windows_sandbox"]:
            argv += ["-c", f'windows.sandbox="{settings["windows_sandbox"]}"']

    if model:
        argv += ["-m", model]
    if effort:
        argv += ["-c", f'model_reasoning_effort="{effort}"']
    argv.append("-")  # The prompt goes through stdin: no length limit and no command-line quoting
    return argv


class Outcome:
    def __init__(self) -> None:
        self.thread_id: Optional[str] = None
        self.message: Optional[str] = None
        self.error: Optional[str] = None
        self.completed = False
        self.failed = False
        self.timed_out = False


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        # node spawns codex.exe; killing only node would orphan it, so /T ends the whole tree by PID
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.kill()


def run_codex(argv: List[str], prompt: str, cwd: str, paths: Dict[str, Path], timeout: float) -> Outcome:
    out = Outcome()
    paths["dir"].mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=cwd)
    except OSError as exc:
        out.error = f"could not start Codex: {exc}"
        return out

    stderr_chunks: List[bytes] = []

    def feed_stdin() -> None:
        try:
            proc.stdin.write(prompt.encode("utf-8"))
        except OSError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def drain_stderr() -> None:
        for chunk in iter(proc.stderr.readline, b""):
            stderr_chunks.append(chunk)

    threading.Thread(target=feed_stdin, daemon=True).start()
    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    timer: Optional[threading.Timer] = None
    if timeout > 0:
        def expire() -> None:
            out.timed_out = True
            kill_tree(proc)
        timer = threading.Timer(timeout, expire)
        timer.start()

    try:
        with paths["events"].open("w", encoding="utf-8", newline="\n") as events:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                events.write(line + "\n")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = event.get("type", "")
                if etype == "thread.started":
                    out.thread_id = event.get("thread_id") or out.thread_id
                elif etype == "item.completed":
                    item = event.get("item") or {}
                    if item.get("type") == "agent_message":
                        out.message = item.get("text", "")
                elif etype == "turn.completed":
                    out.completed = True
                elif etype == "turn.failed":
                    out.failed = True
                    out.error = (event.get("error") or {}).get("message") or out.error or "turn failed"
                elif etype == "error" and not out.failed:
                    # Remember it; if the turn still completes, it was not a failure
                    out.error = event.get("message") or out.error
    except BaseException:
        kill_tree(proc)
        raise
    finally:
        proc.wait()
        if timer:
            timer.cancel()
        stderr_thread.join(timeout=5)

    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    paths["stderr"].write_text(stderr_text, encoding="utf-8")

    if out.timed_out:
        out.error = f"did not finish within {int(timeout)} s; aborted"
    elif out.failed:
        pass
    elif out.completed and out.message:
        out.error = None
    elif out.completed:
        out.error = "Codex completed the turn but produced no text reply"
    elif not out.error:
        tail = stderr_text.strip()[-1500:]
        out.error = f"codex exec exited (code {proc.returncode}) without completing the turn" + (f"\n{tail}" if tail else "")
    return out


def parse_status(message: str) -> str:
    lines = [line for line in message.strip().splitlines() if line.strip()]
    for line in reversed(lines[-5:]):
        match = STATUS_RE.search(line)
        if match:
            return match.group(1).upper()
    return "UNKNOWN"


# ─── subcommands ────────────────────────────────────────────────────────────

def cmd_start(args: argparse.Namespace) -> None:
    log = norm_path(args.log)
    project = norm_path(args.project)
    if not project.is_dir():
        die(f"project folder does not exist: {project}")
    if log.exists():
        die(f"log already exists: {log}. To continue that discussion use `ask`; to start a new one choose another file name.")
    if args.effort and not re.fullmatch(r"[A-Za-z_-]+", args.effort):
        die(f"--effort must contain letters only (for example medium, high, xhigh), got: {args.effort}")
    language = " ".join(args.reply_language.split())
    if not language:
        die("--reply-language must not be empty")

    log.parent.mkdir(parents=True, exist_ok=True)
    created = now()
    state = {
        "version": STATE_VERSION,
        "topic": args.topic,
        "project": str(project),
        "log": str(log),
        "model": args.model,
        "effort": args.effort,
        "reply_language": language,
        "use_user_config": args.use_user_config,
        "thread_id": None,
        "round": 0,
        "created": created.isoformat(),
        "rounds": [],
    }
    # In both modes the effective model is "given to start, else from the user config"; only MCP and plugins differ
    settings = effective_settings(state)
    if args.use_user_config:
        config_label = "User config (`~/.codex/config.toml`, including MCP servers and plugins)"
    else:
        config_label = "Isolated (`--ignore-user-config`): no MCP servers, plugins, or notify program"
    model_label = f"`{settings['model']}`" if settings["model"] else "Codex built-in default"
    effort_label = f"`{settings['effort']}`" if settings["effort"] else "Codex built-in default"
    header = (
        f"# Claude × Codex Sparring: {args.topic}\n\n"
        "| Item | Value |\n"
        "|---|---|\n"
        f"| Project folder | `{project}` |\n"
        f"| Started | {created:%Y-%m-%d %H:%M} |\n"
        "| Codex permissions | Read-only (`--sandbox read-only`): reads the project, never edits files |\n"
        f"| Codex config | {config_label} |\n"
        f"| Codex model | {model_label} |\n"
        f"| Reasoning effort | {effort_label} |\n"
        f"| Reply language | {language.replace('|', '/')} |\n\n"
        "> This log is written by `sparring.py`. Please do not edit it while the discussion is running.\n"
    )
    with log.open("x", encoding="utf-8", newline="\n") as fh:
        fh.write(header)

    save_state(log, state)
    print(f"Created log: {log}")
    print(f"Codex model: {settings['model'] or 'built-in default'}, "
          f"reasoning effort: {settings['effort'] or 'built-in default'}, reply language: {language}")
    print(f"State file: {side_paths(log)['state']}")


def cmd_ask(args: argparse.Namespace) -> None:
    log = norm_path(args.log)
    state = load_state(log)
    message = read_message(args.message_file)
    round_no = int(state.get("round", 0)) + 1
    paths = side_paths(log, round_no)
    template = FOLLOWUP_PROMPT if state.get("thread_id") else FIRST_PROMPT
    prompt = template.format(project=state["project"], log=log, topic=state["topic"],
                             round=round_no, message=message,
                             reply_language=state.get("reply_language") or "English",
                             platform_hint=WINDOWS_HINT if os.name == "nt" else "")
    argv = build_argv(resolve_codex(args.codex_bin), state)

    if args.dry_run:
        print("(dry run: Codex will not be started and the log will not be written)")
        print(f"Working directory: {state['project']}")
        print(f"Command: {subprocess.list2cmdline(argv)}")
        print("----- Prompt sent to Codex -----")
        print(prompt)
        return

    acquire_lock(paths["lock"])
    started = now()
    placeholder = f"_(Codex is replying to round {round_no}... started at {started:%H:%M:%S})_"
    appended = finished = False
    try:
        append_log(log, f"\n---\n\n## Round {round_no} · Claude\n\n{message}\n\n"
                        f"## Round {round_no} · Codex\n\n{placeholder}\n")
        appended = True
        t0 = time.monotonic()
        outcome = run_codex(argv, prompt, state["project"], paths, args.timeout)
        seconds = int(time.monotonic() - t0)

        if outcome.error is None:
            status = parse_status(outcome.message or "")
            replace_in_log(log, placeholder, f"{(outcome.message or '').strip()}\n\n_(took {seconds} s)_")
        else:
            status = "FAILED"
            replace_in_log(log, placeholder,
                           f"_(Codex did not reply successfully this round, after {seconds} s: {one_line(outcome.error)})_")

        if outcome.thread_id and not state.get("thread_id"):
            state["thread_id"] = outcome.thread_id
        state["round"] = round_no
        state["rounds"].append({"round": round_no, "status": status, "seconds": seconds,
                                "started": started.isoformat(), "error": outcome.error})
        save_state(log, state)
        finished = True
    finally:
        if appended and not finished:
            replace_in_log(log, placeholder, f"_(Round {round_no} was interrupted before Codex replied)_")
        paths["lock"].unlink(missing_ok=True)

    if outcome.error is None:
        print(f"=== Codex, round {round_no} (took {seconds} s, STATUS: {status}) ===")
        print((outcome.message or "").strip())
        print(f"=== Written to log: {log} ===")
    else:
        print(f"=== Codex round {round_no} failed (after {seconds} s) ===", file=sys.stderr)
        print(outcome.error, file=sys.stderr)
        print(f"Raw events: {paths['events']}", file=sys.stderr)
        print(f"stderr: {paths['stderr']}", file=sys.stderr)
        sys.exit(2)


def cmd_note(args: argparse.Namespace) -> None:
    log = norm_path(args.log)
    state = load_state(log)  # Also confirms that this log was created by this tool
    message = read_message(args.message_file)
    paths = side_paths(log)
    acquire_lock(paths["lock"])
    try:
        append_log(log, f"\n---\n\n## {args.title}\n\n{message}\n")
        save_state(log, state)
    finally:
        paths["lock"].unlink(missing_ok=True)
    print(f'Appended "{args.title}" to {log}')


def cmd_status(args: argparse.Namespace) -> None:
    log = norm_path(args.log)
    state = load_state(log)
    lock = side_paths(log)["lock"]
    print(f"Topic: {state['topic']}")
    print(f"Project: {state['project']}")
    print(f"Log: {state['log']}")
    print(f"Codex config: {'user config (with MCP servers and plugins)' if state.get('use_user_config') else 'isolated (config.toml not loaded)'}")
    print(f"Reply language: {state.get('reply_language') or 'English'}")
    print(f"Rounds completed: {state['round']}")
    print(f"Codex thread: {state.get('thread_id') or '(not created yet)'}")
    print(f"Running: {'yes (' + str(lock) + ')' if lock.exists() else 'no'}")
    for item in state.get("rounds", []):
        extra = f", error: {one_line(item['error'])}" if item.get("error") else ""
        print(f"  Round {item['round']}: {item['status']}, {item['seconds']} s{extra}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Let Claude Code and OpenAI Codex take turns discussing a question in one Markdown log")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="create the log and its state file")
    p_start.add_argument("--log", required=True, help="path of the Markdown log (must not exist yet)")
    p_start.add_argument("--project", required=True, help="project folder that Codex may read")
    p_start.add_argument("--topic", required=True, help="one-line topic")
    p_start.add_argument("--model", help="Codex model; defaults to your Codex config")
    p_start.add_argument("--effort", help="reasoning effort, e.g. medium, high, xhigh; defaults to your Codex config")
    p_start.add_argument("--reply-language", default="English",
                         help="language Codex should reply in (default: English)")
    p_start.add_argument("--use-user-config", action="store_true",
                         help="load ~/.codex/config.toml including MCP servers and plugins (off by default)")
    p_start.set_defaults(func=cmd_start)

    p_ask = sub.add_parser("ask", help="send one round and wait for Codex's reply")
    p_ask.add_argument("--log", required=True)
    p_ask.add_argument("--message-file", required=True, help="file with Claude's message for this round (UTF-8)")
    p_ask.add_argument("--timeout", type=float, default=0, help="time limit in seconds; 0 means no limit")
    p_ask.add_argument("--dry-run", action="store_true", help="print the command and prompt without running Codex")
    p_ask.add_argument("--codex-bin", help="use a specific Codex executable (.js runs with node, .py with python); mainly for tests")
    p_ask.set_defaults(func=cmd_ask)

    p_note = sub.add_parser("note", help="append to the log without calling Codex")
    p_note.add_argument("--log", required=True)
    p_note.add_argument("--message-file", required=True)
    p_note.add_argument("--title", default="Summary")
    p_note.set_defaults(func=cmd_note)

    p_status = sub.add_parser("status", help="show progress")
    p_status.add_argument("--log", required=True)
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
