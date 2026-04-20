"""Kanonicalizace URL inzerátů Jobs.cz (stejná pozice = stejné ID v /rpd/ID/)."""

from __future__ import annotations

import re

RPD_RE = re.compile(r"https?://(www\.)?jobs\.cz/rpd/(\d+)", re.I)


def normalize_job_url(url: str) -> str:
    """
    Odstraní ?searchId=… a další parametry — jedna nabídka = jeden klíč v DB.
    """
    if not url:
        return ""
    m = RPD_RE.search(url)
    if m:
        return f"https://www.jobs.cz/rpd/{m.group(2)}/"
    return url.split("?")[0].rstrip("/") + "/"
