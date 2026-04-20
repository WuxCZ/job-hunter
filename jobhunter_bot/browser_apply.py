from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from jobhunter_bot.ai import gemini_validate_application_form
from jobhunter_bot.apply_failure_dump import record_apply_failure
from jobhunter_bot.db import JobListing


def _launch_chromium(p, *, slow_mo_ms: int = 0, headless: bool = False):
    """slow_mo_ms > 0 zpomalí všechny akce Playwright (klikání, psaní, navigace) — pro ladění."""
    kw: dict = {"headless": headless}
    if slow_mo_ms > 0:
        kw["slow_mo"] = slow_mo_ms
    return p.chromium.launch(**kw)


def init_session(storage_state_path: str) -> None:
    with sync_playwright() as p:
        browser = _launch_chromium(p, slow_mo_ms=0)
        context = browser.new_context()
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


def _click_first_visible(locator, timeout_ms: int = 8000) -> bool:
    try:
        n = locator.count()
        for i in range(min(n, 12)):
            el = locator.nth(i)
            if el.is_visible():
                el.click(timeout=timeout_ms)
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


def _fill_visible_input(loc, value: str, *, force: bool = False) -> bool:
    """
    Vyplní pole. Pokud force=False, prázdné pole jen doplní (nepřepíše účtem předvyplněný text).
    U kontaktu z profilu používáme force=True, aby se špatný předvypl z Jobs.cz / microsite přepsal.
    """
    if not value:
        return False
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
    try:
        loc.click(timeout=2000)
        loc.fill(value, timeout=10000)
        return True
    except PlaywrightError:
        return False


def _try_by_label(frame, value: str, patterns: tuple[str, ...]) -> bool:
    """Labely typu 'Jméno *', 'Jméno:', 'Jméno' — matchuje bez striktního ^$."""
    if not value:
        return False
    for pat in patterns:
        try:
            loc = frame.get_by_label(re.compile(pat, re.I))
            if loc.count() > 0 and _fill_visible_input(loc.first, value, force=True):
                return True
        except PlaywrightError:
            continue
    return False


def _try_by_role_textbox(frame, value: str, patterns: tuple[str, ...]) -> bool:
    """ARIA přístupné jméno (aria-label / label) přes role=textbox."""
    if not value:
        return False
    for pat in patterns:
        try:
            loc = frame.get_by_role("textbox", name=re.compile(pat, re.I))
            if loc.count() > 0 and _fill_visible_input(loc.first, value, force=True):
                return True
        except PlaywrightError:
            continue
    return False


def _try_by_selector(frame, value: str, selectors: tuple[str, ...]) -> bool:
    if not value:
        return False
    for sel in selectors:
        try:
            fl = frame.locator(sel)
            if fl.count() > 0 and _fill_visible_input(fl.first, value, force=True):
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
            _try_by_label(frame, full, combined_patterns)
            or _try_by_role_textbox(frame, full, combined_patterns)
            or _try_by_selector(frame, full, combined_selectors)
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
        _try_by_label(frame, first, first_patterns) \
            or _try_by_role_textbox(frame, first, first_patterns) \
            or _try_by_selector(frame, first, first_selectors)

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
        _try_by_label(frame, last, last_patterns) \
            or _try_by_role_textbox(frame, last, last_patterns) \
            or _try_by_selector(frame, last, last_selectors)

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
    _try_by_label(frame, email, email_patterns) \
        or _try_by_role_textbox(frame, email, email_patterns) \
        or _try_by_selector(frame, email, email_selectors)

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
    _try_by_label(frame, phone, phone_patterns) \
        or _try_by_role_textbox(frame, phone, phone_patterns) \
        or _try_by_selector(frame, phone, phone_selectors)


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
                if not el.is_visible(timeout=800):
                    continue
            except PlaywrightError:
                continue
            try:
                el.click(timeout=3000)
                el.fill(message, timeout=12000)
                return True
            except PlaywrightError:
                try:
                    el.click(timeout=2000)
                    page.keyboard.type(message, delay=12)
                    return True
                except PlaywrightError:
                    continue
    try:
        tb = frame.get_by_role("textbox")
        if tb.count() > 0:
            el = tb.first
            if el.is_visible(timeout=1200):
                el.fill(message, timeout=12000)
                return True
    except PlaywrightError:
        pass
    return False


def _fill_message(page, message: str) -> bool:
    for fr in page.frames:
        if _fill_message_in_frame(fr, page, message):
            return True
    return False


def _try_check_checkbox(cb) -> bool:
    """
    Jobs.cz / Alma Career mají nativní <input type=checkbox> často schovaný
    (opacity:0 / display:none) a klikatelný je jen <label>. Neopíráme se o is_visible.
    """
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

        # 3) Všechny checkboxy ve formuláři (Jobs.cz microsite — bývá jich 2-4)
        form_boxes = fr.locator("form input[type='checkbox']")
        for i in range(min(form_boxes.count(), 16)):
            _try_check_checkbox(form_boxes.nth(i))

        # 4) Explicitně required / aria-required
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


def _submission_succeeded(page, start_url: str) -> bool:
    """URL změna, nebo potvrzovací text (včetně SPA na stejné adrese)."""
    try:
        page.wait_for_timeout(2600)
    except PlaywrightError:
        pass

    current = page.url.split("#")[0]
    base_start = start_url.split("?")[0].rstrip("/")
    cur_path = current.split("?")[0].rstrip("/")

    if cur_path != base_start:
        return True

    try:
        p0 = urlparse(current)
        qs = p0.query.lower()
        if any(
            x in qs
            for x in (
                "success",
                "sent",
                "odeslano",
                "thank",
                "dekuji",
                "děkuj",
                "confirmed",
            )
        ):
            return True
    except Exception:
        pass

    blob = _gather_visible_text(page)
    if re.search(
        r"děkujeme|děkujeme\s+vám|děkujeme,\s*že|odeslán|odeslána|úspěšně\s+odesl|"
        r"vaše\s+(žádost|odpověď|přihláška|reakce)|žádost\s+(byla\s+)?odesl|odpověď\s+byla\s+odesl|"
        r"reakce\s+byla\s+(úspěšně\s+)?odesl|životopis\s+(byl\s+)?odeslán|"
        r"potvrzení|přijat(a|o)?|přijali\s+jsme|přihláška\s+byla\s+přijata|"
        r"thank\s*you|application\s+(was\s+)?(received|submitted)|successfully\s+submitted|"
        r"we\s+(have\s+)?received\s+your",
        blob,
        re.I,
    ):
        return True

    ok_text = page.get_by_text(
        re.compile(
            r"děkujeme|odeslán|odeslána|úspěšně|potvrzení|žádost byla|thank you|application (received|submitted)|successfully",
            re.I,
        )
    )
    try:
        if ok_text.count() > 0 and ok_text.first.is_visible(timeout=2500):
            return True
    except PlaywrightError:
        pass

    for fr in page.frames:
        try:
            if fr.get_by_role("heading", name=re.compile(r"děkujeme|hotovo|thank you", re.I)).count() > 0:
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
) -> tuple[bool, str]:
    """
    Vrátí (True, "") při úspěchu, jinak (False, krátký důvod pro log).
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

    with sync_playwright() as p:
        sm = max(0, min(10_000, int(browser_slow_mo_ms)))
        browser = _launch_chromium(p, slow_mo_ms=sm, headless=headless)
        context = browser.new_context(storage_state=storage_state_path)
        page = context.new_page()
        page.goto(listing.url, wait_until="load", timeout=90000)

        try:
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
                return _apply_fail(
                    page,
                    listing,
                    "odeslání nepotvrzeno (stejná URL a nenašel jsem text „děkujeme“ — zkontroluj ručně v prohlížeči)",
                )
            return True, ""
        finally:
            context.close()
            browser.close()
