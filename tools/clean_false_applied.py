"""
Vyčistí lokální DB od „falešných applied" — záznamů, kde bot kdysi zalogoval
„OK odesláno", ale přihláška se ve skutečnosti neuložila na Jobs.cz.

Použití:
    python -m tools.clean_false_applied               # dry-run, jen vypíše
    python -m tools.clean_false_applied --apply       # opravdu změní DB

Co dělá:
1. Stáhne aktuální historii odpovědí z Jobs.cz (přes uloženou session).
2. Projde všechny `applied` záznamy v lokální DB.
3. Záznam, který NENÍ ve skutečné historii, přeřadí na status 'failed'
   a do poznámky doplní 'sync: neni na jobs.cz'.
4. Záznamy s nesmyslným URL (ne /rpd/<id>/) označí jako 'skipped'.

Bezpečné: bez --apply neprovede žádnou změnu.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

from jobhunter_bot.config import load_config
from jobhunter_bot.jobs_history import fetch_applied_rpd_urls
from jobhunter_bot.urlnorm import normalize_job_url

_RPD_RE = re.compile(r"^https?://(?:www\.)?jobs\.cz/rpd/\d+/?$", re.I)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="skutečně zapsat změny")
    parser.add_argument(
        "--storage-state",
        default="",
        help="cesta k session (default: z .env STORAGE_STATE_PATH)",
    )
    parser.add_argument(
        "--db",
        default="",
        help="cesta k DB (default: z .env DB_PATH)",
    )
    args = parser.parse_args()

    cfg = load_config()
    storage = args.storage_state or cfg.storage_state_path
    db_path = args.db or getattr(cfg, "db_path", "jobhunter.db")

    if not Path(storage).exists():
        print(f"Chyba: session soubor neexistuje: {storage}")
        return 2
    if not Path(db_path).exists():
        print(f"Chyba: DB soubor neexistuje: {db_path}")
        return 2

    print("Načítám historii z Jobs.cz (headless)...")
    log: list[str] = []
    site_urls = fetch_applied_rpd_urls(storage, log=log)
    for line in log:
        print(f"  {line}")
    print(f"Jobs.cz historie: {len(site_urls)} odpovědí\n")

    db = sqlite3.connect(db_path)
    cur = db.cursor()
    cur.execute("SELECT id, job_url, title FROM applications WHERE status='applied'")
    rows = cur.fetchall()
    print(f"Lokální DB 'applied': {len(rows)} záznamů")

    to_fail: list[tuple[int, str, str]] = []
    to_skip: list[tuple[int, str, str]] = []

    for rid, url, title in rows:
        if not url:
            to_skip.append((rid, "", title or ""))
            continue
        if not _RPD_RE.match(url or ""):
            to_skip.append((rid, url, title or ""))
            continue
        if normalize_job_url(url) not in site_urls:
            to_fail.append((rid, url, title or ""))

    print(f"  -> FALEŠNÉ applied (není na Jobs.cz): {len(to_fail)}")
    for rid, url, title in to_fail:
        print(f"     [{rid}] {title} | {url}")
    print(f"  -> NESMYSLNÁ URL (ne /rpd/<id>/): {len(to_skip)}")
    for rid, url, title in to_skip:
        print(f"     [{rid}] {title} | {url}")

    if not args.apply:
        print("\nDRY-RUN (bez --apply): nic se nezměnilo.")
        print("Pro reálnou aplikaci pusť:")
        print("  python -m tools.clean_false_applied --apply")
        return 0

    print("\nZapisuji změny do DB...")
    cur.executemany(
        "UPDATE applications SET status='failed' WHERE id=?",
        [(rid,) for rid, _, _ in to_fail],
    )
    cur.executemany(
        "UPDATE applications SET status='skipped' WHERE id=?",
        [(rid,) for rid, _, _ in to_skip],
    )
    db.commit()
    print(f"Přeřazeno: {len(to_fail)} applied -> failed, {len(to_skip)} applied -> skipped")
    print("Hotovo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
