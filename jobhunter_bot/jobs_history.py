from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from jobhunter_bot.scraper import DEFAULT_HEADERS
from jobhunter_bot.urlnorm import normalize_job_url

# zpětná kompatibilita
normalize_rpd_url = normalize_job_url


HISTORIE_URLS = (
    "https://www.jobs.cz/osobni/historie-odpovedi/",
    "https://www.jobs.cz/uzivatel/historie-odpovedi/",
    "https://www.jobs.cz/osobni/prehled-odpovedi/",
    "https://www.jobs.cz/osobni/odpovedi/",
)

_DEBUG_DIR = Path("debug_jobs_history")
_CHROME_UA = DEFAULT_HEADERS.get(
    "User-Agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)


def _rpd_urls_from_html(html: str) -> set[str]:
    """Vytáhne /rpd/ID z celého HTML (odkazy i JSON v __NEXT_DATA__), když selžou běžné <a>."""
    out: set[str] = set()
    if not html:
        return out
    for m in re.finditer(r"https?://(?:www\.)?jobs\.cz/rpd/(\d+)", html, re.I):
        out.add(normalize_job_url(m.group(0)))
    for m in re.finditer(r'["\'](?:https://(?:www\.)?jobs\.cz)?/rpd/(\d+)/?["\']', html, re.I):
        out.add(normalize_job_url(f"https://www.jobs.cz/rpd/{m.group(1)}/"))
    for m in re.finditer(r"/rpd/(\d+)(?:/|[\"'\s<>])", html):
        out.add(normalize_job_url(f"https://www.jobs.cz/rpd/{m.group(1)}/"))
    return out


def _rpd_urls_from_next_data(html: str) -> set[str]:
    m = re.search(
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return set()
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return set()

    blob = json.dumps(data, ensure_ascii=False)
    return _rpd_urls_from_html(blob)


def _looks_like_login(url: str, html: str) -> bool:
    u = (url or "").lower()
    if "prihlasit" in u or "/login" in u or "account.seznam.cz" in u or "auth." in u:
        return True
    sample = (html or "")[:5000].lower()
    if "přihlásit" in sample and "heslo" in sample and "e-mail" in sample:
        return True
    return False


def _save_debug_snapshot(page, tag: str) -> Path | None:
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = _DEBUG_DIR / f"{ts}_{tag}.html"
        png_path = _DEBUG_DIR / f"{ts}_{tag}.png"
        try:
            html_path.write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        try:
            page.screenshot(path=str(png_path), full_page=True)
        except Exception:
            pass
        return html_path
    except Exception:
        return None


def fetch_applied_rpd_urls(
    storage_state_path: str,
    timeout_ms: int = 45000,
    log: list[str] | None = None,
) -> set[str]:
    """
    Načte ID/URL inzerátů, na které už uživatel odpověděl (stránka Historie odpovědí).
    Vyžaduje platný storage_state po přihlášení do Jobs.cz.

    Při `log=[]` funkce doplňuje diagnostické záznamy, aby bylo jasné, proč případně vrací 0.
    """

    def _note(msg: str) -> None:
        if log is not None:
            log.append(msg)

    ssp = Path(storage_state_path)
    if not ssp.exists():
        _note(f"Session soubor neexistuje: {storage_state_path} — klikni 'Login do Jobs.cz'.")
        return set()

    try:
        size = ssp.stat().st_size
        if size < 200:
            _note(f"Session soubor {ssp} je podezřele malý ({size} B). Možná je poškozený — přelogovat.")
    except OSError:
        pass

    found: set[str] = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = browser.new_context(
                storage_state=storage_state_path,
                user_agent=_CHROME_UA,
                locale="cs-CZ",
                viewport={"width": 1365, "height": 900},
            )
            page = context.new_page()

            last_url = ""
            last_html = ""
            opened_ok = False

            for candidate in HISTORIE_URLS:
                try:
                    page.goto(candidate, wait_until="load", timeout=timeout_ms)
                except PlaywrightTimeoutError:
                    _note(f"Timeout při načítání {candidate}")
                    continue
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(1800)
                last_url = page.url
                try:
                    last_html = page.content()
                except Exception:
                    last_html = ""

                if _looks_like_login(last_url, last_html):
                    _note(
                        f"Redirect na login při {candidate} → url={last_url}. "
                        "Session je nejspíš propršelá — klikni 'Login do Jobs.cz'."
                    )
                    continue

                _note(f"Historie načtena z {candidate} → finální URL: {last_url}")
                opened_ok = True
                break

            if not opened_ok:
                snap = _save_debug_snapshot(page, "no_history_page")
                if snap:
                    _note(f"Snapshot uložen: {snap}")
                context.close()
                browser.close()
                return set()

            try:
                page.wait_for_selector(
                    'a[href*="/rpd/"], script#__NEXT_DATA__, [data-testid*="response"], '
                    '[data-testid*="answer"]',
                    timeout=15000,
                )
            except PlaywrightTimeoutError:
                pass

            for page_round in range(30):
                html = page.content()
                before = len(found)
                found |= _rpd_urls_from_html(html)
                found |= _rpd_urls_from_next_data(html)

                links = page.locator('a[href*="/rpd/"]')
                try:
                    n = links.count()
                except Exception:
                    n = 0
                for i in range(n):
                    try:
                        href = links.nth(i).get_attribute("href")
                        if href:
                            full = urljoin("https://www.jobs.cz", href)
                            found.add(normalize_job_url(full))
                    except Exception:
                        continue

                if page_round == 0:
                    _note(
                        f"První strana: nalezeno {len(found)} /rpd/ URL (a {n} <a> elementů)."
                    )

                # pokus o „další stránku"
                next_patterns = [
                    page.get_by_role("link", name=re.compile(r"další|následující|next", re.I)),
                    page.locator("a[rel='next']"),
                    page.get_by_text(re.compile(r"^\s*›\s*$")),
                    page.locator("button:has-text('Další')"),
                ]
                clicked = False
                for locator in next_patterns:
                    try:
                        if locator.count() == 0:
                            continue
                    except Exception:
                        continue
                    try:
                        loc = locator.first
                        if loc.is_visible():
                            loc.click(timeout=5000)
                            page.wait_for_timeout(1200)
                            try:
                                page.wait_for_load_state("networkidle", timeout=12000)
                            except PlaywrightTimeoutError:
                                pass
                            clicked = True
                            break
                    except Exception:
                        continue

                if not clicked:
                    # zkusit „Načíst další" / scroll (infinite)
                    try:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(900)
                    except Exception:
                        pass
                    html2 = page.content()
                    found |= _rpd_urls_from_html(html2)
                    found |= _rpd_urls_from_next_data(html2)
                    if len(found) == before:
                        break

            if not found:
                snap = _save_debug_snapshot(page, "history_empty")
                if snap:
                    _note(
                        f"Historie načtena, ale bez /rpd/ odkazů. Snapshot: {snap} — "
                        "mohlo se změnit URL / struktura stránky."
                    )

            context.close()
        finally:
            browser.close()

    return found
