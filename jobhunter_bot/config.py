from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Google AI Studio: staré ID (např. gemini-1.5-flash) vrací 404 — mapujeme na aktuální stabilní modely.
# Výchozí „pro“ kvalita (AI Studio / student často má Pro); přepiš GEMINI_MODEL=gemini-2.5-flash pro úsporu.
_GEMINI_DEFAULT_MODEL = "gemini-2.5-pro"
_GEMINI_MODEL_REPLACEMENTS: dict[str, str] = {
    "gemini-1.5-flash": _GEMINI_DEFAULT_MODEL,
    "gemini-1.5-flash-001": _GEMINI_DEFAULT_MODEL,
    "gemini-1.5-flash-8b": _GEMINI_DEFAULT_MODEL,
    "gemini-1.5-flash-8b-001": _GEMINI_DEFAULT_MODEL,
    "gemini-1.5-pro": "gemini-2.5-pro",
    "gemini-1.5-pro-001": "gemini-2.5-pro",
    "gemini-pro": _GEMINI_DEFAULT_MODEL,
}


def _resolve_gemini_model(raw: str) -> str:
    m = (raw or "").strip()
    if not m:
        return _GEMINI_DEFAULT_MODEL
    return _GEMINI_MODEL_REPLACEMENTS.get(m, m)


@dataclass(frozen=True)
class AppConfig:
    jobs_search_url: str
    cv_path: str
    gemini_api_key: str
    gemini_model: str
    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str
    imap_folder: str
    db_path: str
    storage_state_path: str
    request_timeout_seconds: int


def load_config() -> AppConfig:
    load_dotenv()
    return AppConfig(
        jobs_search_url=os.getenv("JOBS_SEARCH_URL", "").strip(),
        cv_path=os.getenv("CV_PATH", "").strip(),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_model=_resolve_gemini_model(os.getenv("GEMINI_MODEL", _GEMINI_DEFAULT_MODEL)),
        imap_host=os.getenv("IMAP_HOST", "").strip(),
        imap_port=int(os.getenv("IMAP_PORT", "993")),
        imap_user=os.getenv("IMAP_USER", "").strip(),
        imap_password=os.getenv("IMAP_PASSWORD", "").strip(),
        imap_folder=os.getenv("IMAP_FOLDER", "INBOX").strip(),
        db_path=os.getenv("DB_PATH", "jobhunter.db").strip(),
        storage_state_path=os.getenv("STORAGE_STATE_PATH", "storage-state.json").strip(),
        request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "25")),
    )
