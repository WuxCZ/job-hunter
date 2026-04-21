from __future__ import annotations

import io
import json
import re
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    import google.generativeai as genai

from collections.abc import Iterator

from jobhunter_bot.db import JobListing
from jobhunter_bot.scraper import JobDetailSummary


def build_panel_summary_prompt(listing: JobListing, detail: JobDetailSummary) -> str:
    return f"""
Jsi editor pracovních nabídek. Pracuj POUZE s údaji níže — nic si nevymýšlej.

Vstup:
- Název (výpis): {listing.title}
- Firma (výpis): {listing.company or "neuvedeno"}
- Název (detail): {detail.title or "neuvedeno"}
- Firma (detail): {detail.company or "neuvedeno"}
- Lokalita: {detail.location or "neuvedeno"}
- Text inzerátu (může být zmíchaný PR firmy + náplň):
{detail.snippet or "žádný text"}

ÚKOL — v češtině s diakritikou napiš přehled, který se dobře čte v jednom sloupci (žádné zbytečné opakování).

Formát výstupu (markdown):
## Role
Jedna řádka: stručný název pozice.

## Kde
Firma a lokalita (nebo „neuvedeno“).

## O čem to je
4–6 vět o reálné náplni, technologiích, týmu, režimu práce — jen co z textu plyne. Když je v textu jen omáčka o firmě, řekni to na rovinu.

## Požadavky
Odrážky „- …“ (max 6), nebo jedna věta že z úryvku to nejde.

## V jedné větě
Komu je role určená / hlavní riziko nejasnosti.

Max znaků cca 1800. Žádné úvodní „Jako AI…“.
"""


def build_job_panel_summary(
    api_key: str,
    model_name: str,
    listing: JobListing,
    detail: JobDetailSummary,
) -> str:
    """
    Souhrn do levého panelu GUI: buď přes Gemini z načtených dat, nebo čistý text z HTML.
    """
    base = detail.format_text()
    if not api_key:
        return base

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name=model_name)
    prompt = build_panel_summary_prompt(listing, detail)
    try:
        result = model.generate_content(prompt)
        text = (result.text or "").strip()
    except Exception:
        text = ""
    return text if text else base


def stream_job_panel_summary(
    api_key: str,
    model_name: str,
    listing: JobListing,
    detail: JobDetailSummary,
) -> Iterator[str]:
    """Proudové generování souhrnu pro GUI (levý panel se plní průběžně)."""
    base = detail.format_text()
    if not api_key:
        yield base
        return

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name=model_name)
    prompt = build_panel_summary_prompt(listing, detail)
    try:
        response = model.generate_content(prompt, stream=True)
        for chunk in response:
            t = getattr(chunk, "text", None) or ""
            if t:
                yield t
    except Exception:
        yield base


_MAX_MESSAGE_CHARS = 320


def _clean_message_text(raw: str) -> str:
    """
    Gemini 2.x někdy vrací markdown/blok ```cs…```, úvodní prázdné řádky, bullety apod.
    Pro textarea chceme čistý plain-text, jeden odstavec (max 1 prázdný řádek mezi nimi).
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rsplit("```", 1)[0]
    lines: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith(("# ", "## ", "### ")):
            s = s.lstrip("# ").strip()
        if s.startswith(("- ", "* ")):
            s = s[2:].strip()
        s = s.replace("**", "").replace("__", "").replace("`", "")
        lines.append(s)
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    text = text.strip()
    if len(text) > _MAX_MESSAGE_CHARS:
        cut = text.rfind(". ", 0, _MAX_MESSAGE_CHARS)
        text = (text[: cut + 1] if cut > _MAX_MESSAGE_CHARS // 2 else text[:_MAX_MESSAGE_CHARS]).strip()
    return text


def build_message(
    api_key: str,
    model_name: str,
    listing: JobListing,
    sender_name: str = "",
) -> str:
    """
    Krycí dopis je záměrně deterministický (šablona), protože AI generování dělalo
    příliš dlouhé / formátované texty nevhodné do textarey. Parametry api_key / model_name
    zůstávají kvůli kompatibilitě volání, ale nepoužívají se.
    """
    _ = (api_key, model_name)
    return default_message(listing, sender_name=sender_name)


_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F2FF"
    "\U0001F900-\U0001F9FF"
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    """Z titulků typu '🔧 Technik testování HW' odstraní emoji a zbytečné mezery."""
    cleaned = _EMOJI_RE.sub("", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t\n\r-–—•|·:")
    return cleaned


def default_message(listing: JobListing, sender_name: str = "") -> str:
    company = _strip_emoji(listing.company or "")
    company_part = f" u {company}" if company else " u Vaší společnosti"
    title = _strip_emoji(listing.title or "") or "Vaše pozice"
    sender = (sender_name or "").strip() or "Marek Šolc"
    return (
        "Dobrý den,\n"
        f"líbí se mi nabídka {title}{company_part} a rád bych na ni reagoval. "
        "V příloze zasílám svůj životopis.\n"
        "S pozdravem,\n"
        f"{sender}"
    )


_FORM_STATE_JS = r"""() => {
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll("input, textarea, select")) {
    const ty = (el.type || "").toLowerCase();
    if (ty === "hidden" || ty === "submit" || ty === "button" || ty === "reset") continue;
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    const visible =
      r.width > 0 && r.height > 0 &&
      st.visibility !== "hidden" &&
      st.display !== "none" &&
      el.getAttribute("aria-hidden") !== "true";
    if (!visible) continue;
    const val = (el.value || "").trim();
    const key = (el.name || el.id || String(out.length));
    if (seen.has(key)) continue;
    seen.add(key);
    const req = el.required || el.getAttribute("aria-required") === "true";
    out.push({
      tag: el.tagName,
      type: ty,
      name: (el.name || "").slice(0, 64),
      id: (el.id || "").slice(0, 64),
      placeholder: (el.placeholder || "").slice(0, 48),
      requiredHint: !!req,
      empty: val.length === 0,
      valuePreview: val.slice(0, 72),
      ariaInvalid: el.getAttribute("aria-invalid") === "true",
    });
  }
  const errNodes = [...document.querySelectorAll(
    '[role="alert"], .error, [class*="error"], [class*="invalid"], [data-error]'
  )];
  const errs = [];
  for (const e of errNodes) {
    const t = (e.textContent || "").trim();
    if (t && t.length < 200) errs.push(t);
  }
  return {
    fields: out.slice(0, 100),
    visibleErrorTexts: [...new Set(errs)].slice(0, 20),
  };
}"""


def _parse_json_object_from_gemini(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _resize_png_for_gemini(png: bytes, max_w: int = 1280, max_h: int = 4096) -> bytes:
    from PIL import Image

    im = Image.open(io.BytesIO(png))
    im.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def gemini_validate_application_form(
    api_key: str,
    model_name: str,
    page: object,
) -> tuple[bool | None, str]:
    """
    Po vyplnění formuláře: Gemini dostane JSON viditelných polí + screenshot (zmenšený).

    Vrátí (ready, řádek_do_logu).
    ready=None — bez klíče nebo kontrola nedoběhla (část zpráv i tak popíše důvod).
    ready=True/False — model se domnívá, že je / není rozumné odesílat.
    """
    if not (api_key or "").strip():
        return None, ""

    try:
        payload = page.evaluate(_FORM_STATE_JS)
    except Exception as exc:
        return None, f"Kontrola Gemini: nepodařilo se načíst stav formuláře ({exc})"

    try:
        png = page.screenshot(type="png", full_page=True, timeout=60000)
    except Exception:
        try:
            png = page.screenshot(type="png", timeout=30000)
        except Exception as exc2:
            return None, f"Kontrola Gemini: screenshot selhal ({exc2})"

    try:
        png = _resize_png_for_gemini(png)
    except Exception:
        pass

    prompt = f"""Jsi kontrola pracovního přihlašovacího formuláře (ČR).

Máš (1) strukturovaná data polí z DOM a (2) screenshot stránky (může být nepřesně čitelný).

Úkol: Rozhodni, zda má uživatel smysl dokončit odeslání — povinná pole vyplněná, CV/souhlasy pokud je formulář vyžaduje, žádné výrazné inline chyby.

Strukturovaná data:
{json.dumps(payload, ensure_ascii=False, indent=2)}

Pravidla:
- requiredHint=true a empty=true u důležitých typů (text, email, tel, textarea, select) = silný signál problému.
- Prázdná velká textarea často znamená nevyplněnou zprávu.
- Soubor CV: pokud je file input a value v DOM často neukáže název, spolehni se na screenshot + kontext.
- Když si nejsi jistý/á, nastav ready=true a krátce to uveď v issues.

Odpověz POUZE platným JSON objektem (žádný markdown), přesně tento tvar klíčů:
{{"ready": true nebo false, "issues": ["krátké body česky"], "comment": "max 2 věty česky"}}"""

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name=model_name)
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(png))
        gen_kwargs: dict = {
            "temperature": 0.15,
            # Gemini 2.5-flash má „thinking" tokeny — musí se vejít i s reálnou odpovědí.
            "max_output_tokens": 2048,
            "response_mime_type": "application/json",
        }
        try:
            generation_config = genai.types.GenerationConfig(**gen_kwargs)
        except TypeError:
            # Starší verze knihovny (bez response_mime_type) — fallback
            gen_kwargs.pop("response_mime_type", None)
            generation_config = genai.types.GenerationConfig(**gen_kwargs)
        resp = model.generate_content(
            [prompt, image],
            generation_config=generation_config,
        )
        raw = (resp.text or "").strip()
    except Exception as exc:
        return None, f"Kontrola Gemini: volání API selhalo ({exc})"

    parsed = _parse_json_object_from_gemini(raw)
    if not isinstance(parsed, dict):
        snippet = (raw or "").replace("\n", " ")[:180]
        return None, (
            "Kontrola Gemini: model nevrátil rozumný JSON (zkontroluj ručně)."
            + (f" | ukázka: {snippet}" if snippet else "")
        )

    ready = parsed.get("ready")
    issues = parsed.get("issues") or []
    comment = (parsed.get("comment") or "").strip()

    if not isinstance(ready, bool):
        return None, "Kontrola Gemini: nejasná odpověď (chybí boolean ready)."

    issues_text = ""
    if isinstance(issues, list) and issues:
        issues_text = "; ".join(str(x) for x in issues[:8] if x)

    if ready:
        msg = "Kontrola Gemini: formulář vypadá připraveně"
        if comment:
            msg += f". {comment}"
        return True, msg

    msg = "Kontrola Gemini: možné nedostatky"
    if issues_text:
        msg += f" — {issues_text}"
    if comment:
        msg += f" ({comment})"
    return False, msg


# --- Fit scoring vyladěný na profil: HW, helpdesk/servicedesk L1/L2, Windows Server ---
#
# Baseline je schválně LOW (20), aby "IT" samotné nestačilo. Skóre ≥50 vyžaduje
# alespoň jedno silné plus. Silné minus (manager/konzultant/obchodník/stavař) má
# masivní dopad, aby nic takového neproleze.
# Noční směny: uživatel nechce noční provoz — explicitní signály v názvu silně sníží skóre.

_STRONG_POSITIVE = [
    # Helpdesk / Servicedesk / L1 L2
    r"service\s*desk",
    r"servicedesk",
    r"helpdesk",
    r"help\s*desk",
    r"\bIT\s*support\b",
    r"\buser\s*support\b",
    r"desktop\s*support",
    r"technical\s*support",
    r"podpora\s*u[žz]ivatel",
    # L1/L2 / tier
    r"\bL1\b", r"\bL2\b", r"\btier\s*1\b", r"\btier\s*2\b",
    # HW technik / hardware / PC
    r"\bHW\b",
    r"\bhardware\b",
    r"pc\s*technik",
    r"it\s*technik",
    r"(hardwarov|hw)[ýyé]?\s*technik",
    r"technik\s*(hw|hardware|pc|it|sít|server|notebook)",
    # Windows Server / admin
    r"windows\s*server",
    r"server(ov|ů)\s*administr",
    r"(systém|system)[oa]?\s*administr",
    # Junior varianty obecně
    r"\bjunior\b",
]

_MEDIUM_POSITIVE = [
    r"\btechnik\b",
    r"\bservis\b",
    r"\bpodpora\b",
    r"\badministr[áa]tor\b",
    r"infrastruktur",
    r"onsite",
]

_STRONG_NEGATIVE = [
    # Management / business role, které NECHCEME
    r"\bmanager\b", r"\bmana[žz]er\b", r"\bmana[žz]erka\b",
    r"\bdirector\b", r"\b(ředitel|reditel)\b",
    r"\bhead\s+of\b", r"\bvedouc[íi]\b",
    r"\blead\b", r"\bteam\s*lead\b", r"\bteamlead\b",
    # Consulting / analytika bez IT specifikace
    r"\bconsult(ant|ing)\b", r"\bkonzultant\b", r"\bkonzultantka\b",
    r"\barchitect(ure|ect)?\b", r"\barchitekt\b", r"\barchitektka\b",
    # Recruiter / HR
    r"\brecruit(er|ment)\b", r"\btalent\s*partner\b", r"\bhr\b", r"\bpersonalist",
    # Sales / obchod
    r"\bsales\b", r"\bobchod", r"\bobchodní\b", r"\bobchodnik\b", r"\bobchodnice\b",
    r"\baccount\s*manager\b", r"\bkey\s*account\b",
    # Stavebnictví / koordinátor / projektový manažer
    r"\bstavař\b", r"\bstavební?\b", r"\bstavebn",
    r"\bkoordinátor", r"\bprojektov[ýa]\s*manažer", r"\bproject\s*manager\b", r"\bscrum\s*master\b",
    # Vývoj / ne-HW engineer role
    r"\bdevelop(er|ment)\b", r"\bv[ýy]voj[áa][řr]\b", r"\bprogramátor\b",
    r"\bsoftware\s*engineer\b", r"\bsoftwarov[ýý]\s*in[žz]en[ýe]r\b",
    r"\bdata\s*engineer\b", r"\bdevops\s*engineer\b", r"\bsales\s*engineer\b",
    r"\bcloud\s*engineer\b", r"\bsite\s*reliability\b",
    # Data / AI / Cloud architect role
    r"\bdata\s*(scientist|analyst)\b", r"\bmachine\s*learning\b",
    r"\bcloud\s*architect\b", r"\bdevops\b", r"\bsre\b",
    # SAP / ERP konzultace (typicky není Windows/HW)
    r"\bsap\b", r"\berp\b",
]

# Noční směny — zvlášť: po výpočtu skóre se aplikuje tvrdý strop (viz evaluate_fit),
# aby „helpdesk + night shift“ kvůli dvojímu počítání plusů stále neprošel min fit 50.
_NIGHT_SHIFT_TITLE = [
    r"nočn[íi]\s*směn",
    r"směn[ay]?\s*nočn",
    r"nočn[íi]\s*provoz",
    r"nočn[íi]\s*údržb",
    r"nočn[íi]\s*práce",
    r"night\s*shift",
    r"overnight\s*shift",
    r"graveyard\s*shift",
]

_MEDIUM_NEGATIVE = [
    r"\bsenior\b",
    r"\banalytik\b",
    r"\banalyst\b",
]


def _matches_any(title: str, patterns: list[str]) -> list[str]:
    hits = []
    for p in patterns:
        m = re.search(p, title, re.I)
        if m:
            hits.append(m.group(0))
    return hits


def evaluate_fit(listing: JobListing) -> tuple[int, str, str]:
    """
    Profil: HW technik / helpdesk / service desk L1 L2 / Windows Server.
    Bez nočních směn (noční směna / night shift v názvu silně snižuje skóre).
    Baseline 20, silný plus +25, střední +10, silný minus -40, střední -15.
    """
    title = (listing.title or "").lower()

    strong_pos = _matches_any(title, _STRONG_POSITIVE)
    medium_pos = _matches_any(title, _MEDIUM_POSITIVE)
    strong_neg = _matches_any(title, _STRONG_NEGATIVE)
    medium_neg = _matches_any(title, _MEDIUM_NEGATIVE)
    night_hits = _matches_any(title, _NIGHT_SHIFT_TITLE)

    score = 20
    score += min(70, len(strong_pos) * 30)
    score += min(20, len(medium_pos) * 10)
    score -= min(70, len(strong_neg) * 40)
    score -= min(30, len(medium_neg) * 15)
    score = max(0, min(100, score))

    if night_hits:
        # Pod defaultní min fit (50) — uživatel nechce noční provoz
        score = min(score, 40)

    if score >= 65:
        reason = "Silná shoda (helpdesk / HW / Windows Server)."
    elif score >= 50:
        reason = "Středně slušná shoda, spíš relevantní."
    elif score >= 30:
        reason = "Slabší shoda, nejistá."
    else:
        reason = "Nízká shoda (manager / obchod / architekt / noční směna / mimo profil)."

    plus_text = ", ".join(dict.fromkeys(strong_pos + medium_pos)) or "žádná silná klíčová slova"
    minus_parts = list(dict.fromkeys(strong_neg + medium_neg + night_hits))
    minus_text = ", ".join(minus_parts) or "žádné výrazné negativní signály"
    details = (
        f"+ Pozitivní signály: {plus_text}\n"
        f"- Negativní signály: {minus_text}\n"
        f"Profil: HW technik / helpdesk / servicedesk L1 L2 / Windows Server. "
        f"Baseline 20, silný plus +25, střední +10, silný minus -40, střední -15."
    )
    return score, reason, details
