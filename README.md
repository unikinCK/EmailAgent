# EmailAgent

Python CLI app to organize an IMAP mailbox using a **local LLM** (for example LM Studio).

## Safety guarantees

- Credentials are read from environment variables only.
- By default, messages are **not deleted** by this app.
- Moves are done with IMAP `MOVE` when available.
- If server does not support `MOVE`, real processing fails fast unless `--allow-copy-delete-fallback` is set.
- `--dry-run` can still classify/preview assignments even when `MOVE` is unavailable.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Load env values:

```bash
set -a
source .env
set +a
```

## Environment variables

| Variable | Required | Description |
|---|---:|---|
| `IMAP_HOST` | ✅ | IMAP server hostname |
| `IMAP_PORT` | ✅ | IMAP port, usually `993` |
| `IMAP_USER` | ✅ | IMAP login username |
| `IMAP_PASSWORD` | ✅ | IMAP password / app password |
| `IMAP_MAILBOX` | ❌ | Source mailbox (default `INBOX`) |
| `IMAP_SENT_MAILBOX` | ❌ | Sent mailbox checked for existing replies (default `Sent`) |
| `IMAP_TLS` | ❌ | `true/false` (default `true`) |
| `LLM_ENDPOINT` | ✅ | OpenAI-compatible chat completions URL |
| `LLM_MODEL` | ✅ | Model name exposed by local runtime |
| `LLM_API_KEY` | ❌ | Optional bearer token if auth enabled |
| `LLM_TIMEOUT_SECONDS` | ❌ | HTTP timeout (default `120`) |
| `LLM_TEMPERATURE` | ❌ | Generation temperature (default `0.1`) |
| `LLM_MAX_TOKENS` | ❌ | Completion cap (default `700`) |
| `LLM_MAX_CONTEXT_TOKENS` | ❌ | Model context window used for safety clamping (default `4000`) |
| `LLM_INPUT_TOKEN_BUDGET` | ❌ | Approx max prompt tokens kept per request (default `3000`) |
| `STATE_DIR` | ❌ | Local state path (default `.state`) |

## Workflow

### 1) Scan phase (category discovery)

```bash
python app.py scan --sample-size 600 --max-categories 12
```

This phase:
- Reads all UIDs from the mailbox.
- Samples messages across the full UID range (not only newest mail).
- Fetches headers + small body snippets.
- Uses LLM to build category plan.
- Writes `.state/categories.json`.

### 2) Process phase (classification + move)

Start safely with dry-run:

```bash
python app.py process --max-messages 500 --dry-run
```

Then run real move:

```bash
python app.py process --max-messages 500
```

If your server does not support `MOVE`, you can opt in to a fallback mode that does `COPY` then marks originals `\\Deleted` and runs `EXPUNGE`:

```bash
python app.py process --max-messages 500 --allow-copy-delete-fallback
```

During processing, message flags (`\\Seen`, `\\Answered`, etc.) and Sent mailbox matches are included as classification context so the model can make better folder decisions without automatically skipping those messages.

## Strategy for large mailboxes with a ~20B model

For 100k+ mailboxes, avoid one-shot prompts and process in stages:

1. **Global sampling pass**
   - Sample UIDs uniformly over mailbox history.
   - Build stable top-level categories from representative mail.

2. **Staged rollout**
   - Run `process` with `--max-messages` first (e.g., 1k) and review folders.
   - Increase gradually (5k, 20k, full).

3. **Token control**
   - Processing now classifies one message per prompt (effective batch size is always `1`) for better quality.
   - Use short snippets (`~240 chars`) and headers for high signal/low token cost.
   - If your local server has a 4k context, keep `LLM_INPUT_TOKEN_BUDGET` around `3000`.

4. **Category stability**
   - Reuse saved `.state/categories.json` for consistency across runs.
   - Regenerate only when mailbox mix changes significantly.

5. **Operational resilience**
   - Use low temperature for deterministic categorization.
   - Retry failed messages externally via rerun; script is idempotent enough for repeated passes.

## Notes

- Folder names are sanitized to be IMAP-safe ASCII.
- If classification is uncertain, fallback category is used.
