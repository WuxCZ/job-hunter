from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from jobhunter_bot.urlnorm import normalize_job_url


@dataclass
class JobListing:
    title: str
    company: str
    url: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_STATUS_PRIORITY = {"applied": 4, "responded": 3, "skipped": 2, "discovered": 1}


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._init_schema()
        self._merge_duplicate_job_urls()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_url TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT,
                    status TEXT NOT NULL,
                    applied_at TEXT,
                    last_checked_at TEXT,
                    response_received INTEGER DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS email_replies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_uid TEXT UNIQUE NOT NULL,
                    sender TEXT,
                    subject TEXT,
                    received_at TEXT,
                    matched_job_url TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS apply_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT,
                    reason TEXT NOT NULL,
                    failed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_apply_failures_at ON apply_failures(failed_at DESC)"
            )

    def _merge_duplicate_job_urls(self) -> None:
        """Stejný inzerát s různými ?searchId=… sloučí na jednu kanonickou URL."""
        with self._connect() as conn:
            rows = list(
                conn.execute(
                    "SELECT id, job_url, status, title, company, applied_at, last_checked_at, response_received FROM applications"
                ).fetchall()
            )
            by_key: dict[str, list] = {}
            for r in rows:
                key = normalize_job_url(r["job_url"])
                by_key.setdefault(key, []).append(r)

            for canon, group in by_key.items():
                if len(group) == 1:
                    r = group[0]
                    if r["job_url"] != canon:
                        try:
                            conn.execute(
                                "UPDATE applications SET job_url = ? WHERE id = ?",
                                (canon, r["id"]),
                            )
                        except sqlite3.IntegrityError:
                            conn.execute("DELETE FROM applications WHERE id = ?", (r["id"],))
                    continue

                best = max(
                    group,
                    key=lambda x: _STATUS_PRIORITY.get(x["status"] or "", 0),
                )
                for r in group:
                    if r["id"] != best["id"]:
                        conn.execute("DELETE FROM applications WHERE id = ?", (r["id"],))
                try:
                    conn.execute(
                        "UPDATE applications SET job_url = ? WHERE id = ?",
                        (canon, best["id"]),
                    )
                except sqlite3.IntegrityError:
                    pass

    def upsert_listing(self, listing: JobListing) -> None:
        u = normalize_job_url(listing.url)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO applications (job_url, title, company, status, last_checked_at)
                VALUES (?, ?, ?, 'discovered', ?)
                ON CONFLICT(job_url) DO UPDATE SET
                    title=excluded.title,
                    company=excluded.company,
                    last_checked_at=excluded.last_checked_at
                """,
                (u, listing.title, listing.company, utc_now()),
            )

    def should_skip_listing(self, job_url: str) -> bool:
        """Přeskočit: už odesláno, odpověď z mailu, ručně přeskočeno, nebo duplicita."""
        u = normalize_job_url(job_url)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM applications WHERE job_url = ?",
                (u,),
            ).fetchone()
            if row is None:
                return False
            return row["status"] in {"applied", "responded", "skipped"}

    def has_been_applied(self, job_url: str) -> bool:
        """Zpětná kompatibilita – jen odeslání přes bota / označeno jako odeslané."""
        u = normalize_job_url(job_url)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM applications WHERE job_url = ?",
                (u,),
            ).fetchone()
            if row is None:
                return False
            return row["status"] in {"applied", "responded"}

    def mark_skipped(self, listing: JobListing) -> None:
        """Uživatel přeskočil / už je v historii Jobs.cz — už se znovu neukazovat (nesníží stav applied)."""
        u = normalize_job_url(listing.url)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO applications (job_url, title, company, status, last_checked_at)
                VALUES (?, ?, ?, 'skipped', ?)
                ON CONFLICT(job_url) DO UPDATE SET
                    title=excluded.title,
                    company=excluded.company,
                    status=CASE
                        WHEN applications.status IN ('applied', 'responded') THEN applications.status
                        ELSE 'skipped'
                    END,
                    last_checked_at=excluded.last_checked_at
                """,
                (u, listing.title, listing.company, utc_now()),
            )

    def mark_applied(self, listing: JobListing) -> None:
        u = normalize_job_url(listing.url)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO applications (job_url, title, company, status, applied_at, last_checked_at, response_received)
                VALUES (?, ?, ?, 'applied', ?, ?, 0)
                ON CONFLICT(job_url) DO UPDATE SET
                    title=excluded.title,
                    company=excluded.company,
                    status='applied',
                    applied_at=excluded.applied_at,
                    last_checked_at=excluded.last_checked_at
                """,
                (u, listing.title, listing.company, utc_now(), utc_now()),
            )

    def mark_responded(self, job_url: str) -> None:
        u = normalize_job_url(job_url)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE applications
                SET status='responded', response_received=1, last_checked_at=?
                WHERE job_url=?
                """,
                (utc_now(), u),
            )

    def register_reply(
        self,
        message_uid: str,
        sender: str,
        subject: str,
        received_at: str,
        matched_job_url: str | None,
    ) -> bool:
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO email_replies (message_uid, sender, subject, received_at, matched_job_url)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (message_uid, sender, subject, received_at, matched_job_url),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def get_applied_jobs(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT job_url, title, company, status
                    FROM applications
                    WHERE status IN ('applied', 'responded')
                    ORDER BY applied_at DESC
                    """
                ).fetchall()
            )

    def get_recent_applications(self, limit: int = 200) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT job_url, title, company, status, applied_at, last_checked_at
                    FROM applications
                    ORDER BY COALESCE(applied_at, last_checked_at) DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )

    def record_apply_failure(self, listing: JobListing, reason: str) -> None:
        u = normalize_job_url(listing.url)
        reason_clean = (reason or "").strip()[:8000]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO apply_failures (job_url, title, company, reason, failed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (u, listing.title, listing.company or "", reason_clean, utc_now()),
            )

    def get_recent_failures(self, limit: int = 300) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT job_url, title, company, reason, failed_at
                    FROM apply_failures
                    ORDER BY failed_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )

    def clear_apply_failures(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM apply_failures")
