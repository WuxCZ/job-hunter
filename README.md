# JobHunter Auto Apply Bot

Automatizace pro hledani nabidek na Jobs.cz, odesilani CV a kontrolu odpovedi v e-mailu.

## Co to umi

- nacte nabidky z URL filtru Jobs.cz
- udrzuje SQLite databazi odeslanych reakci
- hlida duplicity, aby se neposilalo znovu na stejnou pozici
- umi zkontrolovat inbox pres IMAP a oznacit odpovedi
- umi vygenerovat kratkou zpravu pres Gemini API
- umi otevrit Jobs.cz v Playwright a pokusit se automaticky odeslat odpoved

## Rychly start

1. Nainstaluj Python 3.11+
2. Vytvor virtualni prostredi a nainstaluj zavislosti:
   - `python -m venv .venv`
   - `.venv\Scripts\activate`
   - `pip install -r requirements.txt`
   - `playwright install chromium`
3. Vytvor `.env` podle `.env.example`
4. Uloz prihlaseni do Jobs.cz:
   - `python main.py init-session`
   - v otevrenem okne se prihlas a okno zavri
5. Spust automat:
   - `python main.py run --limit 20`

## GUI rezim (doporuceno)

- Spust panel: `python main.py gui`
- Rezimy:
  - `Manual approval`: pred kazdym odeslanim schvalis / preskocis / stopnes beh
  - `Auto`: bezi automaticky podle aktualniho nastaveni
- V GUI uvidis:
  - fit score (0-100) + duvod shody
  - preview zpravy
  - live log udalosti
  - historii odeslanych/objevenych pozic z DB
- `Dry run` nech zapnuty, dokud nepotvrdis, ze vse funguje.

## Co je nove v GUI

- tmavy "modern" vzhled (dashboard + nastaveni)
- podpora vice lidi pres profily (`profiles.json`)
- pri prvnim spusteni se vytvori default profil
- kazdy profil ma povinne vlastni CV (PDF), bez nej nelze spustit odesilani
- CV lze kdykoli zmenit tlacitkem `Nahrat / zmenit CV`
- nastaveni filtru: lokalita, hledany vyraz (napr. IT), radius
- login a logout do Jobs.cz primo z GUI
- pri kazde nalezene pozici lze otevrit preview na druhem monitoru (pokud je detekovan)

## Poznamky

- Pouzivej `--dry-run`, dokud neoveris, ze vse funguje spravne.
- Struktura Jobs.cz se muze menit, selektory je pak potreba upravit.
- Dodrzuj podminky webu a neposilej hromadny spam.
