# Reader Digest

Use this skill when the user asks to queue a link or file for a reader digest, build a digest EPUB, QA a digest, configure delivery, or run a scheduled reading bundle.

## Default Behavior

- Use the plugin CLI at scripts/reader_digest.py from this plugin directory.
- Keep storage and delivery config-driven. Do not hardcode private paths, email addresses, Kindle addresses, or account names.
- Treat delivery as external side effect. Do not send email unless the user has explicitly requested delivery or a configured scheduled job is running with --confirm-send.
- Prefer local Markdown captures for authenticated, blocked, or fragile sources, then queue them with --file and --build-mode local.

## Slash Command

For direct chat /digest <url>:

1. Queue the URL for today or the configured next digest.
2. If queue succeeds, react with thumbs-up and send no text reply.
3. If queue fails, send one concise error message.

For podcast episodes or video essays, treat the link as an implicit digest request and use Peter Steinberger's summarize CLI/skill before queueing. The queued local file must be a polished reading article, not a raw transcript, timestamp dump, or speaker-turn notes. Prefer summarizing the episode URL directly; if source extraction is incomplete, use summarize --extract, transcript fallback, or local transcription only as source material for the article.

Article capture sequence:

1. Run summarize with a prompt that asks for a long-form Markdown article for Conor's Kindle digest.
2. Save the article under the configured workspace content directory with a named Source link near the top.
3. Queue the original episode URL with --file <article-path> and --build-mode local.
4. Keep any raw transcript/audio as scratch evidence only; do not include it in the EPUB.
5. Stay silent on success; alert Conor only when summarize/source capture fails or queueing fails.

Recommended summarize prompt:

    Convert this podcast/video/source material into a polished long-form reading article for Conor's Kindle digest. Write as an article, not a transcript and not bullet notes. Preserve the best ideas, examples, and arguments. Use a strong title, short dek, and clear section headings. Keep named source links only; do not include long bare URLs. Do not include Transcript sections or speaker turn formatting. Output clean Markdown only.

Recommended command:

    scripts/reader_digest.py queue <url> --json

For blocked pages:

1. Capture the relevant readable text to a local Markdown file in the configured workspace.
2. Queue the source URL with --file <path> --build-mode local.
3. Keep the visible source URL as a named Source link in the Markdown rather than a long bare URL.

## Build Workflow

Use this sequence for manual or scheduled builds:

    scripts/reader_digest.py prepare YYYY-MM-DD --json
    scripts/reader_digest.py build YYYY-MM-DD --json
    scripts/reader_digest.py qa YYYY-MM-DD --json

The EPUB metadata/title should be a short editorial headline based on the contents, followed by the date. Do not use a generic title like `Reader Digest - <date>` when there are queued items. The output filename must match the editorial title because Kindle often surfaces the attachment filename more prominently than EPUB metadata, e.g. `Rick Rubin & AI Agents - May 26 2026.epub`.

Only after QA passes:

    scripts/reader_digest.py send YYYY-MM-DD --confirm-send --json

For complete scheduled runs:

    scripts/reader_digest.py run YYYY-MM-DD --send --confirm-send --json

Use --dry-run during setup or tests.

If the run returns `{"status": "skipped", "reason": "empty_queue"}`, treat it as a successful quiet no-op. Do not notify the user, do not build or send an empty EPUB, and do not mark the scheduled job as failed.

## Optional Newsletter Collection

Newsletter collection is optional and depends on a local mail CLI/config. It must be configured by the host user.

    scripts/reader_digest.py collect newsletters --account icloud --to-address newsletters@example.com --json

For scheduled pipeline:

    scripts/reader_digest.py run YYYY-MM-DD --collect-newsletters --account icloud --send --confirm-send --json

## Storage Modes

- plugin-sqlite: default portable queue and run state.
- external-sqlite: use an existing compatible SQLite database. If the Personal Database tables are present, the CLI writes queue items into entities, sources, and library_items.
- manifest-only: writes queue and digest state as JSON files only.

## Safety

- Email is never sent without --confirm-send.
- --dry-run suppresses email even when --confirm-send is present.
- Do not unflag, archive, or delete source messages unless a host-specific workflow explicitly owns that behavior after successful delivery.
