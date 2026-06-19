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
            self.assertEqual(prepared["filename"], "Local Article - May 26 2026.epub")
            built = self.payload(self.run_cli(tmp, "build", "2026-05-26"))
            self.assertEqual(built["status"], "built")
            epub = Path(built["epubPath"])
            self.assertTrue(epub.exists())
            self.assertEqual(epub.name, "Local Article - May 26 2026.epub")
            qa = self.payload(self.run_cli(tmp, "qa", "2026-05-26"))
            self.assertEqual(qa["status"], "passed", qa)
            self.assertEqual(Path(qa["epubPath"]).name, "Local Article - May 26 2026.epub")

    def test_build_rewrites_stale_date_based_manifest_filename(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            article = tmp / "article.md"
            article.write_text("# Stale Filename\n\nBody.\n", encoding="utf-8")
            self.run_cli(tmp, "init")
            self.run_cli(tmp, "queue", "https://example.com/stale", "--title", "Stale Filename", "--file", str(article), "--build-mode", "local", "--date", "2026-05-26")
            prepared = self.payload(self.run_cli(tmp, "prepare", "2026-05-26"))
            manifest_path = Path(prepared["manifestPath"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["filename"] = "2026-05-26-reader-digest.epub"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            built = self.payload(self.run_cli(tmp, "build", "2026-05-26"))
            self.assertEqual(Path(built["epubPath"]).name, "Stale Filename - May 26 2026.epub")
            rewritten = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(rewritten["filename"], "Stale Filename - May 26 2026.epub")

    def test_qa_rejects_date_based_attachment_filename_for_editorial_title(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            article = tmp / "article.md"
            article.write_text("# Editorial Filename\n\nBody.\n", encoding="utf-8")
            self.run_cli(tmp, "init")
            self.run_cli(tmp, "queue", "https://example.com/editorial", "--title", "Editorial Filename", "--file", str(article), "--build-mode", "local", "--date", "2026-05-26")
            self.run_cli(tmp, "prepare", "2026-05-26")
            built = self.payload(self.run_cli(tmp, "build", "2026-05-26"))
            source_epub = Path(built["epubPath"])
            stale_epub = source_epub.with_name("2026-05-26-reader-digest.epub")
            stale_epub.write_bytes(source_epub.read_bytes())

            qa = self.payload(self.run_cli(tmp, "qa", "2026-05-26", "--epub", str(stale_epub), check=False))
            self.assertEqual(qa["status"], "failed")
            self.assertTrue(any(check["name"] == "filename-matches-manifest" and not check["ok"] for check in qa["checks"]))
            self.assertTrue(any(check["name"] == "editorial-attachment-filename" and not check["ok"] for check in qa["checks"]))

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
            prepared_payload = json.loads(prepared.stdout)
            self.assertEqual(prepared_payload["status"], "prepared")
            self.assertEqual(prepared_payload["filename"], "Manifest Only - May 26 2026.epub")
            built = subprocess.run(base + ["build", "2026-05-26"], check=True, text=True, stdout=subprocess.PIPE)
            epub = Path(json.loads(built.stdout)["epubPath"])
            self.assertTrue(epub.exists())
            self.assertEqual(epub.name, "Manifest Only - May 26 2026.epub")

    def test_run_empty_queue_skips_without_building_epub(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self.run_cli(tmp, "init")
            result = self.payload(self.run_cli(tmp, "run", "2026-05-26"))
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "empty_queue")
            self.assertFalse((tmp / "reading-bundles" / "2026-05-26-kindle" / "dist" / "2026-05-26-reader-digest.epub").exists())

    def test_external_personal_db_schema_queue_prepare_build(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db = tmp / "personal.sqlite"
            con = sqlite3.connect(db)
            con.executescript(
                """
                create table sources (id text primary key, name text not null unique, kind text not null, metadata text not null default '{}', created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
                create table entities (id text primary key, type text not null, canonical_key text not null, title text not null, subtitle text, url text, metadata text not null default '{}', created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ','now')), updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ','now')), unique (type, canonical_key));
                create table library_items (id text primary key, entity_id text not null references entities(id), source_id text not null references sources(id), source_item_id text, title text not null, author text, publisher text, url text, normalized_url text, tags text not null default '[]', word_count integer, in_queue integer not null default 0, favorited integer not null default 0, read integer not null default 0, highlight_count integer not null default 0, last_interaction_at text, content_file_id text, content_path text, status text not null default 'imported', metadata text not null default '{}', created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ','now')), updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
                create table digests (id text primary key, digest_date text not null unique, title text, status text not null default 'draft', epub_path text, sent_at text, qa_passed integer, metadata text not null default '{}', created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ','now')), updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
                create table digest_items (digest_id text not null references digests(id), library_item_id text not null references library_items(id), position integer not null, chapter_path text, build_mode text, metadata text not null default '{}', primary key (digest_id, position));
                """
            )
            con.close()
            article = tmp / "podcast.md"
            article.write_text("# Podcast Transcript\n\nFull transcript body.\n", encoding="utf-8")
            base = [
                sys.executable,
                str(CLI),
                "--workspace",
                str(tmp),
                "--db",
                str(db),
                "--storage-mode",
                "external-sqlite",
                "--json",
            ]
            subprocess.run(base + ["queue", "https://podcasts.example/episode", "--title", "Podcast Transcript", "--file", str(article), "--build-mode", "local", "--date", "2026-05-26"], check=True, text=True, stdout=subprocess.PIPE)
            prepared = subprocess.run(base + ["prepare", "2026-05-26"], check=True, text=True, stdout=subprocess.PIPE)
            prepared_payload = json.loads(prepared.stdout)
            self.assertEqual(prepared_payload["status"], "prepared")
            self.assertEqual(prepared_payload["filename"], "Podcast Transcript - May 26 2026.epub")
            built = subprocess.run(base + ["build", "2026-05-26"], check=True, text=True, stdout=subprocess.PIPE)
            epub = Path(json.loads(built.stdout)["epubPath"])
            self.assertTrue(epub.exists())
            self.assertEqual(epub.name, "Podcast Transcript - May 26 2026.epub")


if __name__ == "__main__":
    unittest.main()
