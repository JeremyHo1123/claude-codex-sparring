"""Offline tests: fake_codex.py simulates `codex exec --json`, so no sign-in or quota is needed.

Run: python tests/test_offline.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "sparring.py"
FAKE = HERE / "fake_codex.py"


class OfflineFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.project = base / "project"
        self.project.mkdir()
        (self.project / "notes.txt").write_text("hello\n", encoding="utf-8")
        self.log = base / "logs" / "talk.md"
        self.side = self.log.parent / ".sparring"
        self.turn = base / "turn.md"
        self.turn.write_text("Test message: \"double\" 'single' {braces} 100% & | < > ^ naïve café\n",
                             encoding="utf-8")
        # Point CODEX_HOME at an empty folder so a local ~/.codex/config.toml cannot affect the tests
        self.codex_home = base / "codex-home"
        self.codex_home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, fake_mode="ok", check=True):
        env = dict(os.environ, FAKE_MODE=fake_mode, CODEX_HOME=str(self.codex_home))
        env.pop("CODEX_BIN", None)
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                              text=True, encoding="utf-8", env=env)
        if check and proc.returncode != 0:
            self.fail(f"exit {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        return proc

    def start(self, *extra, check=True):
        return self.run_cli("start", "--log", str(self.log), "--project", str(self.project),
                            "--topic", "Test topic", *extra, check=check)

    def ask(self, *extra, fake_mode="ok", check=True):
        return self.run_cli("ask", "--log", str(self.log), "--message-file", str(self.turn),
                            "--codex-bin", str(FAKE), *extra, fake_mode=fake_mode, check=check)

    def state(self):
        return json.loads((self.side / "talk.state.json").read_text(encoding="utf-8"))

    def fake_argv(self, round_no):
        lines = (self.side / f"talk.round-{round_no}.stderr.txt").read_text(encoding="utf-8").splitlines()
        return json.loads(lines[0].split("FAKE ARGV: ", 1)[1])

    def test_new_thread_then_resume(self):
        self.start("--effort", "high")

        out = self.ask()
        self.assertIn("STATUS: CONTINUE", out.stdout)
        argv = self.fake_argv(1)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("--ignore-user-config", argv)
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertEqual(argv[-1], "-")
        thread_id = self.state()["thread_id"]
        self.assertTrue(thread_id)

        out = self.ask(fake_mode="agree")
        self.assertIn("STATUS: AGREE", out.stdout)
        argv = self.fake_argv(2)
        self.assertEqual(argv[:3], ["exec", "resume", thread_id])
        self.assertIn('sandbox_mode="read-only"', argv)
        self.assertNotIn("--sandbox", argv)
        self.assertNotIn("--cd", argv)

        text = self.log.read_text(encoding="utf-8")
        for heading in ("## Round 1 · Claude", "## Round 1 · Codex", "## Round 2 · Claude", "## Round 2 · Codex"):
            self.assertIn(heading, text)
        self.assertNotIn("is replying to round", text)
        self.assertEqual([r["status"] for r in self.state()["rounds"]], ["CONTINUE", "AGREE"])

    def test_failures_are_recorded_and_unlock(self):
        self.start()
        self.assertEqual(self.ask(fake_mode="fail", check=False).returncode, 2)
        self.assertEqual(self.ask(fake_mode="crash", check=False).returncode, 2)
        self.assertEqual([r["status"] for r in self.state()["rounds"]], ["FAILED", "FAILED"])
        self.assertIn("did not reply successfully", self.log.read_text(encoding="utf-8"))
        self.assertFalse((self.side / "talk.lock").exists())

    def test_lock_blocks_concurrent_round(self):
        self.start()
        (self.side / "talk.lock").write_text('{"pid": 1}', encoding="utf-8")
        proc = self.ask(check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("lock file", proc.stderr)

    def test_start_validation(self):
        self.start()
        self.assertEqual(self.start(check=False).returncode, 1)  # log already exists
        other = str(self.log.with_name("other.md"))
        bad_effort = self.run_cli("start", "--log", other, "--project", str(self.project),
                                  "--topic", "x", "--effort", 'high"; rm', check=False)
        self.assertEqual(bad_effort.returncode, 1)
        empty_language = self.run_cli("start", "--log", other, "--project", str(self.project),
                                      "--topic", "x", "--reply-language", "   ", check=False)
        self.assertEqual(empty_language.returncode, 1)

    def test_dry_run_leaves_log_untouched(self):
        self.start()
        before = self.log.read_text(encoding="utf-8")
        out = self.ask("--dry-run")
        self.assertIn("read-only sandbox", out.stdout)
        self.assertIn("Reply in English.", out.stdout)
        if os.name == "nt":
            self.assertIn("cmd /c type", out.stdout)
        self.assertEqual(self.log.read_text(encoding="utf-8"), before)
        self.assertEqual(self.state()["round"], 0)

    def test_reply_language_reaches_prompt(self):
        self.start("--reply-language", "Traditional Chinese")
        self.assertIn("Reply in Traditional Chinese.", self.ask("--dry-run").stdout)
        self.assertIn("| Reply language | Traditional Chinese |", self.log.read_text(encoding="utf-8"))

    def test_fullwidth_status_colon_is_parsed(self):
        self.start()
        out = self.ask(fake_mode="fullwidth")
        self.assertIn("STATUS: AGREE", out.stdout)
        self.assertEqual(self.state()["rounds"][0]["status"], "AGREE")

    def test_isolated_mode_forwards_user_config_values(self):
        (self.codex_home / "config.toml").write_text(
            'model = "example-model"\nmodel_reasoning_effort = "medium"\n\n[windows]\nsandbox = "elevated"\n',
            encoding="utf-8")
        self.start()
        self.ask()
        argv = self.fake_argv(1)
        self.assertEqual(argv[argv.index("-m") + 1], "example-model")
        self.assertIn('model_reasoning_effort="medium"', argv)
        if os.name == "nt":
            self.assertIn('windows.sandbox="elevated"', argv)

    def test_use_user_config_skips_isolation(self):
        self.start("--use-user-config")
        self.ask()
        self.assertNotIn("--ignore-user-config", self.fake_argv(1))

    def test_note_and_status(self):
        self.start()
        self.ask()
        self.run_cli("note", "--log", str(self.log), "--message-file", str(self.turn), "--title", "Summary")
        self.assertIn("## Summary", self.log.read_text(encoding="utf-8"))
        self.assertIn("Rounds completed: 1", self.run_cli("status", "--log", str(self.log)).stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
