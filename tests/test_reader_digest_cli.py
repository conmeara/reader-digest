import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "reader_digest.py"


class ReaderDigestCliTests(unittest.TestCase):
    def run_cli(self, tmp, *args, check=True):
        cmd = [
            sys.executable,
            str(CLI),
            "--workspace",
            str(tmp),
            "--db",
            str(tmp / "reader.sqlite"),
            "--json",
            *args,
        ]
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and proc.returncode != 0:
            self.fail(f"command failed: {' '.join(cmd)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        return proc

    def payload(self, proc):
        return json.loads(proc.stdout)

    def test_init_creates_plugin_database(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            result = self.payload(self.run_cli(tmp, "init"))
            self.assertEqual(result["status"], "ok")
            self.assertTrue((tmp / "reader.sqlite").exists())

    def test_queue_and_list_url(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self.run_cli(tmp, "init")
            queued = self.payload(
                self.run_cli(
                    tmp,
                    "queue",
                    "https://example.com/hello-world",
                    "--title",
                    "Hello World",
                    "--date",
                    "2026-05-26",
                )
            )
            self.assertEqual(queued["status"], "queued")
            listed = self.payload(self.run_cli(tmp, "queue", "list", "--date", "2026-05-26"))
            self.assertEqual(len(listed["items"]), 1)
            self.assertEqual(listed["items"][0]["title"], "Hello World")

    def test_prepare_build_and_qa_local_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            article = tmp / "article.md"
            article.write_text("# Local Article\n\nA readable local chapter.\n\n[Source](https://example.com/source)\n", encoding="utf-8")
            self.run_cli(tmp, "init")
            self.run_cli(
                tmp,
                "queue",
                "https://example.com/source",
                "--title",
                "Local Article",
                "--file",
                str(article),
                "--build-mode",
                "local",
                "--date",
                "2026-05-26",
            )
            prepared = self.payload(self.run_cli(tmp, "prepare", "2026-05-26"))
            self.assertEqual(prepared["status"], "prepared")
            built = self.payload(self.run_cli(tmp, "build", "2026-05-26"))
            self.assertEqual(built["status"], "built")
            epub = Path(built["epubPath"])
            self.assertTrue(epub.exists())
            qa = self.payload(self.run_cli(tmp, "qa", "2026-05-26"))
            self.assertEqual(qa["status"], "passed", qa)

    def test_send_requires_confirmation_and_dry_run_suppresses_delivery(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            article = tmp / "article.md"
            article.write_text("# Send Test\n\nBody.\n", encoding="utf-8")
            self.run_cli(tmp, "init")
            self.run_cli(tmp, "queue", "https://example.com/send", "--title", "Send Test", "--file", str(article), "--build-mode", "local", "--date", "2026-05-26")
            self.run_cli(tmp, "prepare", "2026-05-26")
            self.run_cli(tmp, "build", "2026-05-26")
            no_confirm = self.payload(self.run_cli(tmp, "send", "2026-05-26", "--to", "kindle@example.com"))
            self.assertEqual(no_confirm["status"], "not-sent")
            cmd = [
                sys.executable,
                str(CLI),
                "--workspace",
                str(tmp),
                "--db",
                str(tmp / "reader.sqlite"),
                "--json",
                "--dry-run",
                "send",
                "2026-05-26",
                "--to",
                "kindle@example.com",
                "--confirm-send",
            ]
            proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            dry = json.loads(proc.stdout)
            self.assertEqual(dry["status"], "dry-run")

    def test_manifest_only_queue_prepare_build(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            article = tmp / "article.md"
            article.write_text("# Manifest Only\n\nBody.\n", encoding="utf-8")
            base = [
                sys.executable,
                str(CLI),
                "--workspace",
                str(tmp),
                "--storage-mode",
                "manifest-only",
                "--json",
            ]
            subprocess.run(base + ["queue", "https://example.com/m", "--title", "Manifest Only", "--file", str(article), "--build-mode", "local", "--date", "2026-05-26"], check=True, text=True, stdout=subprocess.PIPE)
            prepared = subprocess.run(base + ["prepare", "2026-05-26"], check=True, text=True, stdout=subprocess.PIPE)
            self.assertEqual(json.loads(prepared.stdout)["status"], "prepared")
            built = subprocess.run(base + ["build", "2026-05-26"], check=True, text=True, stdout=subprocess.PIPE)
            self.assertTrue(Path(json.loads(built.stdout)["epubPath"]).exists())


if __name__ == "__main__":
    unittest.main()
