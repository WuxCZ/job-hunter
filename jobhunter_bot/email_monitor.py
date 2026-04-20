from __future__ import annotations

import email
import imaplib
from email.header import decode_header
from email.utils import parsedate_to_datetime

from jobhunter_bot.db import Database


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    output: list[str] = []
    for part, encoding in parts:
        if isinstance(part, bytes):
            output.append(part.decode(encoding or "utf-8", errors="ignore"))
        else:
            output.append(part)
    return "".join(output).strip()


def poll_inbox(
    db: Database,
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str,
    limit: int = 100,
) -> int:
    if not all([host, username, password]):
        return 0

    matched = 0
    applied = db.get_applied_jobs()

    conn = imaplib.IMAP4_SSL(host=host, port=port)
    try:
        conn.login(username, password)
        conn.select(folder)
        status, data = conn.search(None, "ALL")
        if status != "OK":
            return 0
        uids = (data[0] or b"").split()[-limit:]
        for uid_raw in uids:
            uid = uid_raw.decode("utf-8", errors="ignore")
            status, msg_data = conn.fetch(uid, "(RFC822)")
            if status != "OK" or not msg_data:
                continue
            raw = msg_data[0][1]
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            subject = _decode(msg.get("Subject"))
            sender = _decode(msg.get("From"))
            date_value = msg.get("Date")
            received_at = ""
            if date_value:
                try:
                    received_at = parsedate_to_datetime(date_value).isoformat()
                except Exception:
                    received_at = ""

            matched_job_url = None
            sub = subject.lower()
            snd = sender.lower()
            for row in applied:
                title = (row["title"] or "").lower()
                company = (row["company"] or "").lower()
                if (title and title in sub) or (company and company in snd):
                    matched_job_url = row["job_url"]
                    break

            is_new = db.register_reply(uid, sender, subject, received_at, matched_job_url)
            if is_new and matched_job_url:
                db.mark_responded(matched_job_url)
                matched += 1
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    return matched
