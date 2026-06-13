# Reader Digest

Reader Digest is a local-first OpenClaw/Codex plugin for turning queued links and local Markdown into polished EPUB reading bundles, then optionally emailing the EPUB to a Kindle address.

It is designed to work three ways:

- Plugin SQLite: ships with its own queue and digest-run database.
- External SQLite: can write to a compatible external database, including a Personal Database schema with entities, sources, library_items, digests, and digest_items.
- Manifest only: writes JSON manifests and EPUB files without database state.

Email delivery is built in, but guarded. The CLI never sends mail unless --confirm-send is present, and --dry-run always suppresses delivery.

## Install

From the plugin directory:

    chmod +x scripts/reader_digest.py
    chmod +x bin/reader-digest
    scripts/reader_digest.py init

For a shell alias:

    alias reader-digest="/path/to/plugins/reader-digest/bin/reader-digest"

Optional tools:

- percollate: improves public URL extraction.
- himalaya: optional SMTP/profile integration and newsletter collection.

## CLI

Initialize local storage:

    reader-digest init

Queue a URL for today:

    reader-digest queue https://example.com/article --title "Article Title"

Queue a local Markdown capture:

    reader-digest queue https://x.com/some/post --title "Thread capture" --file ./captures/thread.md --build-mode local

List queued items:

    reader-digest queue list

Prepare a manifest:

    reader-digest prepare 2026-05-26

Prepared manifests set the EPUB filename from the editorial title because Kindle surfaces attachment filenames prominently. A digest titled `Rick Rubin & AI Agents - May 26, 2026` builds as `Rick Rubin & AI Agents - May 26 2026.epub`, not a machine slug.

Build and QA:

    reader-digest build 2026-05-26
    reader-digest qa 2026-05-26

Send to Kindle after QA:

    reader-digest send 2026-05-26 --to your-kindle@example.com --confirm-send

Full run:

    reader-digest run 2026-05-26 --send --to your-kindle@example.com --confirm-send

When a scheduled run has no queued items and no collected newsletter chapters, it exits as a quiet no-op:

    {"status": "skipped", "reason": "empty_queue"}

Treat that as success. Do not build or send an empty EPUB.

Dry run delivery:

    reader-digest --dry-run run 2026-05-26 --send --to your-kindle@example.com --confirm-send

## Config

Default config path:

    ~/.local/share/openclaw-reader-digest/config.json

Example:

    {
      "workspace": "/Users/me/reader-digest",
      "storageMode": "plugin-sqlite",
      "dbPath": "/Users/me/reader-digest/reader_digest.sqlite",
      "kindleEmail": "name_123@kindle.com",
      "newsletter": {
        "account": "icloud",
        "toAddress": "newsletters@example.com"
      }
    }

SMTP can be provided through environment variables:

- READER_DIGEST_SMTP_HOST
- READER_DIGEST_SMTP_PORT
- READER_DIGEST_SMTP_USER
- READER_DIGEST_SMTP_PASSWORD
- READER_DIGEST_SMTP_FROM
- READER_DIGEST_KINDLE_EMAIL

The CLI can also try --smtp-profile himalaya if a local Himalaya config exists.

## OpenClaw Slash Command

The companion skill implements the expected direct-chat behavior:

- /digest <url> queues the URL for the next digest.
- On success, react with a thumbs-up and stay silent.
- If a site is likely blocked or authenticated, save a local Markdown chapter and queue it with --file and --build-mode local.
- Errors should be sent as concise messages.

## EPUB Quality Gate

reader-digest qa checks:

- EPUB zip integrity.
- mimetype is first.
- nav TOC exists.
- first page contents are in the spine.
- cover metadata and image exist.
- no visible paragraph markers.
- no long visible source URLs in reading text.

## Public Plugin Rules

This plugin must not hardcode private paths, email addresses, Kindle addresses, or account names. Keep those in config, environment variables, or the host OpenClaw installation.
