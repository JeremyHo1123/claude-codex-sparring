"""A fake `codex` that mimics the event stream of `codex exec --json`, for offline tests of sparring.py.

FAKE_MODE environment variable:
  ok (default)  normal reply ending with "STATUS: CONTINUE"
  agree         normal reply ending with "STATUS: AGREE"
  fullwidth     normal reply ending with "STATUS" + a full-width colon (U+FF1A) + " AGREE"
  fail          emits turn.failed
  crash         emits no events and exits with code 3
"""
import json
import os
import sys
import uuid

sys.stdout.reconfigure(encoding="utf-8")
prompt = sys.stdin.buffer.read().decode("utf-8")
argv = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "ok")
sys.stderr.write("FAKE ARGV: " + json.dumps(argv) + "\n")
sys.stderr.write("FAKE CWD: " + os.getcwd() + "\n")

if mode == "crash":
    sys.stderr.write("simulated crash\n")
    sys.exit(3)

thread_id = argv[argv.index("resume") + 1] if "resume" in argv else str(uuid.uuid4())


def emit(obj):
    print(json.dumps(obj), flush=True)


emit({"type": "thread.started", "thread_id": thread_id})
emit({"type": "turn.started"})
if mode == "fail":
    emit({"type": "turn.failed", "error": {"message": "simulated model error"}})
    sys.exit(1)

status_line = {
    "agree": "STATUS: AGREE",
    "fullwidth": "STATUS" + chr(0xFF1A) + " AGREE",
}.get(mode, "STATUS: CONTINUE")
first_line = next((line for line in prompt.splitlines() if line.strip()), "")
text = (
    f"Received. resume={'resume' in argv}, thread={thread_id}\n"
    f"First prompt line: {first_line[:60]}\n"
    f"Prompt length: {len(prompt)} characters\n\n"
    f"{status_line}"
)
emit({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": text}})
emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
