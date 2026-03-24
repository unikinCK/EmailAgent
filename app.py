#!/usr/bin/env python3
"""IMAP mailbox organizer powered by a local LLM.

Features:
- Reads IMAP + LLM settings from environment variables.
- Never deletes messages. It uses IMAP `MOVE` and will skip messages if MOVE is unsupported.
- Two-pass strategy for large mailboxes:
  1) Scan phase builds candidate categories from a representative sample.
  2) Process phase classifies and moves messages in batches.
"""

from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()


import argparse
import dataclasses
import email
import imaplib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.header import decode_header, make_header
from pathlib import Path
from typing import Iterable


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value or ""


@dataclass
class Settings:
    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str
    imap_mailbox: str
    imap_sent_mailbox: str
    imap_tls: bool
    llm_endpoint: str
    llm_model: str
    llm_api_key: str
    llm_timeout_seconds: int
    llm_temperature: float
    llm_max_tokens: int
    state_dir: Path

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            imap_host=env("IMAP_HOST", required=True),
            imap_port=int(env("IMAP_PORT", "993")),
            imap_user=env("IMAP_USER", required=True),
            imap_password=env("IMAP_PASSWORD", required=True),
            imap_mailbox=env("IMAP_MAILBOX", "INBOX"),
            imap_sent_mailbox=env("IMAP_SENT_MAILBOX", "Sent"),
            imap_tls=env("IMAP_TLS", "true").lower() in {"1", "true", "yes", "on"},
            llm_endpoint=env("LLM_ENDPOINT", "http://127.0.0.1:1234/v1/chat/completions"),
            llm_model=env("LLM_MODEL", "local-model"),
            llm_api_key=env("LLM_API_KEY", ""),
            llm_timeout_seconds=int(env("LLM_TIMEOUT_SECONDS", "120")),
            llm_temperature=float(env("LLM_TEMPERATURE", "0.1")),
            llm_max_tokens=int(env("LLM_MAX_TOKENS", "700")),
            state_dir=Path(env("STATE_DIR", ".state")),
        )


@dataclass
class MessageSummary:
    uid: str
    subject: str
    sender: str
    date: str
    flags: tuple[str, ...]
    replied_in_sent: bool
    snippet: str


@dataclass
class CategoryPlan:
    categories: list[dict[str, str]]


class LocalLLM:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _post(self, messages: list[dict[str, str]], max_tokens: int | None = None) -> dict:
        payload = {
            "model": self.settings.llm_model,
            "temperature": self.settings.llm_temperature,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
            "messages": messages,
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"

        request = urllib.request.Request(
            self.settings.llm_endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.llm_timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"LLM request failed: HTTP {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot connect to LLM endpoint: {exc.reason}") from exc

    @staticmethod
    def _extract_content(response: dict) -> str:
        choices = response.get("choices", [])
        if not choices:
            raise RuntimeError("LLM returned no choices")
        content = choices[0].get("message", {}).get("content", "").strip()
        if not content:
            raise RuntimeError("LLM returned empty content")
        return content

    def build_categories(self, samples: list[MessageSummary], max_categories: int) -> CategoryPlan:
        sample_json = json.dumps([dataclasses.asdict(s) for s in samples], ensure_ascii=False)
        prompt = (
            "You are an email triage planner. Propose practical mailbox categories for an IMAP user. "
            f"Output JSON ONLY with schema: {{\"categories\":[{{\"name\":\"FolderName\",\"description\":\"...\",\"rule_hint\":\"...\"}}]}}. "
            f"Use at most {max_categories} categories. Avoid personal/sensitive assumptions. "
            "Prefer stable categories like Bills, Receipts, Work, Newsletters, Travel, Alerts, Personal, Vendors, Promotions, Uncategorized. "
            "Folder names must be short ASCII and safe for IMAP folder creation.\n\n"
            f"SAMPLES:\n{sample_json}"
        )
        response = self._post(
            [
                {"role": "system", "content": "Return strict JSON only."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=900,
        )
        content = self._extract_content(response)
        data = parse_json_from_text(content)
        categories = data.get("categories", [])
        if not isinstance(categories, list) or not categories:
            raise RuntimeError("LLM category plan is invalid")
        clean_categories: list[dict[str, str]] = []
        for c in categories:
            name = sanitize_folder_name(str(c.get("name", "Uncategorized")))
            desc = str(c.get("description", ""))
            rule_hint = str(c.get("rule_hint", ""))
            if not name:
                continue
            clean_categories.append({"name": name, "description": desc, "rule_hint": rule_hint})
        if not clean_categories:
            clean_categories = [{"name": "Uncategorized", "description": "Fallback", "rule_hint": "default"}]
        return CategoryPlan(categories=clean_categories)

    def classify_batch(self, messages: list[MessageSummary], categories: list[dict[str, str]]) -> dict[str, str]:
        payload = {
            "categories": categories,
            "messages": [dataclasses.asdict(m) for m in messages],
        }
        prompt = (
            "Classify each message into one category name from categories. "
            "Use provided metadata like flags and replied_in_sent as context signals, but still classify every message. "
            "Return JSON only: {\"assignments\": [{\"uid\":\"...\",\"category\":\"FolderName\",\"confidence\":0.0-1.0,\"reason\":\"short\"}]}. "
            "Always include every uid exactly once."
            f"\nINPUT:\n{json.dumps(payload, ensure_ascii=False)}"
        )
        response = self._post(
            [
                {"role": "system", "content": "Return strict JSON only."},
                {"role": "user", "content": prompt},
            ],
        )
        content = self._extract_content(response)
        data = parse_json_from_text(content)
        assignments = data.get("assignments", [])
        result: dict[str, str] = {}
        valid = {c["name"] for c in categories}
        fallback = next(iter(valid)) if valid else "Uncategorized"
        for row in assignments:
            uid = str(row.get("uid", ""))
            cat = sanitize_folder_name(str(row.get("category", fallback)))
            if uid:
                result[uid] = cat if cat in valid else fallback
        for m in messages:
            result.setdefault(m.uid, fallback)
        return result


class ImapMailbox:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.conn: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "ImapMailbox":
        if self.settings.imap_tls:
            context = ssl.create_default_context()
            self.conn = imaplib.IMAP4_SSL(self.settings.imap_host, self.settings.imap_port, ssl_context=context)
        else:
            self.conn = imaplib.IMAP4(self.settings.imap_host, self.settings.imap_port)
        self.conn.login(self.settings.imap_user, self.settings.imap_password)
        status, _ = self.conn.select(f'"{self.settings.imap_mailbox}"', readonly=False)
        if status != "OK":
            raise RuntimeError(f"Cannot select mailbox: {self.settings.imap_mailbox}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn.logout()

    def _assert_conn(self) -> imaplib.IMAP4:
        if self.conn is None:
            raise RuntimeError("IMAP not connected")
        return self.conn

    def supports_move(self) -> bool:
        conn = self._assert_conn()
        capabilities = set(conn.capabilities or [])
        return b"MOVE" in capabilities

    def list_uids(self) -> list[str]:
        conn = self._assert_conn()
        status, data = conn.uid("SEARCH", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []
        return data[0].decode("utf-8").split()

    def list_uids_by_criteria(self, criteria: str) -> list[str]:
        conn = self._assert_conn()
        status, data = conn.uid("SEARCH", None, criteria)
        if status != "OK" or not data or not data[0]:
            return []
        return data[0].decode("utf-8").split()

    def fetch_summaries(self, uids: Iterable[str], snippet_chars: int = 240) -> list[MessageSummary]:
        conn = self._assert_conn()
        result: list[MessageSummary] = []
        for uid in uids:
            status, data = conn.uid("FETCH", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)] BODY.PEEK[TEXT]<0.1024>)")
            if status != "OK" or not data:
                continue
            raw_header = b""
            raw_body = b""
            flags: tuple[str, ...] = tuple()
            for part in data:
                if isinstance(part, tuple):
                    raw = part[1]
                    if b"Subject:" in raw or b"From:" in raw or b"Date:" in raw:
                        raw_header += raw
                    else:
                        raw_body += raw
                elif isinstance(part, bytes):
                    line = part.decode("utf-8", errors="ignore")
                    match = re.search(r"FLAGS \((.*?)\)", line)
                    if match:
                        flags = tuple(flag.strip() for flag in match.group(1).split() if flag.strip())
            msg = email.message_from_bytes(raw_header or b"")
            subject = decode_mime(msg.get("Subject", ""))
            sender = decode_mime(msg.get("From", ""))
            date = decode_mime(msg.get("Date", ""))
            snippet = collapse_ws((raw_body or b"").decode(errors="ignore"))[:snippet_chars]
            result.append(
                MessageSummary(
                    uid=uid,
                    subject=subject,
                    sender=sender,
                    date=date,
                    flags=flags,
                    replied_in_sent=False,
                    snippet=snippet,
                )
            )
        return result

    def ensure_folder(self, name: str) -> None:
        conn = self._assert_conn()
        status, _ = conn.create(f'"{name}"')
        if status not in {"OK", "NO"}:  # NO usually means already exists.
            raise RuntimeError(f"Could not create folder '{name}', status={status}")

    def move_uid(self, uid: str, folder: str) -> bool:
        conn = self._assert_conn()
        status, _ = conn.uid("MOVE", uid, f'"{folder}"')
        return status == "OK"


def decode_mime(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def sanitize_folder_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", name).strip().strip(".")
    cleaned = cleaned.replace("/", "-")
    return cleaned[:60] or "Uncategorized"


def normalize_subject(subject: str) -> str:
    text = collapse_ws(subject).lower()
    while True:
        updated = re.sub(r"^(re|fw|fwd)\s*:\s*", "", text).strip()
        if updated == text:
            return text
        text = updated


def parse_json_from_text(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError("LLM output is not valid JSON")
        return json.loads(text[start : end + 1])


def pick_sample_uids(uids: list[str], sample_size: int) -> list[str]:
    if len(uids) <= sample_size:
        return uids
    step = max(1, len(uids) // sample_size)
    picked = [uids[i] for i in range(0, len(uids), step)]
    return picked[:sample_size]


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def scan_phase(settings: Settings, sample_size: int, max_categories: int) -> None:
    llm = LocalLLM(settings)
    with ImapMailbox(settings) as mailbox:
        all_uids = mailbox.list_uids()
        print(f"Found {len(all_uids)} messages in {settings.imap_mailbox}")
        sample_uids = pick_sample_uids(all_uids, sample_size)
        samples = mailbox.fetch_summaries(sample_uids)
        plan = llm.build_categories(samples, max_categories=max_categories)

    categories_file = settings.state_dir / "categories.json"
    save_json(categories_file, {"generated_at": int(time.time()), "categories": plan.categories})
    print(f"Saved category plan to: {categories_file}")
    for c in plan.categories:
        print(f"- {c['name']}: {c['description']}")


def process_phase(settings: Settings, batch_size: int, max_messages: int | None, dry_run: bool) -> None:
    categories_file = settings.state_dir / "categories.json"
    if not categories_file.exists():
        raise RuntimeError("Category plan not found. Run scan first.")
    categories = load_json(categories_file).get("categories", [])
    if not categories:
        raise RuntimeError("categories.json has no categories")

    llm = LocalLLM(settings)
    with ImapMailbox(settings) as mailbox:
        if not mailbox.supports_move():
            raise RuntimeError("Server does not advertise IMAP MOVE. Refusing to copy-delete because deletion is forbidden.")

        conn = mailbox._assert_conn()
        sent_references: set[str] = set()
        try:
            status, _ = conn.select(f'"{settings.imap_sent_mailbox}"', readonly=True)
            if status == "OK":
                sent_uids = mailbox.list_uids_by_criteria("ALL")
                for chunk_start in range(0, len(sent_uids), 200):
                    chunk = sent_uids[chunk_start : chunk_start + 200]
                    summaries = mailbox.fetch_summaries(chunk, snippet_chars=0)
                    for summary in summaries:
                        if summary.subject:
                            sent_references.add(normalize_subject(summary.subject))
            conn.select(f'"{settings.imap_mailbox}"', readonly=False)
        except Exception:
            conn.select(f'"{settings.imap_mailbox}"', readonly=False)

        all_uids = mailbox.list_uids()
        if max_messages:
            all_uids = all_uids[:max_messages]

        print(f"Processing {len(all_uids)} messages in batches of {batch_size}")
        for c in categories:
            mailbox.ensure_folder(c["name"])

        moved = 0
        skipped = 0
        for i in range(0, len(all_uids), batch_size):
            batch_uids = all_uids[i : i + batch_size]
            summaries = mailbox.fetch_summaries(batch_uids)
            for msg in summaries:
                msg.replied_in_sent = normalize_subject(msg.subject) in sent_references
            assignments = llm.classify_batch(summaries, categories)
            for msg in summaries:
                target = assignments.get(msg.uid, "Uncategorized")
                if dry_run:
                    print(f"[DRY-RUN] uid={msg.uid} -> {target} | {msg.subject[:70]}")
                    skipped += 1
                    continue
                ok = mailbox.move_uid(msg.uid, target)
                if ok:
                    moved += 1
                else:
                    skipped += 1
                    print(f"[WARN] failed to move uid={msg.uid} to {target}")
        print(f"Done. moved={moved}, skipped={skipped}, dry_run={dry_run}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Organize an IMAP mailbox using a local LLM")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Build category plan from a representative sample")
    scan.add_argument("--sample-size", type=int, default=600, help="Number of sampled messages for category planning")
    scan.add_argument("--max-categories", type=int, default=12, help="Maximum category count")

    process = sub.add_parser("process", help="Classify and move messages using categories.json")
    process.add_argument("--batch-size", type=int, default=40, help="LLM batch size")
    process.add_argument("--max-messages", type=int, default=None, help="Optional cap for safer staged runs")
    process.add_argument("--dry-run", action="store_true", help="Only print actions, do not move")

    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    settings = Settings.from_env()

    if args.command == "scan":
        scan_phase(settings, sample_size=args.sample_size, max_categories=args.max_categories)
    elif args.command == "process":
        process_phase(
            settings,
            batch_size=args.batch_size,
            max_messages=args.max_messages,
            dry_run=args.dry_run,
        )
    else:
        raise RuntimeError(f"Unknown command: {args.command}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
