"""Ukládá diagnostiku při neúspěšném odeslání přihlášky (HTML, screenshot, meta)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from jobhunter_bot.db import JobListing

DUMP_ROOT = Path("debug_apply_failures")
MAX_HTML_CHARS = 1_800_000

# Poslední FAIL — přehled pro Cursor / @ soubory (neobsahuje tajné klíče).
CURSOR_LAST_FAILURE = Path("tools") / "last_failure_for_cursor.json"


def _folder_name(listing: JobListing) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    h = hashlib.sha256(f"{listing.title}|{listing.url}".encode("utf-8")).hexdigest()[:10]
    raw = re.sub(r"[^\w\s\-]", "", listing.title, flags=re.UNICODE)[:40]
    raw = re.sub(r"\s+", "_", raw.strip()) or "pozice"
    return f"{ts}_{raw}_{h}"


def record_apply_failure(page, listing: JobListing, reason: str) -> str | None:
    """
    Uloží screenshot, HTML a meta.json do debug_apply_failures/<složka>/.
    Vrátí relativní cestu ke složce nebo None při chybě.
    """
    try:
        DUMP_ROOT.mkdir(parents=True, exist_ok=True)
        folder = DUMP_ROOT / _folder_name(listing)
        folder.mkdir(parents=True, exist_ok=False)

        urls = []
        try:
            ctx = page.context
            for p in ctx.pages:
                try:
                    urls.append(p.url)
                except Exception:
                    urls.append("?")
        except Exception:
            urls = [page.url]

        meta = {
            "reason": reason,
            "listing_title": listing.title,
            "listing_company": listing.company,
            "listing_url": listing.url,
            "page_url": page.url,
            "context_urls": urls,
            "saved_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        (folder / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        try:
            page.screenshot(path=str(folder / "screenshot.png"), full_page=True)
        except Exception:
            pass

        try:
            html = page.content()
            if len(html) > MAX_HTML_CHARS:
                html = html[:MAX_HTML_CHARS] + "\n<!-- truncated -->\n"
            (folder / "page.html").write_text(html, encoding="utf-8", errors="replace")
        except Exception:
            (folder / "page.html").write_text("(HTML se nepodařilo uložit)", encoding="utf-8")

        publish_cursor_failure_bundle(str(folder), listing, reason)
        return str(folder)
    except Exception:
        return None


def publish_cursor_failure_bundle(folder_rel: str, listing: JobListing, reason: str) -> None:
    """Uloží krátký JSON s cestami k diagnostice — v Cursoru otevři @tools/last_failure_for_cursor.json."""
    try:
        rel = folder_rel.replace("\\", "/")
        bundle = {
            "listing_title": listing.title,
            "listing_url": listing.url,
            "listing_company": listing.company,
            "reason": reason,
            "debug_folder": rel,
            "open_in_editor": [
                f"{rel}/meta.json",
                f"{rel}/screenshot.png",
                f"{rel}/page.html",
            ],
            "hint_cs": (
                "V Cursoru: @tools/last_failure_for_cursor.json + @screenshot + @meta.json. "
                "Bot při dalším běhu zkusí Gemini self-heal, pokud máš GEMINI_API_KEY."
            ),
        }
        CURSOR_LAST_FAILURE.parent.mkdir(parents=True, exist_ok=True)
        CURSOR_LAST_FAILURE.write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
