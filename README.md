# Job Hunter

Automatizovaný bot na odpovídání na pracovní inzeráty z **Jobs.cz** a všech **Alma Career microsite** (Teamio, firemní odpovědní formuláře).

Bot umí sám otevřít inzerát, vyplnit kontaktní údaje, mzdové očekávání, krátký motivační dopis, nahrát CV, zaškrtnout souhlasy a odeslat přihlášku — **nebo** se před odesláním zastavit a nechat tě to schválit v GUI.

> Public-ready release: moderní GUI, safe-mode ochrany, noční watchdog režim (`AUTO.bat`), live log (`WATCH.bat`) a detailní diagnostika selhání.

---

## Obsah

- [Co to umí](#co-to-umí)
- [Požadavky](#požadavky)
- [Rychlý start](#rychlý-start)
- [Konfigurace (`.env`)](#konfigurace-env)
- [GUI režim (doporučeno)](#gui-režim-doporučeno)
- [CLI režim](#cli-režim)
- [Jak funguje vyplňování formulářů](#jak-funguje-vyplňování-formulářů)
- [Krycí dopis](#krycí-dopis)
- [Gemini: souhrn inzerátu & validace formuláře](#gemini-souhrn-inzerátu--validace-formuláře)
- [Struktura repa](#struktura-repa)
- [Diagnostika a debug složky](#diagnostika-a-debug-složky)
- [Bezpečnost & privátní data](#bezpečnost--privátní-data)
- [Odpovědné používání](#odpovědné-používání)
- [Problémy a troubleshooting](#problémy-a-troubleshooting)

---

## Co to umí

- **Scraper Jobs.cz** — stránkování, detaily inzerátu z `__NEXT_DATA__`, čistý text popisu
- **Fit score** 0–100 s krátkým zdůvodněním (kritéria podle profilu: lokalita, klíčová slova, radius)
- **Automatické vyplnění formulářů** přes Playwright:
  - jméno / příjmení / e-mail / telefon s robustními selektory (label + ARIA role + CSS + placeholder)
  - **mzdové očekávání** (input i `<select>` s rozsahy, např. „30 000 – 50 000 Kč")
  - krátký český motivační dopis (3 věty, šablona, žádné AI halucinace)
  - nahrání PDF CV
  - zaškrtnutí souhlasů (GDPR, marketing, provozovatel) — i pro custom-styled checkboxy, které nemají nativní `<input>` viditelný v DOMu
- **Manuální schvalování** — formulář se vyplní v prohlížeči a bot počká na tvoje **Schválit / Přeskočit / Stop vše**
- **Auto režim v GUI** — žádné schvalování, bot jede od A do Z (není to totéž co `AUTO.bat`)
- **Dry run** — všechno proběhne, ale odeslání se přeskočí
- **Duplicitní ochrana** — lokální SQLite DB + synchronizace s **Historií odpovědí na Jobs.cz** (pokud je platná session)
- **Kontrola inboxu** přes IMAP (spáruje přijaté odpovědi s odeslanými přihláškami)
- **Fallback "dokončím ručně"** — při FAILu můžeš nechat Chrome otevřený, formulář doplnit ručně a bot mezitím pokračuje na další nabídku
- **Gemini** jen na:
  - souhrn inzerátu do levého panelu GUI
  - AI validaci vyplněného formuláře (screenshot + DOM → „vše vyplněné?")
- **Multi-profily** — každý profil má své CV, session, kontakt, mzdu, filtr lokality / query / radius
- **Preview okno** — inzerát se otevře v izolovaném Chrome oknu (vlastní user-data-dir), po rozhodnutí se automaticky zavře

---

## Požadavky

- **Windows 10/11** (testováno; macOS / Linux by měl fungovat, `.bat` skripty jsou Windows-only)
- **Python 3.11+**
- **Google Chrome** nebo **Chromium** (Playwright si stáhne sám)
- **Gemini API klíč** (zdarma na [aistudio.google.com](https://aistudio.google.com/app/apikey)) — jen když chceš souhrn inzerátu nebo AI validaci formuláře, jinak volitelné
- **IMAP přístup k e-mailu** (volitelné, jen pro spárování odpovědí)

---

## Rychlý start

```powershell
# 1) klon
git clone https://github.com/WuxCZ/job-hunter.git
cd job-hunter

# 2) virtualenv + závislosti
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# 3) konfigurace
copy .env.example .env
notepad .env         # vyplň GEMINI_API_KEY, IMAP_USER, IMAP_PASSWORD, CV_PATH

# 4) login do Jobs.cz (jednorázově — uloží storage-state.json)
python main.py init-session

# 5) spustit GUI
python main.py gui
# nebo dvojklikem Start.bat
```

První, co udělej po spuštění GUI:

1. Záložka **Nastavení** → **Kontakt pro přihlášky**: vyplň jméno, e-mail, telefon, mzdu (default 50 000 Kč).
2. Záložka **Nastavení** → **Nahrát / změnit CV** (povinné, PDF).
3. Klikni **Login do Jobs.cz** (otevře se Chrome, přihlásíš se, zavřeš okno → session se uloží).
4. Na dashboardu zapni **Dry run** a dej **Start**. Otestuj pár inzerátů v manuálním režimu.
5. Když to sedí, vypni Dry run a pusť auto.

**Chceš to nechat běžet přes noc bez klikání?** Dvojklik `AUTO.bat` v rootu — viz [AUTO mode sekce](#auto-mode-smyčka-přes-noc) níže.

---

## Konfigurace (`.env`)

Vytvoř `.env` podle `.env.example`:

```ini
# URL s filtry Jobs.cz (lokalita, radius, vzdělání, …) — filtr, podle kterého bot scrapuje
JOBS_SEARCH_URL=https://www.jobs.cz/prace/brno/?locality%5Bradius%5D=20

# Cesta k CV (PDF)
CV_PATH=C:\Users\tvuj_uzivatel\Documents\CV.pdf

# Gemini API klíč (volitelné)
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash

# IMAP pro kontrolu přijatých odpovědí (volitelné)
IMAP_HOST=imap.seznam.cz
IMAP_PORT=993
IMAP_USER=
IMAP_PASSWORD=
IMAP_FOLDER=INBOX

# Provoz
DB_PATH=jobhunter.db
STORAGE_STATE_PATH=storage-state.json
REQUEST_TIMEOUT_SECONDS=25

# Volitelné: přepisují profil, pokud profil pole nemá vyplněné
APPLICANT_FULL_NAME=
APPLICANT_EMAIL=
APPLICANT_PHONE=
APPLICANT_SALARY=50000

# Gemini strict validace formuláře: při `1` bot odmítne odeslat, když AI vyhodnotí formulář jako neúplný
FORM_VALIDATE_STRICT=0
```

Profilové hodnoty (jméno, e-mail, telefon, mzda) v GUI **mají přednost** před `.env`.

---

## GUI režim (doporučeno)

```powershell
python main.py gui
# nebo:
Start.bat
```

### Dashboard

- **Profil** — přepínání mezi profily (každý má svoje CV, session, kontakt, mzdu)
- **Režim**: `manual` (schvaluješ) / `auto` (bez ptaní)
- **Důležité**: `auto` v GUI = stejný běh jako manual, jen bez tlačítek Schválit/Přeskočit.  
  Noční watchdog smyčka je samostatně přes `AUTO.bat` v rootu.
- **Limit** — max. počet inzerátů na jeden běh
- **Dry run** — žádné reálné odeslání, jen simulace (CV + kontakt + zpráva se vyplní, pak skip)
- **Dry: ignorovat duplicity v DB** — pro opakovaný test stejných inzerátů
- **Preview na 2. monitor** — otevře inzerát v samostatném Chrome okně vedle
- **Start / Stop / Obnovit historii**
- **Safe mode** (doporučeno, zapnuté defaultně): rozumné brzdy proti banu
  - `min fit` (default 50) — bot přeskočí inzeráty s fit score pod prahem
  - `max odeslání` (default 50) — tvrdý limit odeslání v jednom běhu
  - `pauza mezi pokusy` (default 15 s) — rate-limit mezi přihláškami
  - `stop po FAILech v řadě` (default 5) — po tolika chybách po sobě se běh zastaví (čekej, než vyprší server error / změníš nastavení)
  - při chybě serveru jobs.cz („We run into some problem") bot automaticky zkusí odeslání **ještě jednou po 60s**
  - volba **„Při FAIL ponechat Chrome otevřený (CDP)"**: když odeslání selže, okno zůstane otevřené pro ruční dokončení, zatímco bot pokračuje dál

V manuálním režimu se u každého inzerátu objeví box **„Čeká na schválení"**:

- **horní info**: název pozice, firma, skóre, URL, pokyn zkontrolovat prohlížeč
- **dolní box „Odpověď, která se odešle do formuláře"**: přesné znění motivačního dopisu
- tlačítka **Schválit** / **Přeskočit** / **Stop vše**

Paralelně je **Prohlížeč** s Playwrightem vpravo, kde vidíš vyplněný formulář — než kliknou na Schválit, můžeš si ho zkontrolovat očima.

### Nastavení

- **Profil** (CV, lokalita, query, radius, session path)
- **Kontakt pro přihlášky** (jméno, e-mail, telefon, **mzdové očekávání**)
- **Login / Logout do Jobs.cz**
- **Debug slow-mo** pro Playwright (vidět, jak bot klikání dělá v reálném čase)

---

## CLI režim

```powershell
python main.py <command> [args]
```

| Příkaz | Co dělá |
|---|---|
| `init-session` | Otevře okno, přihlásíš se na Jobs.cz, session se uloží do `storage-state.json` |
| `scrape --limit N` | Jen naparsuje N inzerátů z `JOBS_SEARCH_URL` a uloží do DB |
| `check-mail --limit N` | Projde IMAP a spáruje odpovědi s přihláškami v DB |
| `run --limit N` | Hlavní běh: scrape + apply |
| `gui` | Spustí GUI |
| `show-config` | Vypíše loadnutou konfiguraci (hesla jsou maskovaná) |

Užitečné flagy k `run`:

```powershell
python main.py run --limit 50 --dry-run --ignore-db --no-gemini-form-check --headless
```

- `--dry-run` — žádné reálné odeslání
- `--ignore-db` (jen s dry-run) — ignoruj lokální DB duplicity (pro opakovaný dávkový test)
- `--no-gemini-form-check` — nevolej AI validaci formuláře (ušetříš API quota při stovkách pokusů)
- `--headless` — Chromium bez okna (rychlejší)
- `--browser-slow-mo 600` — zpomalení v ms mezi akcemi Playwrightu (pro debug)
- `JOBHUNTER_LEAVE_BROWSER_ON_FAIL=1` — při FAILu ponechá systémový Chrome/Edge otevřený pro ruční dokončení formuláře (CLI režim)

---

## Jak funguje vyplňování formulářů

Bot pracuje ve **třech úrovních tolerance**, každá zkouší tu další, když předchozí selže:

1. **`<label>` match** — regexy jako `Jméno(\s*\*)?`, `Příjmení`, `E-?mail`, `Telefon`, `Mzdov[áé]\s*o[čc]ek[áa]v[áa]n[íi]` — chytne i `Jméno *`, `Jméno:`, `Mzdové očekávání`.
2. **ARIA `role=textbox`** — pro React formuláře bez klasického `<label for="…">`.
3. **CSS selektory** — `name=`, `id=`, `autocomplete=`, `placeholder=` s wildcards (`*=`) a case-insensitive (`i`).

Speciálně:

- **Kombinované pole** „Jméno a příjmení" se zkouší **první** (hodně microsite ho mají).
- **Mzdový `<select>` s rozsahy** — bot parsuje čísla z textů options a vybere tu, do jejíhož rozsahu hodnota spadne.
- **Alma Career microsites** — pořadí kroků: klik „Nahrát vlastní CV" (odkryje skrytá pole) → kontakt + zpráva → upload PDF → druhé kolo kontaktu (některé microsite přerenderují pole až po volbě souboru) → mzda → souhlasy.
- **Souhlasy** — `cb.check()` → `check(force=True)` → JS `el.checked = true` + `dispatchEvent('input')` + `dispatchEvent('change')` → fallback klik na `<label>`, ale **jen když je přidružený `<input>` ještě unchecked** (aby label neodškrtl už zaškrtnutý box).

---

## Krycí dopis

Záměrně **deterministická šablona**, ne AI. Důvod: Gemini dělalo moc dlouhé / markdown formátované texty, které se do `<textarea>` hodí špatně.

```
Dobrý den,
líbí se mi nabídka <pozice> u <firma> a rád bych na ni reagoval. V příloze zasílám svůj životopis.
S pozdravem,
<tvoje jméno>
```

- Z názvu inzerátu se automaticky **odstraní emoji** (`🔧 Technik…` → `Technik…`).
- Když není známá firma, použije se `„u Vaší společnosti"`.
- Podpis je ze `profile.applicant_full_name` (fallback `APPLICANT_FULL_NAME` z `.env`, dále `Marek Šolc` jako sentinel).

Pokud chceš delší / custom dopis, uprav `jobhunter_bot/ai.py` → funkci `default_message`.

---

## Gemini: souhrn inzerátu & validace formuláře

Gemini se používá jen na **dvě** věci (ne na psaní dopisu):

1. **Souhrn inzerátu** do levého panelu dashboardu — streamuje, aby text nabíhal průběžně.
2. **Validace vyplněného formuláře** — pošle screenshot + stav DOM (label, placeholder, hodnota, required, chybové hlášky) a vrátí `{ "ready": true/false, "msg": "…" }`.

- Když `FORM_VALIDATE_STRICT=1` a Gemini řekne „neúplný", bot **odmítne odeslat**. Default je `0` (logne warning, ale odešle).
- Bez `GEMINI_API_KEY` se oba kroky přeskočí bez chyby — bot funguje dál.

Defaultní model: **`gemini-2.5-flash`**. Stará jména (`gemini-1.5-flash`) se mapují na aktuální modely automaticky v `jobhunter_bot/config.py`.

---

## Struktura repa

```
job-hunter/
├── jobhunter_bot/
│   ├── ai.py                  # Gemini prompty + krycí dopis + validace formuláře
│   ├── apply_failure_dump.py  # debug dump při neúspěšném odeslání
│   ├── browser_apply.py       # Playwright logika (vyplnění + submit + consents)
│   ├── cli.py                 # argparse + CLI příkazy
│   ├── config.py              # .env loader + mapování retired Gemini modelů
│   ├── db.py                  # SQLite (jobhunter.db)
│   ├── email_monitor.py       # IMAP kontrola odpovědí
│   ├── gui.py                 # starší GUI (backup)
│   ├── jobs_history.py        # scraping Historie odpovědí z jobs.cz
│   ├── modern_gui.py          # aktuální CustomTkinter GUI (dashboard + nastavení)
│   ├── preview.py             # izolované Chrome preview okno
│   ├── profiles.py            # UserProfile + ProfileStore (profiles.json)
│   ├── scraper.py             # Jobs.cz listing + detail scraper
│   └── urlnorm.py             # normalizace URL pro deduplikaci
├── main.py                    # entrypoint: python main.py <command>
├── requirements.txt
├── .env.example
├── Start.bat                  # dvojklik spustí GUI
├── Vytvorit_zastupce*.ps1     # vytvoření Windows zástupce
└── README.md
```

Po prvním spuštění vzniknou navíc (všechno je v `.gitignore`, takže se nepushuje):

```
.env                           # tvé API klíče + IMAP heslo
storage-state.json             # session cookies na Jobs.cz
profiles.json                  # tvé profily (jméno, e-mail, telefon, mzda)
jobhunter.db                   # lokální DB inzerátů + odpovědí
debug_apply_failures/          # screenshoty + HTML z neúspěšných odeslání
debug_jobs_history/            # snapshoty historie Jobs.cz pro ladění
```

---

## Údržba & cleanup

### Vyčištění falešných "applied" v DB

Bot dřív uměl falešně označit odeslání jako úspěšné (od commitu `bcaa52a` už to dělat nebude). Pokud máš staré záznamy, spusť:

```powershell
# Dry-run (jen vypíše):
python -m tools.clean_false_applied

# Skutečně zapsat:
python -m tools.clean_false_applied --apply
```

Skript si stáhne tvou reálnou historii z Jobs.cz a srovná s lokální DB. Falešná „applied" přeřadí na `failed`, nesmyslná URL na `skipped`.

---

## Diagnostika a debug složky

Když cokoli selže, bot **místo tichého returnu ukládá diagnostiku**:

- **`debug_apply_failures/<timestamp>_<title>_<hash>/`**
  - `page.html` — celý DOM formuláře v okamžiku selhání
  - `screenshot.png` — full-page screenshot prohlížeče
  - `error.txt` — důvod selhání + URL + stacktrace
- **`debug_jobs_history/<timestamp>_<tag>.{html,png}`**
  - snapshot stránky „Historie odpovědí", když fetch vrátí 0 URL

V GUI se u každého FAILu do logu vypíše cesta ke konkrétní složce.

---

## Bezpečnost & privátní data

**Nikdy se do gitu nepushují:**

- `.env` (API klíče, IMAP heslo)
- `storage-state.json` (session cookies — v podstatě přihlašovací token)
- `profiles.json` (jméno, e-mail, telefon, mzda)
- `jobhunter.db` (historie, e-maily)
- `debug_apply_failures/`, `debug_jobs_history/` (screenshoty, HTML s osobními daty)

Vše je v `.gitignore`. Když repo klonuješ, dostaneš jen kód — svoje `.env` a session si musíš udělat sám.

---

## Odpovědné používání

- **Nespammuj.** Rozumný limit je ~20–50 inzerátů denně.
- **Respektuj ToS** Jobs.cz / Alma Career. Bot je automat a weby to v ToS ne vždy povolují — používej na vlastní odpovědnost.
- **Dry run je tvůj kamarád.** Prvních X běhů nech `--dry-run` zapnutý, ať vidíš, co to dělá.
- Když narazíš na formulář, co bot neumí vyplnit, nepokoušej se to obcházet hrubou silou — otevři issue nebo si ho vyplň ručně.

---

## Problémy a troubleshooting

**„Nejsem přihlášený, i když jsem"**
Session propršela. Klikni v GUI **Login do Jobs.cz** (nebo `python main.py init-session`). Nová `storage-state.json` se uloží.

**„Pole jméno se nevyplnilo"**
Otevři `debug_apply_failures/<složka>/page.html`, najdi `<input>` pro jméno a pošli issue s názvem `name=/id=/placeholder=`. Přidáme selektor.

**„Checkboxy se zaškrtnou, pak hned odškrtnou"**
Starší bug — fix už je v `_check_application_consents` (label fallback čte `input.checked` před klikem).

**„Gemini 404 / model not found"**
Stará jména modelů se mapují automaticky na aktuální (`gemini-2.5-flash`). Když jsi přepsal `.env` ručně, dej tam `GEMINI_MODEL=gemini-2.5-flash`.

**„FAIL odeslání: není vidět tlačítko Odpovědět"**
Typicky překrytá cookie lišta nebo jiný jazyk. V `debug_apply_failures/` uvidíš screenshot — bud přidej pattern do `_submit_in_frame`, nebo na tu stránku si otevři browser ručně a zavři cookies.

**GUI nezobrazuje české znaky**
Zkontroluj, že máš Windows Terminal / PowerShell s UTF-8 kódováním. `Start.bat` to nastavuje přes `chcp 65001`.

---

## AUTO mode (smyčka přes noc)

Pro kamoše nebo pro dávkové noční běhy je v repu **dvoudílné řešení**:

- **`AUTO.bat`** (v kořeni repa) — dvojkliknutím otevře viditelné okno a pouští smyčku:
  - max 16 iterací, mezi nimi 40min pauza, tvrdý stop v 08:00
  - každá iterace: `python main.py run --max-apply 30 --min-fit 50 --pause-seconds 20 --max-consecutive-fails 5`
  - Windows sleep je uzamčen (SetThreadExecutionState API, bez potřeby admina)
  - auto-stop po 3 iteracích v řadě bez jediného odeslání (došla nabídka)
- **`WATCH.bat`** — otevře druhé okno s **live tail** nejnovějšího `tools/night_logs/run_XX_*.log`. Vidíš každý SKIP / OK / FAIL v reálném čase bez nutnosti otevírat hlavní okno smyčky.

Typický workflow:

```text
1. dvojklik AUTO.bat     → okno 1: smyčka běží
2. dvojklik WATCH.bat    → okno 2: live log
3. jdi spát              → počítač nespí (sleep zamčen Pythonem)
4. ráno zkoukni výsledky  → tools/night_logs/run_01..NN_*.log
```

Konfigurace smyčky je v `tools/night_loop.py`:

```python
MAX_ITERATIONS = 16
SLEEP_BETWEEN_RUNS_SECONDS = 40 * 60
STOP_HOUR = 8
STOP_MINUTE = 0
RUN_ARGS = ["run", "--limit", "2000", "--max-apply", "30", ...]
```

Uprav, commitni, kamoš pullne a jede.

---

## Autor

**Marek Šolc** — [WuxCZ](https://github.com/WuxCZ)

Made by Wux with ♥

PR / issue vítány.
