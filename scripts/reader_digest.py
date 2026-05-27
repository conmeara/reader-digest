#!/usr/bin/env python3
"""Reader Digest CLI.

Local-first EPUB digest builder for OpenClaw/Codex plugins.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import email.message
import html
import json
import os
import re
import shutil
import smtplib
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from xml.etree import ElementTree as ET


APP_DIR = Path.home() / ".local" / "share" / "openclaw-reader-digest"
DEFAULT_DB = APP_DIR / "reader_digest.sqlite"
DEFAULT_WORKSPACE = Path.cwd()
PERSONAL_TABLES = {"entities", "library_items", "sources", "digests", "digest_items"}


class DigestError(RuntimeError):
    pass


@dataclass
class Context:
    config: dict[str, Any]
    db_path: Path
    workspace: Path
    storage_mode: str
    json_output: bool
    dry_run: bool
    no_input: bool
    verbose: bool


def today_str() -> str:
    return date.today().isoformat()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_config_path() -> Path:
    return APP_DIR / "config.json"


def load_config(path: str | None) -> dict[str, Any]:
    config_path = Path(path).expanduser() if path else default_config_path()
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def resolve_context(args: argparse.Namespace) -> Context:
    config = load_config(getattr(args, "config", None))
    db_path = Path(args.db or config.get("dbPath") or DEFAULT_DB).expanduser()
    workspace = Path(args.workspace or config.get("workspace") or DEFAULT_WORKSPACE).expanduser()
    storage_mode = args.storage_mode or config.get("storageMode") or "plugin-sqlite"
    return Context(
        config=config,
        db_path=db_path,
        workspace=workspace,
        storage_mode=storage_mode,
        json_output=bool(args.json),
        dry_run=bool(args.dry_run),
        no_input=bool(args.no_input),
        verbose=bool(args.verbose),
    )


def emit(ctx: Context, payload: Any, text: str | None = None) -> None:
    if ctx.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif text:
        print(text)
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def ensure_dirs(ctx: Context) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    ctx.workspace.mkdir(parents=True, exist_ok=True)
    if ctx.storage_mode != "manifest-only":
        ctx.db_path.parent.mkdir(parents=True, exist_ok=True)


def connect(ctx: Context) -> sqlite3.Connection:
    if ctx.storage_mode == "manifest-only":
        raise DigestError("manifest-only storage does not use SQLite")
    ensure_dirs(ctx)
    con = sqlite3.connect(ctx.db_path)
    con.row_factory = sqlite3.Row
    return con


def existing_tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute("select name from sqlite_master where type='table'").fetchall()
    return {row[0] for row in rows}


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    rows = con.execute(f"pragma table_info({table})").fetchall()
    return {row["name"] for row in rows}


def detect_storage(ctx: Context, con: sqlite3.Connection) -> str:
    if ctx.storage_mode != "external-sqlite":
        return ctx.storage_mode
    tables = existing_tables(con)
    if PERSONAL_TABLES.issubset(tables):
        return "personal-db"
    return "external-sqlite"


def init_schema(ctx: Context) -> dict[str, Any]:
    ensure_dirs(ctx)
    if ctx.storage_mode == "manifest-only":
        return {"status": "ok", "storageMode": "manifest-only", "workspace": str(ctx.workspace)}
    with connect(ctx) as con:
        storage = detect_storage(ctx, con)
        if storage in {"plugin-sqlite", "external-sqlite"}:
            con.executescript(
                """
                create table if not exists queue_items (
                  id text primary key,
                  url text not null,
                  title text,
                  author text,
                  file_path text,
                  source text,
                  build_mode text not null default 'url',
                  digest_date text not null,
                  status text not null default 'queued',
                  created_at text not null,
                  metadata_json text
                );
                create table if not exists digest_runs (
                  id text primary key,
                  digest_date text not null,
                  title text,
                  manifest_path text,
                  epub_path text,
                  status text not null,
                  created_at text not null,
                  metadata_json text
                );
                create table if not exists digest_items (
                  digest_id text not null,
                  queue_item_id text,
                  position integer not null,
                  title text,
                  url text,
                  file_path text,
                  metadata_json text
                );
                """
            )
        return {"status": "ok", "storageMode": storage, "dbPath": str(ctx.db_path), "workspace": str(ctx.workspace)}


def queue_item(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    digest_date = args.date or today_str()
    item = {
        "id": str(uuid.uuid4()),
        "url": args.url,
        "title": args.title or infer_title(args.url),
        "author": args.author,
        "filePath": str(Path(args.file).expanduser()) if args.file else None,
        "source": args.source,
        "buildMode": args.build_mode,
        "digestDate": digest_date,
        "status": "queued",
        "createdAt": now_iso(),
        "metadata": {"plugin": "reader-digest"},
    }
    if ctx.storage_mode == "manifest-only":
        path = queue_manifest_path(ctx, digest_date)
        data = read_json(path, {"items": []})
        data["items"].append(item)
        write_json(path, data)
        return {"status": "queued", "storageMode": "manifest-only", "item": item}
    init_schema(ctx)
    with connect(ctx) as con:
        storage = detect_storage(ctx, con)
        if storage == "personal-db":
            insert_personal_db(con, item)
        else:
            con.execute(
                """
                insert into queue_items
                (id, url, title, author, file_path, source, build_mode, digest_date, status, created_at, metadata_json)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["id"],
                    item["url"],
                    item["title"],
                    item["author"],
                    item["filePath"],
                    item["source"],
                    item["buildMode"],
                    item["digestDate"],
                    item["status"],
                    item["createdAt"],
                    json.dumps(item["metadata"]),
                ),
            )
        con.commit()
    return {"status": "queued", "item": item}


def list_items(ctx: Context, digest_date: str | None) -> dict[str, Any]:
    digest_date = digest_date or today_str()
    if ctx.storage_mode == "manifest-only":
        return {"digestDate": digest_date, "items": read_json(queue_manifest_path(ctx, digest_date), {"items": []})["items"]}
    init_schema(ctx)
    with connect(ctx) as con:
        storage = detect_storage(ctx, con)
        if storage == "personal-db":
            items = read_personal_items(con, digest_date)
        else:
            rows = con.execute(
                "select * from queue_items where digest_date = ? and status = 'queued' order by created_at, title",
                (digest_date,),
            ).fetchall()
            items = [plugin_row_to_item(row) for row in rows]
    return {"digestDate": digest_date, "items": items}


def insert_personal_db(con: sqlite3.Connection, item: dict[str, Any]) -> None:
    source_id = "src_reader_digest_plugin"
    entity_id = str(uuid.uuid4())
    metadata = {
        "queued_for_digest_date": item["digestDate"],
        "build_mode": item["buildMode"],
        "file_path": item["filePath"],
        "source": item["source"],
        "plugin": "reader-digest",
    }
    source_cols = table_columns(con, "sources")
    entity_cols = table_columns(con, "entities")
    library_cols = table_columns(con, "library_items")

    if "metadata_json" in source_cols:
        source_id = str(uuid.uuid4())
        con.execute(
            "insert into sources (id, source_type, source_ref, imported_at, metadata_json) values (?, ?, ?, ?, ?)",
            (source_id, "reader-digest", item["url"], item["createdAt"], json.dumps(metadata)),
        )
    else:
        con.execute(
            """
            insert into sources (id, name, kind, metadata)
            values (?, ?, ?, ?)
            on conflict(name) do update set metadata = excluded.metadata
            """,
            (source_id, "Reader Digest Plugin", "reader_digest", json.dumps(metadata)),
        )

    if "canonical_url" in entity_cols:
        con.execute(
            "insert into entities (id, type, title, canonical_url, created_at, updated_at, metadata_json) values (?, ?, ?, ?, ?, ?, ?)",
            (entity_id, "article", item["title"], item["url"], item["createdAt"], item["createdAt"], json.dumps(metadata)),
        )
    else:
        con.execute(
            """
            insert into entities (id, type, canonical_key, title, url, metadata)
            values (?, ?, ?, ?, ?, ?)
            on conflict(type, canonical_key) do update set
              title = excluded.title,
              url = excluded.url,
              metadata = excluded.metadata,
              updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            """,
            (entity_id, "library_item", item["url"], item["title"], item["url"], json.dumps(metadata)),
        )
        entity_id = con.execute(
            "select id from entities where type = ? and canonical_key = ?",
            ("library_item", item["url"]),
        ).fetchone()["id"]

    if "metadata_json" in library_cols:
        con.execute(
            "insert into library_items (id, entity_id, source_id, status, created_at, metadata_json) values (?, ?, ?, ?, ?, ?)",
            (item["id"], entity_id, source_id, "queued", item["createdAt"], json.dumps(metadata)),
        )
    else:
        con.execute(
            """
            insert into library_items
            (id, entity_id, source_id, source_item_id, title, author, url, normalized_url, tags,
             in_queue, favorited, read, highlight_count, last_interaction_at, content_file_id,
             content_path, status, metadata)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set
              title = excluded.title,
              url = excluded.url,
              normalized_url = excluded.normalized_url,
              in_queue = excluded.in_queue,
              last_interaction_at = excluded.last_interaction_at,
              content_file_id = excluded.content_file_id,
              content_path = excluded.content_path,
              status = excluded.status,
              metadata = excluded.metadata,
              updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            """,
            (
                item["id"],
                entity_id,
                source_id,
                item["url"],
                item["title"],
                item.get("author"),
                item["url"],
                item["url"],
                json.dumps(["digest-queue"]),
                1,
                0,
                0,
                0,
                item["createdAt"],
                Path(item["filePath"]).name if item.get("filePath") else None,
                item.get("filePath"),
                "queued_for_digest",
                json.dumps(metadata),
            ),
        )
    if "events" in existing_tables(con):
        event_cols = table_columns(con, "events")
        if "metadata_json" in event_cols:
            con.execute(
                "insert into events (id, entity_id, event_type, occurred_at, metadata_json) values (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), entity_id, "queued_for_digest", item["createdAt"], json.dumps(metadata)),
            )
        else:
            con.execute(
                "insert into events (id, entity_id, source_id, type, occurred_at, metadata) values (?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), entity_id, source_id, "queued_for_digest", item["createdAt"], json.dumps(metadata)),
            )


def read_personal_items(con: sqlite3.Connection, digest_date: str) -> list[dict[str, Any]]:
    entity_cols = table_columns(con, "entities")
    library_cols = table_columns(con, "library_items")
    url_expr = "e.canonical_url" if "canonical_url" in entity_cols else "coalesce(li.url, e.url)"
    created_expr = "li.created_at" if "created_at" in library_cols else "li.last_interaction_at"
    metadata_expr = "li.metadata_json" if "metadata_json" in library_cols else "li.metadata"
    status_filter = (
        "li.status = 'queued'"
        if "metadata_json" in library_cols
        else "(li.status = 'queued_for_digest' or li.status = 'queued' or li.in_queue = 1)"
    )
    rows = con.execute(
        f"""
        select li.id, e.title, {url_expr} as url, {created_expr} as created_at, {metadata_expr} as metadata_json
        from library_items li
        join entities e on e.id = li.entity_id
        where {status_filter}
        order by created_at, e.title
        """
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        metadata = safe_json(row["metadata_json"])
        if metadata.get("queued_for_digest_date") != digest_date:
            continue
        items.append(
            {
                "id": row["id"],
                "url": row["url"],
                "title": row["title"] or infer_title(row["url"]),
                "author": None,
                "filePath": metadata.get("file_path"),
                "source": metadata.get("source"),
                "buildMode": metadata.get("build_mode", "url"),
                "digestDate": digest_date,
                "status": "queued",
                "createdAt": row["created_at"],
                "metadata": metadata,
            }
        )
    return items


def plugin_row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "url": row["url"],
        "title": row["title"] or infer_title(row["url"]),
        "author": row["author"],
        "filePath": row["file_path"],
        "source": row["source"],
        "buildMode": row["build_mode"],
        "digestDate": row["digest_date"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "metadata": safe_json(row["metadata_json"]),
    }


def prepare_digest(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    digest_date = args.date or today_str()
    listed = list_items(ctx, digest_date)
    items = listed["items"]
    bundle = bundle_dir(ctx, digest_date)
    articles = bundle / "articles"
    articles.mkdir(parents=True, exist_ok=True)
    title = args.title or make_digest_title(items, digest_date)
    manifest_items: list[dict[str, Any]] = []
    for index, item in enumerate(items, 1):
        file_path = item.get("filePath")
        article_rel = None
        if file_path:
            src = Path(file_path).expanduser()
            if src.exists():
                dest = articles / f"{index:03d}-{slugify(item['title'])}.md"
                shutil.copyfile(src, dest)
                article_rel = str(dest.relative_to(bundle))
        manifest_items.append(
            {
                "id": item["id"],
                "title": item["title"],
                "url": item["url"],
                "author": item.get("author"),
                "buildMode": item.get("buildMode", "url"),
                "file": article_rel,
                "sourceFile": file_path,
            }
        )
    manifest = {
        "title": title,
        "date": digest_date,
        "buildMode": "mixed",
        "createdAt": now_iso(),
        "items": manifest_items,
    }
    path = bundle / "manifest.json"
    write_json(path, manifest)
    return {"status": "prepared", "manifestPath": str(path), "itemCount": len(items), "title": title}


def build_digest(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    digest_date = args.date or today_str()
    bundle = bundle_dir(ctx, digest_date)
    manifest_path = Path(args.manifest).expanduser() if args.manifest else bundle / "manifest.json"
    manifest = read_json(manifest_path)
    dist = bundle / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    epub_path = dist / f"{digest_date}-reader-digest.epub"
    chapters = build_chapters(bundle, manifest)
    write_epub(epub_path, manifest, chapters)
    record_digest_run(ctx, digest_date, manifest, str(manifest_path), str(epub_path), "built")
    return {"status": "built", "epubPath": str(epub_path), "chapterCount": len(chapters)}


def build_chapters(bundle: Path, manifest: dict[str, Any]) -> list[dict[str, str]]:
    chapters = []
    for index, item in enumerate(manifest.get("items", []), 1):
        title = item.get("title") or infer_title(item.get("url", "Untitled"))
        content = None
        file_name = item.get("file")
        if file_name:
            path = bundle / file_name
            if path.exists():
                content = markdown_to_html(path.read_text(encoding="utf-8"), title)
        elif item.get("buildMode") == "url" and shutil.which("percollate"):
            content = fetch_with_percollate(item.get("url"), title)
        if not content:
            content = source_stub_html(title, item.get("url"))
        chapters.append(
            {
                "id": f"chapter-{index}",
                "href": f"chapter-{index}.xhtml",
                "title": title,
                "body": content,
            }
        )
    return chapters


def fetch_with_percollate(url: str | None, title: str) -> str | None:
    if not url:
        return None
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "article.html"
        proc = subprocess.run(
            ["percollate", "html", "--output", str(out), "--title", title, url],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if proc.returncode == 0 and out.exists():
            return clean_html_fragment(out.read_text(encoding="utf-8", errors="replace"))
    return None


def markdown_to_html(markdown: str, fallback_title: str) -> str:
    lines = markdown.splitlines()
    body: list[str] = []
    in_list = False
    in_code = False
    code_lines: list[str] = []
    fence = chr(96) * 3
    for line in lines:
        raw = line.rstrip()
        if raw.startswith(fence):
            if not in_code:
                in_code = True
                code_lines = []
            else:
                body.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
                in_code = False
            continue
        if in_code:
            code_lines.append(raw)
            continue
        if not raw.strip():
            if in_list:
                body.append("</ul>")
                in_list = False
            continue
        if raw.startswith("# "):
            body.append(f"<h1>{html.escape(raw[2:].strip() or fallback_title)}</h1>")
        elif raw.startswith("## "):
            body.append(f"<h2>{html.escape(raw[3:].strip())}</h2>")
        elif raw.startswith(("- ", "* ")):
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{inline_markdown(raw[2:].strip())}</li>")
        else:
            if in_list:
                body.append("</ul>")
                in_list = False
            body.append(f"<p>{inline_markdown(raw.strip())}</p>")
    if in_list:
        body.append("</ul>")
    if in_code:
        body.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
    return "\n".join(body) or source_stub_html(fallback_title, None)


def inline_markdown(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>', escaped)
    return escaped


def clean_html_fragment(raw: str) -> str:
    match = re.search(r"<body[^>]*>(.*)</body>", raw, re.I | re.S)
    fragment = match.group(1) if match else raw
    fragment = re.sub(r"<script[^>]*>.*?</script>", "", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<style[^>]*>.*?</style>", "", fragment, flags=re.I | re.S)
    return fragment.strip()


def source_stub_html(title: str, url: str | None) -> str:
    source = f'<p><a href="{html.escape(url, quote=True)}">Source</a></p>' if url else ""
    return f"<h1>{html.escape(title)}</h1><p>This item is queued for the digest, but no local article text was available.</p>{source}"


def write_epub(epub_path: Path, manifest: dict[str, Any], chapters: list[dict[str, str]]) -> None:
    title = manifest.get("title") or "Reader Digest"
    uid = f"reader-digest-{manifest.get('date') or today_str()}"
    files: dict[str, bytes] = {
        "META-INF/container.xml": container_xml().encode("utf-8"),
        "OEBPS/styles.css": epub_css().encode("utf-8"),
        "OEBPS/cover.png": transparent_png(),
    }
    contents = contents_page(title, chapters)
    files["OEBPS/contents.xhtml"] = xhtml_doc(title, contents).encode("utf-8")
    for chapter in chapters:
        files[f"OEBPS/{chapter['href']}"] = xhtml_doc(chapter["title"], chapter["body"]).encode("utf-8")
    files["OEBPS/nav.xhtml"] = nav_doc(title, chapters).encode("utf-8")
    files["OEBPS/content.opf"] = opf_doc(uid, title, manifest, chapters).encode("utf-8")
    with zipfile.ZipFile(epub_path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, data in files.items():
            zf.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)


def container_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""


def epub_css() -> str:
    return "body{font-family:serif;line-height:1.45;margin:5%;} img{max-width:100%;} nav ol{line-height:1.7;} .source{font-size:.9em;color:#555;}"


def xhtml_doc(title: str, body: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head><title>{html.escape(title)}</title><link rel="stylesheet" type="text/css" href="styles.css"/></head>
<body>{body}</body>
</html>"""


def contents_page(title: str, chapters: list[dict[str, str]]) -> str:
    links = "\n".join(f'<li><a href="{c["href"]}">{html.escape(c["title"])}</a></li>' for c in chapters)
    return f"<h1>{html.escape(title)}</h1><h2>Contents</h2><ol>{links}</ol>"


def nav_doc(title: str, chapters: list[dict[str, str]]) -> str:
    links = "\n".join(f'<li><a href="{c["href"]}">{html.escape(c["title"])}</a></li>' for c in chapters)
    return xhtml_doc(title, f'<nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops"><h1>Contents</h1><ol>{links}</ol></nav>')


def opf_doc(uid: str, title: str, manifest: dict[str, Any], chapters: list[dict[str, str]]) -> str:
    item_lines = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="contents" href="contents.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="css" href="styles.css" media-type="text/css"/>',
        '<item id="cover-image" href="cover.png" media-type="image/png" properties="cover-image"/>',
    ]
    spine = ['<itemref idref="contents"/>']
    for chapter in chapters:
        item_lines.append(f'<item id="{chapter["id"]}" href="{chapter["href"]}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{chapter["id"]}"/>')
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">{html.escape(uid)}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:language>en</dc:language>
    <dc:date>{html.escape(manifest.get("date") or today_str())}</dc:date>
    <meta name="cover" content="cover-image"/>
  </metadata>
  <manifest>
    {chr(10).join(item_lines)}
  </manifest>
  <spine>
    {chr(10).join(spine)}
  </spine>
</package>"""


def transparent_png() -> bytes:
    return base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")


def qa_epub(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    digest_date = args.date or today_str()
    epub_path = Path(args.epub).expanduser() if args.epub else bundle_dir(ctx, digest_date) / "dist" / f"{digest_date}-reader-digest.epub"
    checks: list[dict[str, Any]] = []
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            failures.append(f"{name}: {detail}")

    check("exists", epub_path.exists(), str(epub_path))
    if not epub_path.exists():
        return {"status": "failed", "epubPath": str(epub_path), "checks": checks, "failures": failures}
    try:
        with zipfile.ZipFile(epub_path) as zf:
            bad = zf.testzip()
            names = zf.namelist()
            check("zip-integrity", bad is None, bad or "ok")
            check("mimetype-first", names[0] == "mimetype", names[0] if names else "empty")
            opf = zf.read("OEBPS/content.opf").decode("utf-8")
            check("nav-toc", "properties=\"nav\"" in opf and "OEBPS/nav.xhtml" in names, "nav present")
            check("cover", "cover-image" in opf and "OEBPS/cover.png" in names, "cover metadata/image present")
            check("contents-first", "<itemref idref=\"contents\"" in opf, "contents in spine")
            visible = "\n".join(
                zf.read(name).decode("utf-8", errors="replace")
                for name in names
                if name.endswith(".xhtml")
            )
            check("no-paragraph-markers", "¶" not in visible, "no visible paragraph markers")
            long_urls = re.findall(r">https?://[^<\s]{80,}<", visible)
            check("no-long-visible-urls", not long_urls, f"{len(long_urls)} long visible URLs")
    except Exception as exc:
        check("readable", False, str(exc))
    return {"status": "passed" if not failures else "failed", "epubPath": str(epub_path), "checks": checks, "failures": failures}


def send_digest(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    digest_date = args.date or today_str()
    epub_path = Path(args.epub).expanduser() if args.epub else bundle_dir(ctx, digest_date) / "dist" / f"{digest_date}-reader-digest.epub"
    recipient = args.to or ctx.config.get("kindleEmail") or os.environ.get("READER_DIGEST_KINDLE_EMAIL") or os.environ.get("KINDLE_EMAIL")
    if not args.confirm_send:
        return {"status": "not-sent", "reason": "missing --confirm-send", "epubPath": str(epub_path), "to": recipient}
    if ctx.dry_run:
        return {"status": "dry-run", "epubPath": str(epub_path), "to": recipient}
    if not recipient:
        raise DigestError("missing Kindle recipient; pass --to or set READER_DIGEST_KINDLE_EMAIL")
    if not epub_path.exists():
        raise DigestError(f"EPUB does not exist: {epub_path}")
    smtp = smtp_settings(ctx, args.smtp_profile)
    subject = Path(epub_path).stem
    msg = email.message.EmailMessage()
    msg["From"] = smtp["from"]
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content("Attached reader digest.")
    msg.add_attachment(epub_path.read_bytes(), maintype="application", subtype="epub+zip", filename=epub_path.name)
    with smtplib.SMTP_SSL(smtp["host"], int(smtp["port"])) as client:
        if smtp.get("user"):
            client.login(smtp["user"], smtp["password"])
        client.send_message(msg)
    return {"status": "sent", "epubPath": str(epub_path), "to": recipient}


def smtp_settings(ctx: Context, profile: str) -> dict[str, str]:
    if profile == "himalaya":
        settings = read_himalaya_smtp()
        if settings:
            return settings
    required = {
        "host": os.environ.get("READER_DIGEST_SMTP_HOST"),
        "port": os.environ.get("READER_DIGEST_SMTP_PORT", "465"),
        "user": os.environ.get("READER_DIGEST_SMTP_USER"),
        "password": os.environ.get("READER_DIGEST_SMTP_PASSWORD"),
        "from": os.environ.get("READER_DIGEST_SMTP_FROM") or os.environ.get("READER_DIGEST_SMTP_USER"),
    }
    if not required["host"] or not required["from"]:
        raise DigestError("missing SMTP config; set READER_DIGEST_SMTP_* env vars or use --smtp-profile himalaya")
    return required


def read_himalaya_smtp() -> dict[str, str] | None:
    path = Path.home() / ".config" / "himalaya" / "config.toml"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    data: dict[str, str] = {}
    for key, env_key in {
        "smtp-host": "host",
        "smtp-port": "port",
        "smtp-login": "user",
        "smtp-passwd": "password",
        "email": "from",
    }.items():
        match = re.search(rf"^\s*{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]", text, flags=re.M)
        if match:
            data[env_key] = match.group(1)
    return data if data.get("host") and data.get("from") else None


def collect_newsletters(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    to_address = args.to_address or ctx.config.get("newsletter", {}).get("toAddress")
    if not to_address:
        raise DigestError("newsletter collection needs --to-address or config.newsletter.toAddress")
    if shutil.which("himalaya") is None:
        raise DigestError("himalaya CLI is not installed")
    digest_date = args.date or today_str()
    bundle = bundle_dir(ctx, digest_date)
    articles = bundle / "articles"
    articles.mkdir(parents=True, exist_ok=True)
    account = args.account or ctx.config.get("newsletter", {}).get("account")
    query = f"flag flagged and to {to_address} order by date asc"
    list_cmd = ["himalaya", "envelope", "list"]
    if account:
        list_cmd.extend(["-a", account])
    list_cmd.extend(["-f", args.folder, "-s", "100", "-o", "json", query])
    proc = subprocess.run(
        list_cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    if proc.returncode != 0:
        raise DigestError(proc.stderr.strip() or "himalaya list failed")
    messages = json.loads(proc.stdout or "[]")
    collected = []
    for index, envelope in enumerate(messages, 1):
        message_id = str(envelope.get("id"))
        subject = envelope.get("subject") or "Flagged newsletter"
        sender = envelope.get("from") or {}
        sender_name = sender.get("name") or sender.get("addr") or "Newsletter"
        read_cmd = ["himalaya", "message", "read"]
        if account:
            read_cmd.extend(["-a", account])
        read_cmd.extend(["-f", args.folder, "-o", "json", message_id])
        body_proc = subprocess.run(read_cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        if body_proc.returncode != 0:
            raise DigestError(body_proc.stderr.strip() or f"himalaya read failed for message {message_id}")
        body = message_body_from_himalaya_json(body_proc.stdout)
        body = clean_message_body(strip_headers(body))
        path = articles / f"nl{index:02d}-{slugify(subject)}.md"
        path.write_text(
            "\n".join(
                [
                    f"# {subject}",
                    "",
                    f"From: {sender_name}",
                    f"Date: {envelope.get('date', '')}",
                    "",
                    body,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        queue_args = argparse.Namespace(
            url=f"newsletter:{message_id}",
            title=subject,
            author=sender_name,
            file=str(path),
            build_mode="local",
            date=digest_date,
            source="newsletter",
        )
        queue_item(ctx, queue_args)
        collected.append({"id": message_id, "title": subject, "from": sender_name, "file": str(path)})
    return {"status": "collected", "count": len(collected), "items": collected}


def message_body_from_himalaya_json(raw: str) -> str:
    data = json.loads(raw or "\"\"")
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("body", "text", "content"):
            value = data.get(key)
            if isinstance(value, str):
                return value
    return str(data)


def strip_headers(message: str) -> str:
    parts = message.split("\n\n", 1)
    return parts[1] if len(parts) == 2 else message


def clean_message_body(body: str) -> str:
    body = body.replace("\r\n", "\n")
    body = normalize_inline_links(body)
    body = normalize_bare_urls(body)
    body = re.sub(r"\n{4,}", "\n\n\n", body)
    body = re.sub(r"(?i)\nunsubscribe https?://\S+.*$", "", body, flags=re.S)
    return body.strip()


def normalize_inline_links(body: str) -> str:
    pattern = re.compile(r"(?P<label>[^\n\[\]]{1,180}?)\s*\[\s*(?P<url>https?://[^\]\s]+)\s*\]")

    def repl(match: re.Match[str]) -> str:
        label = re.sub(r"\s+", " ", match.group("label")).strip().strip(",:")
        return f"[{label or 'link'}]({match.group('url')})"

    previous = None
    while previous != body:
        previous = body
        body = pattern.sub(repl, body)
    return body


def normalize_bare_urls(body: str) -> str:
    return re.sub(r"(?<!\]\()https?://\S+", r"[link](\g<0>)", body)


def run_pipeline(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    digest_date = args.date or today_str()
    steps: list[dict[str, Any]] = []
    if args.collect_newsletters:
        ns = argparse.Namespace(date=digest_date, folder=args.folder, to_address=args.to_address, account=args.account)
        steps.append({"collect": collect_newsletters(ctx, ns)})
    prepared = prepare_digest(ctx, argparse.Namespace(date=digest_date, title=args.title))
    steps.append({"prepare": prepared})
    built = build_digest(ctx, argparse.Namespace(date=digest_date, manifest=None))
    steps.append({"build": built})
    qa = qa_epub(ctx, argparse.Namespace(date=digest_date, epub=None))
    steps.append({"qa": qa})
    if qa["status"] != "passed":
        return {"status": "failed", "steps": steps}
    if args.send:
        sent = send_digest(ctx, argparse.Namespace(date=digest_date, epub=None, to=args.to, smtp_profile=args.smtp_profile, confirm_send=args.confirm_send))
        steps.append({"send": sent})
    return {"status": "ok", "steps": steps}


def record_digest_run(ctx: Context, digest_date: str, manifest: dict[str, Any], manifest_path: str, epub_path: str, status: str) -> None:
    if ctx.storage_mode == "manifest-only":
        return
    init_schema(ctx)
    with connect(ctx) as con:
        storage = detect_storage(ctx, con)
        if storage == "personal-db":
            if "digests" in existing_tables(con):
                run_id = str(uuid.uuid4())
                digest_cols = table_columns(con, "digests")
                metadata = json.dumps({"plugin": "reader-digest", "manifest_path": manifest_path})
                if "metadata_json" in digest_cols:
                    con.execute(
                        "insert into digests (id, digest_date, title, manifest_path, epub_path, status, created_at, metadata_json) values (?, ?, ?, ?, ?, ?, ?, ?)",
                        (run_id, digest_date, manifest.get("title"), manifest_path, epub_path, status, now_iso(), metadata),
                    )
                else:
                    con.execute(
                        """
                        insert into digests (id, digest_date, title, status, epub_path, metadata, updated_at)
                        values (?, ?, ?, ?, ?, ?, ?)
                        on conflict(digest_date) do update set
                          title = excluded.title,
                          status = excluded.status,
                          epub_path = excluded.epub_path,
                          metadata = excluded.metadata,
                          updated_at = excluded.updated_at
                        """,
                        (run_id, digest_date, manifest.get("title"), status, epub_path, metadata, now_iso()),
                    )
        else:
            con.execute(
                "insert into digest_runs (id, digest_date, title, manifest_path, epub_path, status, created_at, metadata_json) values (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), digest_date, manifest.get("title"), manifest_path, epub_path, status, now_iso(), json.dumps({"plugin": "reader-digest"})),
            )
        con.commit()


def import_digests(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    return {"status": "ok", "message": "Digest runs are recorded during build; external import hooks can call this command after delivery."}


def doctor(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    checks = {
        "workspace": str(ctx.workspace),
        "dbPath": str(ctx.db_path),
        "storageMode": ctx.storage_mode,
        "percollate": shutil.which("percollate"),
        "himalaya": shutil.which("himalaya"),
        "configPath": str(default_config_path()),
    }
    if ctx.storage_mode != "manifest-only" and ctx.db_path.exists():
        with connect(ctx) as con:
            checks["detectedStorage"] = detect_storage(ctx, con)
            checks["tables"] = sorted(existing_tables(con))
    return {"status": "ok", "checks": checks}


def config_show(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    return {"config": ctx.config, "resolved": {"dbPath": str(ctx.db_path), "workspace": str(ctx.workspace), "storageMode": ctx.storage_mode}}


def make_digest_title(items: list[dict[str, Any]], digest_date: str) -> str:
    display_date = datetime.strptime(digest_date, "%Y-%m-%d").strftime("%b %d, %Y").replace(" 0", " ")
    if not items:
        return f"Reader Digest - {display_date}"
    titles = [short_title(item.get("title") or item.get("url") or "Item") for item in items[:3]]
    return f"{', '.join(titles)} - {display_date}"


def short_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip()[:42].rstrip()


def infer_title(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc:
        stem = Path(parsed.path).stem.replace("-", " ").replace("_", " ").strip()
        return stem.title() if stem else parsed.netloc
    return Path(url).stem or "Untitled"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug[:70] or "item"


def bundle_dir(ctx: Context, digest_date: str) -> Path:
    return ctx.workspace / "reading-bundles" / f"{digest_date}-kindle"


def queue_manifest_path(ctx: Context, digest_date: str) -> Path:
    path = ctx.workspace / "state" / f"reader_digest_queue_{digest_date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        if default is not None:
            return default
        raise DigestError(f"missing file: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def safe_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Queue links, build EPUB reader digests, QA them, and optionally email them to Kindle.")
    parser.add_argument("--config", help="Config JSON path. Defaults to ~/.local/share/openclaw-reader-digest/config.json")
    parser.add_argument("--db", help="SQLite DB path for plugin-sqlite or external-sqlite storage.")
    parser.add_argument("--storage-mode", choices=["plugin-sqlite", "external-sqlite", "manifest-only"], help="Queue/run storage backend.")
    parser.add_argument("--workspace", help="Workspace for reading-bundles and state output.")
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    parser.add_argument("--no-input", action="store_true", help="Never prompt interactively.")
    parser.add_argument("--dry-run", action="store_true", help="Do not perform external side effects.")
    parser.add_argument("--verbose", action="store_true", help="Print extra diagnostics.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create local workspace/db structures.")

    queue = sub.add_parser("queue", help="Queue a URL or list queued items.")
    queue.add_argument("url", nargs="?", help="URL to queue, or 'list'.")
    queue.add_argument("--title")
    queue.add_argument("--author")
    queue.add_argument("--file", help="Local Markdown file to use as article body.")
    queue.add_argument("--build-mode", choices=["url", "local"], default="url")
    queue.add_argument("--date")
    queue.add_argument("--source")

    prepare = sub.add_parser("prepare", help="Create a digest manifest for a date.")
    prepare.add_argument("date", nargs="?")
    prepare.add_argument("--title")

    build = sub.add_parser("build", help="Build EPUB from a prepared manifest.")
    build.add_argument("date", nargs="?")
    build.add_argument("--manifest")

    qa = sub.add_parser("qa", help="Run EPUB QA checks.")
    qa.add_argument("date", nargs="?")
    qa.add_argument("--epub")

    send = sub.add_parser("send", help="Email EPUB to a Kindle address.")
    send.add_argument("date", nargs="?")
    send.add_argument("--epub")
    send.add_argument("--to")
    send.add_argument("--smtp-profile", choices=["env", "himalaya"], default="env")
    send.add_argument("--confirm-send", action="store_true")

    run = sub.add_parser("run", help="Prepare, build, QA, and optionally send.")
    run.add_argument("date", nargs="?")
    run.add_argument("--title")
    run.add_argument("--collect-newsletters", action="store_true")
    run.add_argument("--folder", default="Newsletters")
    run.add_argument("--to-address")
    run.add_argument("--account")
    run.add_argument("--send", action="store_true")
    run.add_argument("--to")
    run.add_argument("--smtp-profile", choices=["env", "himalaya"], default="env")
    run.add_argument("--confirm-send", action="store_true")

    collect = sub.add_parser("collect", help="Collect optional sources.")
    collect.add_argument("kind", choices=["newsletters"])
    collect.add_argument("--date")
    collect.add_argument("--folder", default="Newsletters")
    collect.add_argument("--to-address")
    collect.add_argument("--account")

    sub.add_parser("import-digests", help="Import sent digest state into the configured DB.")

    config = sub.add_parser("config", help="Config helpers.")
    config.add_argument("action", choices=["show"])

    sub.add_parser("doctor", help="Show environment and storage diagnostics.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx = resolve_context(args)
    try:
        if args.command == "init":
            result = init_schema(ctx)
        elif args.command == "queue":
            if args.url == "list":
                result = list_items(ctx, args.date)
            elif args.url:
                result = queue_item(ctx, args)
            else:
                raise DigestError("queue requires a URL or 'list'")
        elif args.command == "prepare":
            result = prepare_digest(ctx, args)
        elif args.command == "build":
            result = build_digest(ctx, args)
        elif args.command == "qa":
            result = qa_epub(ctx, args)
        elif args.command == "send":
            result = send_digest(ctx, args)
        elif args.command == "run":
            result = run_pipeline(ctx, args)
        elif args.command == "collect":
            result = collect_newsletters(ctx, args)
        elif args.command == "import-digests":
            result = import_digests(ctx, args)
        elif args.command == "config":
            result = config_show(ctx, args)
        elif args.command == "doctor":
            result = doctor(ctx, args)
        else:
            parser.error("unknown command")
            return 2
        emit(ctx, result)
        return 0 if result.get("status") not in {"failed"} else 1
    except DigestError as exc:
        payload = {"status": "error", "error": str(exc)}
        emit(ctx, payload, f"reader-digest: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
