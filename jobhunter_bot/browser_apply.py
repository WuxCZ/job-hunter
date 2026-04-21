from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from jobhunter_bot.ai import gemini_validate_application_form
from jobhunter_bot.apply_failure_dump import record_apply_failure
from jobhunter_bot.db import JobListing


_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Stealth init-script — přepíše nejběžnější fingerprintovací stopy po Playwright / automation.
# Spouští se v každém novém dokumentu ještě před tím, než se spustí JS stránky.
_STEALTH_INIT_SCRIPT = r"""
(() => {
  // 1) navigator.webdriver -> undefined
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: () => undefined,
      configurable: true,
    });
  } catch (e) {}

  // 2) window.chrome objekt
  try {
    if (!window.chrome) {
      window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {} };
    }
  } catch (e) {}

  // 3) navigator.plugins - reálný Chrome jich má ~5, typ musí být PluginArray
  try {
    const fakePluginArray = Object.create(PluginArray.prototype);
    const fakePlugins = [
      { name: 'PDF Viewer', description: 'Portable Document Format', filename: 'internal-pdf-viewer' },
      { name: 'Chrome PDF Viewer', description: '', filename: 'internal-pdf-viewer' },
      { name: 'Chromium PDF Viewer', description: '', filename: 'internal-pdf-viewer' },
      { name: 'Microsoft Edge PDF Viewer', description: '', filename: 'internal-pdf-viewer' },
      { name: 'WebKit built-in PDF', description: '', filename: 'internal-pdf-viewer' },
    ];
    fakePlugins.forEach((pl, idx) => {
      Object.setPrototypeOf(pl, Plugin.prototype);
      Object.defineProperty(fakePluginArray, idx, { value: pl, enumerable: true });
      Object.defineProperty(fakePluginArray, pl.name, { value: pl });
    });
    Object.defineProperty(fakePluginArray, 'length', { value: fakePlugins.length });
    Object.defineProperty(Navigator.prototype, 'plugins', {
      get: () => fakePluginArray,
      configurable: true,
    });
  } catch (e) {}

  // 4) navigator.languages
  try {
    Object.defineProperty(Navigator.prototype, 'languages', {
      get: () => ['cs-CZ', 'cs', 'en-US', 'en'],
      configurable: true,
    });
  } catch (e) {}

  // 5) Permissions API - Notification.permission
  try {
    const origQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
    window.navigator.permissions.query = (parameters) =>
      parameters && parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : origQuery(parameters);
  } catch (e) {}

  // 6) WebGL vendor/renderer - nebýt headless / swiftshader
  try {
    const origGetParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
      // UNMASKED_VENDOR_WEBGL = 37445, UNMASKED_RENDERER_WEBGL = 37446
      if (parameter === 37445) return 'Intel Inc.';
      if (parameter === 37446) return 'Intel Iris OpenGL Engine';
      return origGetParameter.apply(this, [parameter]);
    };
  } catch (e) {}

  // 7) navigator.hardwareConcurrency / deviceMemory
  try {
    Object.defineProperty(Navigator.prototype, 'hardwareConcurrency', {
      get: () => 8, configurable: true,
    });
    Object.defineProperty(Navigator.prototype, 'deviceMemory', {
      get: () => 8, configurable: true,
    });
  } catch (e) {}

  // 8) Console warning - některé detekční knihovny testují že console.debug existuje
  try {
    if (typeof window.console.debug !== 'function') {
      window.console.debug = function() {};
    }
  } catch (e) {}
})();
"""


def _launch_chromium(p, *, slow_mo_ms: int = 0, headless: bool = False):
    """
    Stealth-friendly Chromium:
    - Vypne automation flagy, které jobs.cz a spol. detekují
    - Reálný UA, realistické argy
    - Stealth init-script v každém kontextu (viz apply_new_context_with_stealth).
    """
    args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process,AutomationControlled",
        "--disable-infobars",
        "--no-default-browser-check",
        "--no-first-run",
        "--disable-dev-shm-usage",
    ]
    kw: dict = {
        "headless": headless,
        "args": args,
        "ignore_default_args": ["--enable-automation"],
    }
    if slow_mo_ms > 0:
        kw["slow_mo"] = slow_mo_ms
    return p.chromium.launch(**kw)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _find_chrome_exe() -> Path | None:
    """Systémový Chrome nebo Edge (Windows) — pro CDP režim „nechat okno otevřené“."""
    if sys.platform == "win32":
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        for rel in (
            Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ):
            if rel.is_file():
                return rel
        for cmd in ("chrome.exe", "chrome", "msedge.exe", "msedge"):
            p = shutil.which(cmd)
            if p:
                pp = Path(p)
                if pp.is_file():
                    return pp
        return None
    for p in (
        Path("/usr/bin/google-chrome-stable"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ):
        if p.is_file():
            return p
    return None


def _spawn_chrome_cdp(chrome_exe: Path, port: int, user_data_dir: Path) -> subprocess.Popen:
    args = [
        str(chrome_exe),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={str(user_data_dir)}",
        "--no-first-run",
        "--disable-popup-blocking",
        "--disable-extensions",
    ]
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        kwargs["creationflags"] = CREATE_BREAKAWAY_FROM_JOB | detached
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


def _wait_cdp_ready(port: int, timeout: float = 75.0) -> bool:
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.getcode() == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.35)
    return False


def _apply_stealth_to_context(context) -> None:
    """Do každého nového dokumentu vloží anti-detekční init script."""
    try:
        context.add_init_script(_STEALTH_INIT_SCRIPT)
    except PlaywrightError:
        pass


def init_session(storage_state_path: str) -> None:
    with sync_playwright() as p:
        browser = _launch_chromium(p, slow_mo_ms=0)
        context = browser.new_context(
            user_agent=_CHROME_UA,
            locale="cs-CZ",
            timezone_id="Europe/Prague",
            viewport={"width": 1366, "height": 900},
        )
        _apply_stealth_to_context(context)
        page = context.new_page()
        page.goto("https://www.jobs.cz", wait_until="domcontentloaded")
        print("Přihlas se do Jobs.cz a po dokončení stiskni Enter v terminálu.")
        try:
            input()
        except EOFError:
            try:
                page.wait_for_timeout(90000)
            except PlaywrightError:
                pass
        context.storage_state(path=storage_state_path)
        browser.close()


def _resolve_page_after_apply_click(context, page) -> object:
    """
    Po „Odpovědět“ na Jobs.cz často přesměruje na firemní microsite (např. eon.jobs.cz, Alma Career)
    v novém tabu nebo ve stejném okně — počkáme na formulář.
    """
    before = len(context.pages)
    deadline = time.monotonic() + 50.0
    while time.monotonic() < deadline:
        if len(context.pages) > before:
            np = context.pages[-1]
            try:
                np.wait_for_load_state("domcontentloaded", timeout=60000)
            except PlaywrightError:
                pass
            try:
                np.bring_to_front()
            except PlaywrightError:
                pass
            return np
        try:
            if page.locator("textarea, [contenteditable='true'], input[type='file']").count() > 0:
                return page
        except PlaywrightError:
            pass
        try:
            page.wait_for_load_state("domcontentloaded", timeout=2000)
        except PlaywrightError:
            pass
        page.wait_for_timeout(450)
    return page


def _human_click(el, timeout_ms: int = 8000) -> bool:
    """
    Humanizovaný klik: scroll do viewportu, mouse hover, krátká pauza, klik.
    Pomáhá proti bot-detekčním skriptům, které testují event.isTrusted /
    mousemove patterny.
    """
    import random

    try:
        el.scroll_into_view_if_needed(timeout=3000)
    except PlaywrightError:
        pass
    try:
        el.hover(timeout=2000)
    except PlaywrightError:
        pass
    # Krátká lidská pauza před kliknutím (150-450 ms)
    try:
        import time as _t
        _t.sleep(random.uniform(0.15, 0.45))
    except Exception:
        pass
    try:
        el.click(timeout=timeout_ms)
        return True
    except (PlaywrightError, PlaywrightTimeoutError):
        return False


def _click_first_visible(locator, timeout_ms: int = 8000) -> bool:
    try:
        n = locator.count()
        for i in range(min(n, 12)):
            el = locator.nth(i)
            if el.is_visible():
                if _human_click(el, timeout_ms):
                    return True
    except PlaywrightTimeoutError:
        return False
    return False


def _dismiss_cookie_banners(page) -> None:
    """LMC cookie lišta často překrývá CTA „Odpovědět“ na microsite."""
    for pat in (
        re.compile(r"^Souhlasím$", re.I),
        re.compile(r"Souhlasím se vším", re.I),
        re.compile(r"Přijmout vše", re.I),
        re.compile(r"^Accept all$", re.I),
    ):
        try:
            b = page.get_by_role("button", name=pat)
            if b.count() > 0 and b.first.is_visible(timeout=1200):
                b.first.click(timeout=5000)
                try:
                    page.wait_for_timeout(700)
                except PlaywrightError:
                    pass
                return
        except PlaywrightError:
            continue
    try:
        alt = page.locator("#cc-main button, .cc_div button").first
        if alt.count() > 0 and alt.is_visible(timeout=600):
            alt.click(timeout=4000)
            page.wait_for_timeout(500)
    except PlaywrightError:
        pass


def _click_apply_locator_scroll(loc, timeout_ms: int = 15000) -> bool:
    try:
        n = loc.count()
    except PlaywrightError:
        return False
    for i in range(min(n, 18)):
        el = loc.nth(i)
        try:
            el.scroll_into_view_if_needed(timeout=5000)
        except PlaywrightError:
            pass
        try:
            if el.is_visible(timeout=3000):
                el.click(timeout=timeout_ms)
                return True
        except PlaywrightError:
            continue
    return False


def _try_click_apply_entry(page) -> bool:
    """
    Firemní microsites (např. wistron.jobs.cz) mají „Odpovědět“ v <span> uvnitř .cp-button —
    čistý get_by_role(name=Odpovědět) často nic nenajde.
    """
    _dismiss_cookie_banners(page)
    try:
        page.wait_for_load_state("load", timeout=30000)
    except PlaywrightError:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=22000)
    except PlaywrightTimeoutError:
        pass
    try:
        page.wait_for_timeout(1600)
    except PlaywrightError:
        pass

    locators = [
        page.locator("button:has-text('Odpovědět')"),
        page.locator("a:has-text('Odpovědět')"),
        page.locator("button.cp-button--submit"),
        page.locator(".cp-button__wrapper button.cp-button--submit"),
        page.locator("a[href*='odpovedni-formular']"),
        page.get_by_role("button", name=re.compile(r"Odpovědět|odpovědět", re.I)),
        page.get_by_role("link", name=re.compile(r"Odpovědět|odpovědět", re.I)),
        page.locator("a, button").filter(has_text=re.compile(r"^Odpovědět$", re.I)),
        page.get_by_role("link", name=re.compile(r"Mám zájem|Reagovat", re.I)),
    ]
    for loc in locators:
        if _click_apply_locator_scroll(loc, 15000):
            return True
    return False


def _switch_to_own_file_upload_frame(fr, page) -> None:
    # 1) Jobs.cz /jof/: přímý radio button userProfileAttached=custom (důležité —
    #    pokud zůstane na 'jobs' a my uploadneme custom CV, server odmítne jako konflikt).
    for sel in (
        "input[type='radio'][name='jobad_application[userProfileAttached]'][value='custom']",
        "input[type='radio'][name*='userProfileAttached' i][value='custom']",
        "input[type='radio'][value='custom'][name*='attached' i]",
    ):
        try:
            radio = fr.locator(sel)
            if radio.count() > 0:
                try:
                    radio.first.check(timeout=3000)
                except PlaywrightError:
                    try:
                        radio.first.check(timeout=3000, force=True)
                    except PlaywrightError:
                        try:
                            radio.first.evaluate(
                                "(el) => { el.checked = true;"
                                " el.dispatchEvent(new Event('input', { bubbles: true }));"
                                " el.dispatchEvent(new Event('change', { bubbles: true })); }"
                            )
                        except PlaywrightError:
                            pass
                page.wait_for_timeout(400)
                break
        except PlaywrightError:
            continue

    # 2) Fallback: textové tlačítko „Nahrát vlastní životopis" apod.
    patterns = [
        r"Vlastní\s+životopis",
        r"Nahrát\s+vlastní",
        r"Soubor\s+z\s+počítače",
        r"Nahrát\s+ze\s+zařízení",
        r"Nahrát\s+životopis",
        r"Nahrát\s+soubor",
        r"Nahrát\s+CV",
        r"Vlastní\s+soubor",
        r"Nahrát\s+PDF",
    ]
    for pat in patterns:
        loc = fr.get_by_text(re.compile(pat, re.I))
        if loc.count() > 0:
            try:
                loc.first.click(timeout=5000)
                page.wait_for_timeout(450)
            except PlaywrightError:
                pass
    for name in ("Vlastní", "Nahrát soubor", "Ze zařízení", "Soubor"):
        try:
            radio = fr.get_by_role("radio", name=re.compile(name, re.I))
            if radio.count() > 0:
                radio.first.click(timeout=4000)
                page.wait_for_timeout(350)
        except PlaywrightError:
            continue


def _switch_to_own_file_upload(page) -> None:
    """Jobs.cz často předvybere životopis z účtu — přepneme na nahrání vlastního PDF (všechny framey)."""
    for fr in page.frames:
        _switch_to_own_file_upload_frame(fr, page)


def _set_cv_pdf_in_frame(frame, cv_file: Path) -> bool:
    pdf_inputs = frame.locator(
        "input[type='file'][accept*='pdf'], input[type='file'][accept*='PDF'], "
        "input[type='file'][accept*='application/pdf']"
    )
    for i in range(min(pdf_inputs.count(), 6)):
        inp = pdf_inputs.nth(i)
        try:
            inp.set_input_files(str(cv_file))
            return True
        except PlaywrightError:
            continue

    block = frame.locator("div, section, form").filter(
        has_text=re.compile(r"životopis|Životopis|Curriculum|CV\s+z|resume|upload", re.I)
    )
    if block.count() > 0:
        inner = block.first.locator("input[type='file']")
        for i in range(min(inner.count(), 4)):
            try:
                inner.nth(i).set_input_files(str(cv_file))
                return True
            except PlaywrightError:
                continue

    all_inputs = frame.locator("input[type='file']")
    for i in range(all_inputs.count()):
        try:
            all_inputs.nth(i).set_input_files(str(cv_file))
            return True
        except PlaywrightError:
            continue
    return False


def _set_cv_pdf_file(page, cv_file: Path) -> bool:
    """Nahraje PDF v hlavním frame i v iframe (Alma Career / Teamio)."""
    for fr in page.frames:
        if _set_cv_pdf_in_frame(fr, cv_file):
            try:
                page.wait_for_timeout(400)
            except PlaywrightError:
                pass
            return True
    return False


def _split_full_name(full: str) -> tuple[str, str]:
    full = (full or "").strip()
    if not full:
        return "", ""
    parts = full.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _human_type(loc, value: str, *, per_char_min_ms: int = 35, per_char_max_ms: int = 115) -> bool:
    """
    Napodobí lidské psaní: klikne, smaže, pak píše písmenko po písmenku s náhodným delay.
    Mnohem méně detekovatelné než locator.fill(), který hodí hodnotu najednou.
    """
    import random

    try:
        loc.click(timeout=3000)
    except PlaywrightError:
        return False
    try:
        # vyber vše a smaž (safeguard, když je pole předvyplněné)
        loc.press("Control+A", timeout=1500)
        loc.press("Delete", timeout=1500)
    except PlaywrightError:
        pass
    try:
        delay = random.randint(per_char_min_ms, per_char_max_ms)
        loc.type(value, delay=delay, timeout=max(10_000, len(value) * per_char_max_ms + 2000))
        return True
    except PlaywrightError:
        # fallback — instant fill, pokud human typing neprojde
        try:
            loc.fill(value, timeout=8000)
            return True
        except PlaywrightError:
            return False


def _fill_visible_input(loc, value: str, *, force: bool = False, humanize: bool = False) -> bool:
    """
    Vyplní pole. Pokud force=False, prázdné pole jen doplní (nepřepíše účtem předvyplněný text).
    U kontaktu z profilu používáme force=True, aby se špatný předvypl z Jobs.cz / microsite přepsal.
    Pokud humanize=True, píše se po písmenku s náhodným delay (méně detekovatelné).

    Defense in depth: odmítne honeypot pole (hp_field_input apod.).
    """
    if not value:
        return False
    try:
        if _is_honeypot_field(loc):
            return False
    except PlaywrightError:
        pass
    try:
        if not loc.is_visible(timeout=1200):
            return False
    except PlaywrightError:
        return False
    if not force:
        try:
            cur = loc.input_value(timeout=3000)
        except PlaywrightError:
            cur = ""
        if (cur or "").strip():
            return True
    if humanize:
        if _human_type(loc, value):
            return True
    try:
        loc.click(timeout=2000)
        loc.fill(value, timeout=10000)
        return True
    except PlaywrightError:
        return False


def _try_by_label(frame, value: str, patterns: tuple[str, ...], *, humanize: bool = False) -> bool:
    """Labely typu 'Jméno *', 'Jméno:', 'Jméno' — matchuje bez striktního ^$."""
    if not value:
        return False
    for pat in patterns:
        try:
            loc = frame.get_by_label(re.compile(pat, re.I))
            if loc.count() > 0 and _fill_visible_input(loc.first, value, force=True, humanize=humanize):
                return True
        except PlaywrightError:
            continue
    return False


def _try_by_role_textbox(frame, value: str, patterns: tuple[str, ...], *, humanize: bool = False) -> bool:
    """ARIA přístupné jméno (aria-label / label) přes role=textbox."""
    if not value:
        return False
    for pat in patterns:
        try:
            loc = frame.get_by_role("textbox", name=re.compile(pat, re.I))
            if loc.count() > 0 and _fill_visible_input(loc.first, value, force=True, humanize=humanize):
                return True
        except PlaywrightError:
            continue
    return False


def _try_by_selector(frame, value: str, selectors: tuple[str, ...], *, humanize: bool = False) -> bool:
    if not value:
        return False
    for sel in selectors:
        try:
            fl = frame.locator(sel)
            if fl.count() > 0 and _fill_visible_input(fl.first, value, force=True, humanize=humanize):
                return True
        except PlaywrightError:
            continue
    return False


def _fill_contact_in_frame(frame, first: str, last: str, email: str, phone: str) -> None:
    """Alma Career / Teamio / firemní weby: Jméno, Příjmení, E-mail, Telefon."""
    full = f"{first} {last}".strip()

    # --- NEJDŘÍV kombinované pole „Jméno a příjmení“ (Jobs.cz Teamio bývá tohle) ---
    combined_patterns = (
        r"Jméno\s*a\s*příjmení",
        r"Celé\s*jméno",
        r"Vaše\s*jméno",
        r"Kontaktní\s*osoba",
        r"Full\s*name",
        r"^Name\b",
    )
    combined_selectors = (
        "input[name='fullName']",
        "input[name='FullName']",
        "input[name='name']",
        "input[autocomplete='name']",
        "input[placeholder*='jméno a příjmení' i]",
        "input[placeholder*='celé jméno' i]",
        "input[placeholder*='full name' i]",
    )
    combined_filled = False
    if full:
        combined_filled = (
            _try_by_label(frame, full, combined_patterns, humanize=True)
            or _try_by_role_textbox(frame, full, combined_patterns, humanize=True)
            or _try_by_selector(frame, full, combined_selectors, humanize=True)
        )

    # --- Pokud kombinované pole nenalezeno, zkus dvě samostatná ---
    if not combined_filled:
        first_patterns = (
            r"^\s*Jméno\s*[:*]?\s*$",
            r"Křestní\s*jméno",
            r"First\s*name",
            r"Forename",
            r"Given\s*name",
        )
        first_selectors = (
            "input[name='firstName']",
            "input[name='FirstName']",
            "input[name='first_name']",
            "input[id*='firstName' i], input[id*='FirstName' i], input[id*='first_name' i]",
            "input[autocomplete='given-name']",
            "input[placeholder*='křestní' i]",
            "input[placeholder*='first name' i]",
            "input[placeholder*='jméno' i]:not([placeholder*='příjmení' i]):not([placeholder*='uživatel' i])",
        )
        _try_by_label(frame, first, first_patterns, humanize=True) \
            or _try_by_role_textbox(frame, first, first_patterns, humanize=True) \
            or _try_by_selector(frame, first, first_selectors, humanize=True)

        last_patterns = (
            r"^\s*Příjmení\s*[:*]?\s*$",
            r"Last\s*name",
            r"Surname",
            r"Family\s*name",
        )
        last_selectors = (
            "input[name='lastName']",
            "input[name='LastName']",
            "input[name='last_name']",
            "input[name='surname']",
            "input[id*='lastName' i], input[id*='LastName' i], input[id*='last_name' i], input[id*='surname' i]",
            "input[autocomplete='family-name']",
            "input[placeholder*='příjmení' i]",
            "input[placeholder*='surname' i]",
            "input[placeholder*='last name' i]",
        )
        _try_by_label(frame, last, last_patterns, humanize=True) \
            or _try_by_role_textbox(frame, last, last_patterns, humanize=True) \
            or _try_by_selector(frame, last, last_selectors, humanize=True)

    # --- E-mail ---
    email_patterns = (
        r"E-?mail",
        r"E-?mailová\s*adresa",
        r"Váš\s*e-?mail",
    )
    email_selectors = (
        "input[type='email']",
        "input[autocomplete='email']",
        "input[name*='email' i]",
        "input[id*='email' i]",
        "input[placeholder*='email' i]",
        "input[placeholder*='e-mail' i]",
    )
    _try_by_label(frame, email, email_patterns, humanize=True) \
        or _try_by_role_textbox(frame, email, email_patterns, humanize=True) \
        or _try_by_selector(frame, email, email_selectors, humanize=True)

    # --- Telefon ---
    phone_patterns = (
        r"Telefon",
        r"Mobil",
        r"Mobile",
        r"^\s*Phone\s*$",
        r"Tel\.?",
        r"Telefonní\s*číslo",
        r"Kontaktní\s*telefon",
    )
    phone_selectors = (
        "input[type='tel']",
        "input[autocomplete='tel']",
        "input[name*='phone' i]",
        "input[name*='mobile' i]",
        "input[name*='telefon' i]",
        "input[id*='phone' i]",
        "input[id*='mobile' i]",
        "input[id*='telefon' i]",
        "input[placeholder*='telefon' i]",
        "input[placeholder*='phone' i]",
    )
    _try_by_label(frame, phone, phone_patterns, humanize=True) \
        or _try_by_role_textbox(frame, phone, phone_patterns, humanize=True) \
        or _try_by_selector(frame, phone, phone_selectors, humanize=True)


def _fill_applicant_contact_fields(page, full_name: str, email: str, phone: str) -> None:
    first, last = _split_full_name(full_name)
    for fr in page.frames:
        _fill_contact_in_frame(fr, first, last, email, phone)


def _fill_salary_in_frame(frame, salary: str) -> bool:
    """
    Vyplní mzdové očekávání, pokud formulář má odpovídající pole.
    'salary' je prostá číselná hodnota (např. '50000'). Funkce se pokusí o label /
    aria / name / placeholder a u select polí hledá volbu obsahující číslo.
    """
    if not salary:
        return False
    value = str(salary).strip()
    if not value:
        return False

    patterns = (
        r"Mzdov[áé]\s*o[čc]ek[áa]v[áa]n[íi]",
        r"O[čc]ek[áa]van[áý]\s*plat",
        r"Po[žz]adovan[áý]\s*plat",
        r"Požadovaná\s*mzda",
        r"Hrub[áý]\s*m[ěe]s[íi][čc]n[íi]\s*mzda",
        r"Plat(?:ov[éý])?\s*o[čc]ek[áa]v[áa]n[íi]",
        r"^\s*Plat\s*[:*]?\s*$",
        r"^\s*Mzda\s*[:*]?\s*$",
        r"Salary\s*expect",
        r"Expected\s*salary",
        r"Desired\s*salary",
        r"^\s*Salary\s*[:*]?\s*$",
    )
    selectors = (
        "input[name*='salary' i]",
        "input[name*='plat' i]",
        "input[name*='mzda' i]",
        "input[name*='Salary']",
        "input[id*='salary' i]",
        "input[id*='plat' i]",
        "input[id*='mzda' i]",
        "input[placeholder*='plat' i]",
        "input[placeholder*='mzda' i]",
        "input[placeholder*='salary' i]",
        "input[autocomplete*='salary' i]",
    )

    filled = (
        _try_by_label(frame, value, patterns)
        or _try_by_role_textbox(frame, value, patterns)
        or _try_by_selector(frame, value, selectors)
    )

    # Select / dropdown s rozsahy (např. „30000 - 50000 Kč")
    if not filled:
        for pat in patterns:
            try:
                sel = frame.get_by_label(re.compile(pat, re.I))
                if sel.count() == 0:
                    continue
                el = sel.first
                tag = (el.evaluate("n => n.tagName") or "").lower()
                if tag != "select":
                    continue
                options = el.evaluate(
                    "s => Array.from(s.options).map(o => ({ value: o.value, text: o.textContent || '' }))"
                )
                salary_num = int(re.sub(r"\D", "", value) or "0")
                best = None
                for opt in options or []:
                    digits = re.findall(r"\d+", (opt.get("text") or "") + " " + (opt.get("value") or ""))
                    if not digits:
                        continue
                    nums = [int(d) for d in digits if len(d) >= 4]
                    if not nums:
                        continue
                    lo = min(nums)
                    hi = max(nums)
                    if lo <= salary_num <= hi:
                        best = opt
                        break
                    if best is None and lo >= salary_num:
                        best = opt
                if best:
                    el.select_option(value=best.get("value"))
                    filled = True
                    break
            except PlaywrightError:
                continue

    return filled


def _fill_applicant_salary(page, salary: str) -> None:
    if not salary:
        return
    for fr in page.frames:
        try:
            if _fill_salary_in_frame(fr, salary):
                return
        except PlaywrightError:
            continue


def _fill_message_in_frame(frame, page, message: str) -> bool:
    import random

    selectors = [
        "textarea[name='message']",
        "textarea[name='coverLetter']",
        "textarea",
        "[contenteditable='true']",
        "div[role='textbox']",
    ]
    for selector in selectors:
        loc = frame.locator(selector)
        for i in range(min(loc.count(), 12)):
            el = loc.nth(i)
            try:
                if _is_honeypot_field(el):
                    continue
            except PlaywrightError:
                pass
            try:
                if not el.is_visible(timeout=800):
                    continue
            except PlaywrightError:
                continue
            try:
                el.click(timeout=3000)
                # Smaž existující obsah
                try:
                    el.press("Control+A", timeout=1500)
                    el.press("Delete", timeout=1500)
                except PlaywrightError:
                    pass
                # Humanizované psaní - po písmenku s náhodným delay 20-70ms
                delay = random.randint(20, 70)
                page.keyboard.type(message, delay=delay)
                return True
            except PlaywrightError:
                try:
                    el.fill(message, timeout=12000)
                    return True
                except PlaywrightError:
                    continue
    try:
        tb = frame.get_by_role("textbox")
        if tb.count() > 0:
            el = tb.first
            if el.is_visible(timeout=1200) and not _is_honeypot_field(el):
                el.click(timeout=2000)
                delay = random.randint(20, 70)
                page.keyboard.type(message, delay=delay)
                return True
    except PlaywrightError:
        pass
    return False


def _fill_message(page, message: str) -> bool:
    for fr in page.frames:
        if _fill_message_in_frame(fr, page, message):
            return True
    return False


_HONEYPOT_NAME_PATTERNS = re.compile(
    r"(^|[_\-\[])(hp|honeypot|hpot|website|url|bot|trap|spam|fax|dont[_\-]?fill|leave[_\-]?blank)"
    r"([_\-\]]|$)",
    re.I,
)


def _is_honeypot_field(element) -> bool:
    """
    Jobs.cz (a další) přidávají skrytá 'honeypot' pole — musí zůstat prázdná/nedotčená,
    jinak server označí submission jako bota a odmítne ji.
    Heuristika: jméno/id/třída elementu NEBO předka obsahuje 'hp_', 'honeypot',
    'accessibility-hidden', 'visually-hidden', nebo je element skrytý.
    """
    try:
        info = element.evaluate(
            "(el) => {"
            " const s = window.getComputedStyle(el);"
            " const r = el.getBoundingClientRect();"
            " const classesChain = [];"
            " const attrsChain = [];"
            " let node = el;"
            " let depth = 0;"
            " while (node && depth < 6) {"
            "   classesChain.push(node.getAttribute ? (node.getAttribute('class') || '') : '');"
            "   attrsChain.push(node.getAttribute ? (node.getAttribute('aria-hidden') || '') : '');"
            "   node = node.parentElement; depth++;"
            " }"
            " return {"
            "   name: (el.getAttribute('name') || ''),"
            "   id: (el.getAttribute('id') || ''),"
            "   classChain: classesChain.join(' '),"
            "   ariaHiddenChain: attrsChain.join(' '),"
            "   display: s.display,"
            "   visibility: s.visibility,"
            "   opacity: s.opacity,"
            "   width: r.width,"
            "   height: r.height,"
            "   offTop: el.offsetTop,"
            "   offLeft: el.offsetLeft,"
            " };"
            "}"
        ) or {}
    except PlaywrightError:
        return False

    name_blob = " ".join([str(info.get("name", "")), str(info.get("id", ""))])
    class_blob = str(info.get("classChain", ""))
    if _HONEYPOT_NAME_PATTERNS.search(name_blob):
        return True
    if re.search(
        r"accessibility-hidden|visually-hidden|sr-only|screen-reader|honeypot|is-hidden|hp-field",
        class_blob,
        re.I,
    ):
        return True
    # aria-hidden=true na elementu nebo předku
    if re.search(r"\btrue\b", str(info.get("ariaHiddenChain", "")), re.I):
        return True
    if (info.get("display") or "") == "none":
        return True
    if (info.get("visibility") or "") == "hidden":
        return True
    try:
        if float(info.get("opacity") or 1) == 0.0:
            return True
    except (TypeError, ValueError):
        pass
    if (info.get("width") or 0) == 0 and (info.get("height") or 0) == 0:
        return True
    if int(info.get("offTop") or 0) < -500 or int(info.get("offLeft") or 0) < -500:
        return True

    return False


def _try_check_checkbox(cb) -> bool:
    """
    Jobs.cz / Alma Career mají nativní <input type=checkbox> často schovaný
    (opacity:0 / display:none) a klikatelný je jen <label>. Neopíráme se o is_visible.

    KRITICKÉ: Odmítáme honeypoty (hp_field_checkbox apod.) — jejich zaškrtnutí
    způsobí server-side odmítnutí „We ran into a problem submitting the form".
    """
    try:
        if _is_honeypot_field(cb):
            return False
    except PlaywrightError:
        pass
    try:
        if cb.is_checked():
            return True
    except PlaywrightError:
        pass
    for attempt in (
        {"timeout": 2500, "force": False},
        {"timeout": 2500, "force": True},
    ):
        try:
            cb.check(**attempt)
            return True
        except PlaywrightError:
            continue
    try:
        cb.evaluate(
            "(el) => { if (!el.checked) { el.checked = true;"
            " el.dispatchEvent(new Event('input', { bubbles: true }));"
            " el.dispatchEvent(new Event('change', { bubbles: true })); } }"
        )
        return True
    except PlaywrightError:
        return False


def _check_application_consents(page) -> None:
    """Zaškrtne souhlasy (Jobs.cz, Alma Career i firemní microsite, vč. customly styled checkboxů)."""
    for fr in page.frames:
        # 1) Klikatelné labely typu „Souhlasím, aby mi…"
        label_patterns = (
            r"Souhlas(ím|uji)",
            r"Odesláním (odpovědi )?souhlas",
            r"Beru na vědomí",
            r"Agree",
            r"I consent",
            r"I agree",
        )
        for pat in label_patterns:
            try:
                loc = fr.get_by_label(re.compile(pat, re.I))
                for i in range(min(loc.count(), 10)):
                    _try_check_checkbox(loc.nth(i))
            except PlaywrightError:
                pass

        # 2) Sekce „Souhlasy / GDPR / Ochrana osobních údajů"
        for header_text in ("Souhlasy", "souhlas", "Ochrana osobních údajů", "GDPR"):
            try:
                loc = fr.locator(
                    f"section:has-text('{header_text}'), "
                    f"div:has-text('{header_text}'), "
                    f"fieldset:has-text('{header_text}')"
                )
                for i in range(min(loc.count(), 6)):
                    box = loc.nth(i).locator("input[type='checkbox']")
                    for j in range(min(box.count(), 8)):
                        _try_check_checkbox(box.nth(j))
            except PlaywrightError:
                continue

        # 3) Všechny checkboxy ve formuláři, kromě honeypot kontejnerů Alma Career
        form_boxes = fr.locator(
            "form input[type='checkbox']:not(.accessibility-hidden *):not(.visually-hidden *)"
        )
        for i in range(min(form_boxes.count(), 16)):
            _try_check_checkbox(form_boxes.nth(i))

        # 4) Explicitně required / aria-required (honeypoty required nebývají, ale guard v _try_check_checkbox to jistí)
        required_boxes = fr.locator(
            "input[type='checkbox'][required], input[type='checkbox'][aria-required='true']"
        )
        for i in range(min(required_boxes.count(), 12)):
            _try_check_checkbox(required_boxes.nth(i))

        # 5) Fallback: klik na <label>, ale pouze pokud přidružený checkbox ještě NENÍ zaškrtnutý
        #    (jinak by klik na label toggl'nul dříve zaškrtnutý box do uncheck stavu)
        for pat in label_patterns:
            try:
                lbl = fr.locator("label").filter(has_text=re.compile(pat, re.I))
                for i in range(min(lbl.count(), 10)):
                    try:
                        el = lbl.nth(i)
                        if not el.is_visible(timeout=500):
                            continue
                        already = el.evaluate(
                            "(label) => {"
                            " const forId = label.getAttribute('for');"
                            " let input = forId ? document.getElementById(forId) : null;"
                            " if (!input) input = label.querySelector(\"input[type='checkbox']\");"
                            " if (!input) {"
                            "   const host = label.closest('label, [role=checkbox]');"
                            "   input = host ? host.querySelector(\"input[type='checkbox']\") : null;"
                            " }"
                            " return !!(input && input.checked);"
                            "}"
                        )
                        if already:
                            continue
                        el.click(timeout=2000)
                    except PlaywrightError:
                        continue
            except PlaywrightError:
                continue


def _submit_in_frame(frame) -> bool:
    """Jobs.cz + firemní microsites (Alma Career) — různé popisky tlačítek."""
    patterns = [
        re.compile(r"Odeslat odpověď", re.I),
        re.compile(r"Odeslat životopis", re.I),
        re.compile(r"Odeslat\s+přihlášku", re.I),
        re.compile(r"Podat\s+žádost", re.I),
        re.compile(r"^Odeslat$", re.I),
        re.compile(r"Odeslat\s+žádost", re.I),
        re.compile(r"^Submit$", re.I),
        re.compile(r"^Apply now$", re.I),
    ]
    for pat in patterns:
        try:
            btn = frame.get_by_role("button", name=pat)
            if btn.count() > 0 and _click_first_visible(btn, 12000):
                return True
        except PlaywrightError:
            continue
    for text in (
        "Odeslat odpověď",
        "odeslat odpověď",
        "Odeslat přihlášku",
        "Odeslat žádost",
    ):
        try:
            fb = frame.locator(f"button:has-text('{text}')")
            if fb.count() > 0 and _click_first_visible(fb, 12000):
                return True
        except PlaywrightError:
            continue

    try:
        submits = frame.locator('input[type="submit"], button[type="submit"]')
        for i in range(min(submits.count(), 8)):
            el = submits.nth(i)
            try:
                if not el.is_visible(timeout=600):
                    continue
                val = el.get_attribute("value") or ""
                try:
                    val += el.inner_text(timeout=500)
                except PlaywrightError:
                    pass
                if re.search(
                    r"odeslat|přihláš|žádost|submit|send|apply",
                    val,
                    re.I,
                ):
                    el.click(timeout=10000)
                    return True
            except PlaywrightError:
                continue
    except PlaywrightError:
        pass
    return False


def _submit_application(page) -> bool:
    for fr in page.frames:
        if _submit_in_frame(fr):
            return True
    return False


def _click_one_wizard_continue(page) -> bool:
    """Vícekrokový formulář — jednou kliknout Pokračovat / Další, než je vidět finální Odeslat."""
    labels = (
        r"^Pokračovat$",
        r"^Další$",
        r"^Další krok$",
        r"^Next$",
        r"^Continue$",
    )
    for lab in labels:
        for fr in page.frames:
            try:
                btn = fr.get_by_role("button", name=re.compile(lab, re.I))
                if btn.count() == 0:
                    continue
                el = btn.first
                if el.is_visible(timeout=800):
                    el.click(timeout=6000)
                    try:
                        page.wait_for_timeout(1000)
                    except PlaywrightError:
                        pass
                    return True
            except PlaywrightError:
                continue
    return False


def _submit_application_with_retries(page, rounds: int = 5) -> bool:
    """Zkusí odeslat; když není tlačítko, zkusí „Pokračovat“ a znovu (kouzelník)."""
    for r in range(rounds):
        if _submit_application(page):
            return True
        if r == rounds - 1:
            break
        if not _click_one_wizard_continue(page):
            break
        try:
            page.wait_for_timeout(900)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except PlaywrightError:
            pass
    return False


def _apply_fail(page, listing: JobListing, reason: str) -> tuple[bool, str]:
    """Uloží screenshot + HTML při FAIL, vrátí (False, důvod + cesta)."""
    path = record_apply_failure(page, listing, reason)
    if path:
        return False, f"{reason} | diagnostika: {path}"
    return False, reason


def _gather_visible_text(page) -> str:
    chunks: list[str] = []
    for fr in page.frames:
        try:
            t = fr.evaluate(
                "() => document.body ? (document.body.innerText || document.body.textContent || '') : ''"
            )
            if t and isinstance(t, str):
                chunks.append(t)
        except PlaywrightError:
            continue
    return "\n".join(chunks)


_ERROR_PATTERNS = re.compile(
    # Jobs.cz / Alma Career server-side error varianty (CZ + EN):
    r"(ran?|run)\s+into\s+(a|some|the)\s+problem|"
    r"problem\s+submitting\s+the\s+form|"
    r"problem\s+with\s+(your\s+)?(submission|submitting)|"
    r"we\s+could\s+not\s+submit|we\s+were\s+unable\s+to\s+submit|"
    r"unable\s+to\s+submit\s+(your\s+)?(form|application)|"
    r"failed\s+to\s+(submit|process)\s+(your\s+)?(form|application|request)|"
    # CZ varianty
    r"došlo\s+k\s+chyb|nastala\s+chyba|něco\s+se\s+(pokazilo|nepovedlo)|"
    r"formulář\s+se\s+nepodařilo\s+odesl|nepodařilo\s+se\s+odeslat|"
    r"(odpověď|přihlášku)\s+se\s+nepodařilo\s+odesl|"
    r"zkuste\s+to\s+(prosím\s+)?(znovu|později)|"
    # HTTP / server
    r"(chyba\s+serveru|server\s+error|500\s+internal|bad\s+gateway|503\s+service|504\s+gateway)|"
    # Obecné EN chyby
    r"something\s+went\s+wrong|(an\s+)?error\s+occurred|please\s+try\s+again",
    re.I,
)

_SUCCESS_TEXT_PATTERNS = re.compile(
    r"děkujeme|děkujeme\s+vám|děkujeme,\s*že|"
    r"vaše\s+(žádost|odpověď|přihláška|reakce)\s+(byla|byly)?\s*(úspěšně\s+)?odesl|"
    r"žádost\s+(byla\s+)?odesl|odpověď\s+byla\s+odesl|reakce\s+byla\s+(úspěšně\s+)?odesl|"
    r"životopis\s+(byl\s+)?odeslán|úspěšně\s+odesl(ána|áno|án)|"
    r"přijali\s+jsme|přihláška\s+byla\s+přijata|"
    r"thank\s*you\s+for\s+(applying|your\s+application)|"
    r"application\s+(was\s+)?(received|submitted|sent)|successfully\s+submitted|"
    r"we\s+(have\s+)?received\s+your\s+(application|response)",
    re.I,
)


def _page_shows_error(blob: str, page) -> bool:
    if _ERROR_PATTERNS.search(blob or ""):
        return True
    try:
        err_loc = page.get_by_text(_ERROR_PATTERNS)
        if err_loc.count() > 0 and err_loc.first.is_visible(timeout=1200):
            return True
    except PlaywrightError:
        pass
    try:
        alert = page.locator("[role='alert'], .alert-danger, .error-message, .has-error")
        for i in range(min(alert.count(), 6)):
            el = alert.nth(i)
            try:
                if el.is_visible(timeout=400):
                    txt = (el.inner_text(timeout=800) or "").strip()
                    if txt and _ERROR_PATTERNS.search(txt):
                        return True
            except PlaywrightError:
                continue
    except PlaywrightError:
        pass
    return False


def _submission_succeeded(page, start_url: str) -> bool:
    """
    Potvrdí úspěch jen když:
      - je vidět explicitní „děkujeme / odesláno" text, NEBO
      - URL se změnila na novou cestu, která obsahuje success/thank/dekuj/potvrzeni,
      - a zároveň stránka NEOBSAHUJE chybovou hlášku („We run into some problem", …).
    Jinak vrací False a volající zapíše FAIL (žádné falešné „OK odesláno").
    """
    try:
        page.wait_for_timeout(2600)
    except PlaywrightError:
        pass

    current = page.url.split("#")[0]
    base_start = start_url.split("?")[0].rstrip("/")
    cur_path = current.split("?")[0].rstrip("/")

    blob = _gather_visible_text(page)

    # Tvrdá kontrola chybové stránky — má přednost.
    if _page_shows_error(blob, page):
        return False

    # 1) Silná signatura v query-stringu
    try:
        qs = urlparse(current).query.lower()
        if any(
            x in qs
            for x in ("success", "sent", "odeslano", "thank", "dekuji", "děkuj", "confirmed")
        ):
            return True
    except Exception:
        pass

    # 2) URL se změnila na „confirmation" cestu
    if cur_path != base_start:
        low = cur_path.lower()
        if any(
            x in low
            for x in (
                "dekujeme",
                "děkujeme",
                "thank",
                "success",
                "potvrzeni",
                "confirmation",
                "sent",
                "odeslano",
                "done",
                "hotovo",
            )
        ):
            return True
        # URL se změnila, ale neznáme cílovou cestu → musí být potvrzovací text, jinak to
        # NENÍ signál úspěchu (mohla to být jen chybová stránka nebo cookie banner přesun).

    # 3) Potvrzovací text (nejspolehlivější signál)
    if _SUCCESS_TEXT_PATTERNS.search(blob):
        return True

    ok_text = page.get_by_text(_SUCCESS_TEXT_PATTERNS)
    try:
        if ok_text.count() > 0 and ok_text.first.is_visible(timeout=2500):
            return True
    except PlaywrightError:
        pass

    for fr in page.frames:
        try:
            if fr.get_by_role(
                "heading", name=re.compile(r"děkujeme|hotovo|thank you", re.I)
            ).count() > 0:
                return True
        except PlaywrightError:
            continue

    if re.search(
        r"odeslání\s+proběhlo|odesláno|odeslána\s+reakce|vaše\s+data\s+byla\s+odeslána",
        blob,
        re.I,
    ):
        return True

    return False


def apply_to_job(
    listing: JobListing,
    cv_path: str,
    storage_state_path: str,
    message: str,
    dry_run: bool = False,
    browser_slow_mo_ms: int = 0,
    *,
    applicant_full_name: str = "",
    applicant_email: str = "",
    applicant_phone: str = "",
    applicant_salary: str = "",
    gemini_api_key: str = "",
    gemini_model: str = "",
    info_log: list[str] | None = None,
    skip_gemini_form_check: bool = False,
    headless: bool = False,
    approval_callback=None,
    leave_browser_open_on_failure: bool = False,
) -> tuple[bool, str]:
    """
    Vrátí (True, "") při úspěchu, jinak (False, krátký důvod pro log).

    leave_browser_open_on_failure: při neúspěchu zkusí systémový Chrome/Edge s CDP,
    po skončení Playwright odpojí bez zavření oken — formulář můžeš doplnit ručně.
    Ignuje se v headless a dry-run. Když CDP nelze nastartovat, použije se běžný Chromium.
    """
    cv_file = Path(cv_path)
    if not cv_file.exists():
        raise FileNotFoundError(f"CV nebylo nalezeno: {cv_path}")
    if cv_file.suffix.lower() != ".pdf":
        raise ValueError("CV musí být ve formátu PDF.")

    if not Path(storage_state_path).exists():
        raise FileNotFoundError(
            f"Session file chybí: {storage_state_path}. Spusť login v GUI nebo `python main.py init-session`."
        )

    start_url = listing.url.split("#")[0]

    want_detach = bool(leave_browser_open_on_failure and not headless and not dry_run)
    browser = None
    context = None
    chrome_proc: subprocess.Popen | None = None
    temp_uds: Path | None = None
    use_cdp = False
    detach_requested = False
    sm = max(0, min(10_000, int(browser_slow_mo_ms)))
    pw = None

    try:
        pw = sync_playwright().start()
        if want_detach:
            chrome_exe = _find_chrome_exe()
            if chrome_exe is not None:
                cd_port = _pick_free_port()
                temp_uds = Path(tempfile.mkdtemp(prefix="jobhunter_cdp_"))
                chrome_proc = None
                try:
                    chrome_proc = _spawn_chrome_cdp(chrome_exe, cd_port, temp_uds)
                except OSError:
                    chrome_proc = None
                ok_cdp = (
                    chrome_proc is not None
                    and chrome_proc.poll() is None
                    and _wait_cdp_ready(cd_port)
                )
                if not ok_cdp:
                    if chrome_proc is not None and chrome_proc.poll() is None:
                        try:
                            chrome_proc.terminate()
                            chrome_proc.wait(timeout=12)
                        except Exception:
                            pass
                    chrome_proc = None
                    if temp_uds is not None:
                        shutil.rmtree(temp_uds, ignore_errors=True)
                        temp_uds = None
                    if info_log is not None:
                        info_log.append(
                            "Režim „ponechat prohlížeč“: CDP se nerozběhlo — používám vestavěný Chromium; okno se po běhu zavře."
                        )
                else:
                    use_cdp = True
                    if info_log is not None:
                        info_log.append(f"Režim „ponechat prohlížeč“: CDP běží přes {chrome_exe}.")
                    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cd_port}")
            elif info_log is not None:
                info_log.append(
                    "Režim „ponechat prohlížeč“: není Chrome/Edge v očekávaných cestách — používám vestavěný Chromium."
                )

        if browser is None:
            browser = _launch_chromium(pw, slow_mo_ms=sm, headless=headless)

        context = browser.new_context(
            storage_state=storage_state_path,
            user_agent=_CHROME_UA,
            locale="cs-CZ",
            timezone_id="Europe/Prague",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={
                "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.7,sk;q=0.5",
                "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            },
        )
        _apply_stealth_to_context(context)
        page = context.new_page()
        page.goto(listing.url, wait_until="load", timeout=90000)

        def _inner_apply() -> tuple[bool, str]:
            nonlocal page

            if not _try_click_apply_entry(page):
                return _apply_fail(
                    page,
                    listing,
                    "není vidět / nejde kliknout „Odpovědět“ (zkus zavřít cookies nebo zkontroluj přihlášení)",
                )

            page = _resolve_page_after_apply_click(context, page)

            try:
                page.wait_for_selector(
                    "textarea, [contenteditable='true'], input[type='file'], div[role='textbox']",
                    timeout=35000,
                )
            except PlaywrightTimeoutError:
                pass

            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1000)
            try:
                page.evaluate("window.scrollTo(0, Math.min(800, document.body.scrollHeight / 3))")
            except PlaywrightError:
                pass

            # 1) Nejdřív přepnout na „Vlastní životopis“ — na Alma Career to odkryje
            #    sekci Jméno / E-mail / Telefon / Zpráva / Souhlasy.
            _switch_to_own_file_upload(page)
            try:
                page.wait_for_timeout(650)
            except PlaywrightError:
                pass

            # 2) Vyplnit kontakt (force=True přepíše špatný předvypl z účtu).
            _fill_applicant_contact_fields(
                page, applicant_full_name, applicant_email, applicant_phone
            )

            # 3) Vyplnit zprávu / motivační text.
            _fill_message(page, message)

            # 4) Nahrát PDF.
            if not _set_cv_pdf_file(page, cv_file):
                return _apply_fail(
                    page,
                    listing,
                    "nepodařilo se nahrát PDF (žádný vhodný file input / iframe)",
                )

            try:
                page.wait_for_timeout(1200)
            except PlaywrightError:
                pass

            # 5) Záchranné druhé kolo — některé microsite odkryjí nebo přerenderují
            #    pole až po volbě souboru. Force=True znovu prosadí naše hodnoty.
            _fill_applicant_contact_fields(
                page, applicant_full_name, applicant_email, applicant_phone
            )
            _fill_message(page, message)

            # 6) Volitelné: mzdové očekávání (pokud formulář má odpovídající pole).
            if applicant_salary:
                _fill_applicant_salary(page, applicant_salary)

            _check_application_consents(page)

            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except PlaywrightError:
                pass

            ready: bool | None = None
            gemini_msg = ""
            if not skip_gemini_form_check:
                ready, gemini_msg = gemini_validate_application_form(
                    gemini_api_key, gemini_model, page
                )
                if info_log is not None and gemini_msg:
                    info_log.append(gemini_msg)
                strict = os.environ.get("FORM_VALIDATE_STRICT", "").strip().lower() in (
                    "1",
                    "true",
                    "yes",
                    "on",
                )
                if (
                    strict
                    and (gemini_api_key or "").strip()
                    and ready is False
                ):
                    return _apply_fail(
                        page,
                        listing,
                        gemini_msg or "Gemini: formulář vypadá neúplně (FORM_VALIDATE_STRICT)",
                    )

            if approval_callback is not None:
                try:
                    decision_raw = approval_callback()
                except Exception as exc:
                    return False, f"manuální schválení selhalo: {exc}"
                decision = (decision_raw or "").strip().lower()
                if decision == "stop":
                    return False, "__manual_stop__"
                if decision != "approve":
                    return True, "__manual_skip__"

            if dry_run:
                try:
                    page.wait_for_timeout(500)
                except PlaywrightError:
                    pass
                return (
                    True,
                    "dry-run: kontakt + zpráva + CV vyplněny, finální odeslání přeskočeno",
                )

            if not _submit_application_with_retries(page):
                return _apply_fail(
                    page,
                    listing,
                    "není finální odeslání (zkus Debug slow-mo; může být více kroků nebo jiný text tlačítka)",
                )

            try:
                page.wait_for_load_state("networkidle", timeout=18000)
            except PlaywrightTimeoutError:
                pass

            if not _submission_succeeded(page, start_url):
                # Rozlišíme server chybu (má smysl zkusit znovu) od „nevíme":
                try:
                    blob_after = _gather_visible_text(page)
                except Exception:
                    blob_after = ""
                if _page_shows_error(blob_after, page):
                    return _apply_fail(
                        page,
                        listing,
                        'server chyba jobs.cz (We run into some problem) - vhodne pro retry',
                    )
                # SPA / microsite někdy doplní „děkujeme“ až po dalším ticku — zkusíme znovu před FAIL.
                for extra_ms in (4000, 8000):
                    try:
                        page.wait_for_timeout(extra_ms)
                    except PlaywrightError:
                        pass
                    try:
                        page.wait_for_load_state("networkidle", timeout=12000)
                    except PlaywrightTimeoutError:
                        pass
                    if _submission_succeeded(page, start_url):
                        return True, ""
                return _apply_fail(
                    page,
                    listing,
                    'odeslani nepotvrzeno (stejna URL a nenasel jsem text "dekujeme" - zkontroluj rucne v prohlizeci)',
                )
            return True, ""

        result = _inner_apply()
        detach_requested = (
            want_detach
            and use_cdp
            and (not result[0])
            and (result[1] or "") != "__manual_stop__"
        )
        return result

    finally:
        if detach_requested:
            try:
                if browser is not None:
                    browser.disconnect()
            except Exception:
                pass
            if info_log is not None:
                info_log.append(
                    "Prohlížeč zůstává otevřený — můžeš ručně doplnit/odeslat; až skončíš, zavři Chrome (Edge)."
                )
        else:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                if chrome_proc is not None and chrome_proc.poll() is None:
                    chrome_proc.terminate()
                    chrome_proc.wait(timeout=20)
            except Exception:
                pass
            if temp_uds is not None and temp_uds.exists():
                shutil.rmtree(temp_uds, ignore_errors=True)
        try:
            if pw is not None:
                pw.stop()
        except Exception:
            pass
