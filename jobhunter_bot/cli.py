from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict

import time

from jobhunter_bot.ai import build_message, evaluate_fit
from jobhunter_bot.browser_apply import apply_to_job, init_session
from jobhunter_bot.config import load_config
from jobhunter_bot.db import Database
from jobhunter_bot.email_monitor import poll_inbox
from jobhunter_bot.gui import launch_gui
from jobhunter_bot.jobs_history import fetch_applied_rpd_urls
from jobhunter_bot.scraper import scrape_jobs
from jobhunter_bot.urlnorm import normalize_job_url


def cmd_init_session() -> None:
    cfg = load_config()
    init_session(cfg.storage_state_path)
    print("Session ulozena.")


def cmd_scrape(limit: int) -> None:
    cfg = load_config()
    db = Database(cfg.db_path)
    listings = scrape_jobs(cfg.jobs_search_url, cfg.request_timeout_seconds, max_listings=limit)
    for listing in listings:
        db.upsert_listing(listing)
        print(f"- {listing.title} | {listing.company} | {listing.url}")
    print(f"Nacteno: {len(listings)}")


def cmd_check_mail(limit: int) -> None:
    cfg = load_config()
    db = Database(cfg.db_path)
    matched = poll_inbox(
        db=db,
        host=cfg.imap_host,
        port=cfg.imap_port,
        username=cfg.imap_user,
        password=cfg.imap_password,
        folder=cfg.imap_folder,
        limit=limit,
    )
    print(f"Spárováno odpovědí: {matched}")


def cmd_run(
    limit: int,
    dry_run: bool,
    browser_slow_mo_ms: int = 0,
    *,
    ignore_db: bool = False,
    skip_gemini_form_check: bool = False,
    headless: bool = False,
    safe_mode: bool = True,
    min_fit: int = 50,
    max_apply: int = 50,
    pause_seconds: int = 15,
    max_consecutive_fails: int = 5,
    auto_recover_after_fail: bool = False,
) -> None:
    cfg = load_config()
    db = Database(cfg.db_path)

    if not dry_run:
        poll_inbox(
            db=db,
            host=cfg.imap_host,
            port=cfg.imap_port,
            username=cfg.imap_user,
            password=cfg.imap_password,
            folder=cfg.imap_folder,
            limit=150,
        )

        history_log: list[str] = []
        site_applied_urls = fetch_applied_rpd_urls(cfg.storage_state_path, log=history_log)
        for line in history_log:
            print(f"Historie Jobs.cz: {line}")
        print(f"Historie Jobs.cz (účet): {len(site_applied_urls)} URL")
    else:
        site_applied_urls = set()
        print("DRY RUN — přeskočeny inbox a historie Jobs.cz (žádné zápisy do DB)")
        if ignore_db:
            print("DRY RUN — --ignore-db: přeskakuji kontrolu duplicit v lokální DB")

    # 2) Scrape latest listings.
    listings = scrape_jobs(cfg.jobs_search_url, cfg.request_timeout_seconds, max_listings=limit)
    applied_count = 0
    skipped_count = 0
    failed_count = 0

    if headless:
        print("Headless prohlížeč (bez okna)")

    if safe_mode:
        print(
            f"Safe mode: min fit={min_fit}, max odeslání={max_apply}, "
            f"pauza={pause_seconds}s, stop po {max_consecutive_fails} FAILech v řadě."
        )
    else:
        print("Safe mode VYPNUTÝ (žádné brzdy) — používáš na vlastní riziko.")

    apply_attempts = 0
    consecutive_fails = 0

    for listing in listings:
        if normalize_job_url(listing.url) in site_applied_urls:
            if not dry_run:
                db.mark_skipped(listing)
            skipped_count += 1
            print(f"SKIP (Jobs.cz historie): {listing.title}")
            continue

        if not dry_run:
            db.upsert_listing(listing)

        if not (dry_run and ignore_db) and db.should_skip_listing(listing.url):
            skipped_count += 1
            print(f"SKIP (duplicitni): {listing.title}")
            continue

        # --- Safe-mode brzdy PŘED voláním browseru (úspora času) ---
        score, reason, _fit_details = evaluate_fit(listing)
        if safe_mode and score < min_fit:
            if not dry_run:
                db.mark_skipped(listing)
            skipped_count += 1
            print(f"SKIP (fit {score} < min {min_fit}): {listing.title}")
            continue

        if safe_mode and apply_attempts >= max_apply:
            print(f"Safe mode: dosažen limit max odeslání = {max_apply}. Ukončuji běh.")
            break

        if safe_mode and apply_attempts > 0 and pause_seconds > 0:
            print(f"Safe mode: pauza {pause_seconds}s před dalším pokusem…")
            time.sleep(pause_seconds)

        sm = max(0, min(10_000, int(browser_slow_mo_ms)))
        if sm:
            print(f"Debug: prohlížeč slow-mo={sm} ms")
        leave_browser_on_fail = os.environ.get(
            "JOBHUNTER_LEAVE_BROWSER_ON_FAIL", ""
        ).strip().lower() in ("1", "true", "yes", "on")
        email_apply = os.getenv("APPLICANT_EMAIL", "").strip() or (cfg.imap_user or "").strip()
        name_apply = os.getenv("APPLICANT_FULL_NAME", "").strip()
        message = build_message(
            cfg.gemini_api_key,
            cfg.gemini_model,
            listing,
            sender_name=name_apply,
        )
        apply_info: list[str] = []
        try:
            ok, apply_err = apply_to_job(
                listing=listing,
                cv_path=cfg.cv_path,
                storage_state_path=cfg.storage_state_path,
                message=message,
                dry_run=dry_run,
                browser_slow_mo_ms=sm,
                applicant_full_name=name_apply,
                applicant_email=email_apply,
                applicant_phone=os.getenv("APPLICANT_PHONE", "").strip(),
                applicant_salary=os.getenv("APPLICANT_SALARY", "").strip(),
                gemini_api_key=cfg.gemini_api_key,
                gemini_model=cfg.gemini_model,
                info_log=apply_info,
                skip_gemini_form_check=skip_gemini_form_check,
                headless=headless,
                leave_browser_open_on_failure=leave_browser_on_fail,
            )
        except Exception as apply_exc:
            for line in apply_info:
                print(line)
            failed_count += 1
            print(
                f"FAIL (výjimka): {listing.title} — "
                f"{apply_exc.__class__.__name__}: {apply_exc}"
            )
            try:
                db.record_apply_failure(
                    listing, f"crash: {apply_exc.__class__.__name__}: {apply_exc}"
                )
            except Exception:
                pass
            continue
        for line in apply_info:
            print(line)
        apply_attempts += 1

        # Retry once at server error (jobs.cz občas vrátí "We ran into a problem")
        if not ok and apply_err and "server chyba" in apply_err.lower() and not dry_run:
            print(f"Retry za 60s: {listing.title} (server chyba jobs.cz)")
            time.sleep(60)
            retry_info: list[str] = []
            try:
                ok, apply_err = apply_to_job(
                    listing=listing,
                    cv_path=cfg.cv_path,
                    storage_state_path=cfg.storage_state_path,
                    message=message,
                    dry_run=dry_run,
                    browser_slow_mo_ms=sm,
                    applicant_full_name=name_apply,
                    applicant_email=email_apply,
                    applicant_phone=os.getenv("APPLICANT_PHONE", "").strip(),
                    applicant_salary=os.getenv("APPLICANT_SALARY", "").strip(),
                    gemini_api_key=cfg.gemini_api_key,
                    gemini_model=cfg.gemini_model,
                    info_log=retry_info,
                    skip_gemini_form_check=True,
                    headless=headless,
                    leave_browser_open_on_failure=False,
                )
            except Exception as retry_exc:
                ok = False
                apply_err = f"retry selhal: {retry_exc}"
            for line in retry_info:
                print(f"[retry] {line}")

        if auto_recover_after_fail and not ok and not dry_run and apply_err:
            print(f"Auto-obnova: druhý pokus za 6s — {listing.title}")
            time.sleep(6)
            recover_info: list[str] = []
            recover_sm = max(sm, 350)
            try:
                ok, apply_err = apply_to_job(
                    listing=listing,
                    cv_path=cfg.cv_path,
                    storage_state_path=cfg.storage_state_path,
                    message=message,
                    dry_run=dry_run,
                    browser_slow_mo_ms=recover_sm,
                    applicant_full_name=name_apply,
                    applicant_email=email_apply,
                    applicant_phone=os.getenv("APPLICANT_PHONE", "").strip(),
                    applicant_salary=os.getenv("APPLICANT_SALARY", "").strip(),
                    gemini_api_key=cfg.gemini_api_key,
                    gemini_model=cfg.gemini_model,
                    info_log=recover_info,
                    skip_gemini_form_check=True,
                    headless=headless,
                    leave_browser_open_on_failure=False,
                )
            except Exception as rec_exc:
                ok = False
                apply_err = f"auto-obnova selhala: {rec_exc}"
            for line in recover_info:
                print(f"[auto-obnova] {line}")

        if ok:
            if not dry_run:
                db.mark_applied(listing)
                applied_count += 1
                consecutive_fails = 0
                print(f"OK: {listing.title} (fit {score})")
            else:
                print(f"DRY RUN (nic neodeslano): {listing.title}")
        else:
            failed_count += 1
            consecutive_fails += 1
            print(f"FAIL: {listing.title} (fit {score}) — {apply_err}")
            if safe_mode and consecutive_fails >= max_consecutive_fails:
                print(
                    f"Safe mode: {consecutive_fails} FAILů v řadě → HARD STOP. "
                    "Zkontroluj diagnostiku v debug_apply_failures/."
                )
                break

    print(
        f"Hotovo | applied={applied_count} skipped={skipped_count} "
        f"failed={failed_count} dry_run={dry_run}"
    )


def cmd_show_config() -> None:
    cfg = load_config()
    safe = asdict(cfg)
    if safe["gemini_api_key"]:
        safe["gemini_api_key"] = "***"
    if safe["imap_password"]:
        safe["imap_password"] = "***"
    print(safe)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="JobHunter auto-apply bot")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-session")

    scrape_p = sub.add_parser("scrape")
    scrape_p.add_argument("--limit", type=int, default=30)

    mail_p = sub.add_parser("check-mail")
    mail_p.add_argument("--limit", type=int, default=200)

    run_p = sub.add_parser("run")
    run_p.add_argument("--limit", type=int, default=30)
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument(
        "--browser-slow-mo",
        type=int,
        default=0,
        metavar="MS",
        help="Zpomalit Playwright (ms mezi akcemi), 0=vypnuto; vhodné pro sledování odeslání",
    )
    run_p.add_argument(
        "--ignore-db",
        action="store_true",
        help="Jen s --dry-run: neřešit duplicity v lokální DB (vhodné pro dávkový test)",
    )
    run_p.add_argument(
        "--no-gemini-form-check",
        action="store_true",
        help="Nepoužít Gemini kontrolu formuláře (ušetří API při stovkách pokusů)",
    )
    run_p.add_argument(
        "--headless",
        action="store_true",
        help="Chromium bez okna (rychlejší dávkový dry-run)",
    )
    run_p.add_argument(
        "--no-safe-mode",
        action="store_true",
        help="Vypne bezpečnostní limity (rate-limit, min-fit, hard-stop). Používej opatrně.",
    )
    run_p.add_argument("--min-fit", type=int, default=50, help="Safe mode: min fit score pro odeslání")
    run_p.add_argument("--max-apply", type=int, default=50, help="Safe mode: max odeslání za běh")
    run_p.add_argument("--pause-seconds", type=int, default=15, help="Safe mode: pauza mezi pokusy")
    run_p.add_argument(
        "--max-consecutive-fails",
        type=int,
        default=5,
        help="Safe mode: hard stop po N FAILech v řadě",
    )
    run_p.add_argument(
        "--auto-recover",
        action="store_true",
        help="Po FAIL jednou zopakovat celé apply (bez Gemini kontroly; pomalejší slow-mo)",
    )

    sub.add_parser("gui")
    sub.add_parser("show-config")
    return parser


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = build_parser()
    args = parser.parse_args()

    if args.command == "init-session":
        cmd_init_session()
    elif args.command == "scrape":
        cmd_scrape(args.limit)
    elif args.command == "check-mail":
        cmd_check_mail(args.limit)
    elif args.command == "run":
        if getattr(args, "ignore_db", False) and not args.dry_run:
            print("--ignore-db je povolené jen s --dry-run.")
            sys.exit(2)
        env_auto_recover = os.environ.get("JOBHUNTER_AUTO_RECOVER", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        cmd_run(
            args.limit,
            args.dry_run,
            args.browser_slow_mo,
            ignore_db=getattr(args, "ignore_db", False),
            skip_gemini_form_check=getattr(args, "no_gemini_form_check", False),
            headless=getattr(args, "headless", False),
            safe_mode=not getattr(args, "no_safe_mode", False),
            min_fit=getattr(args, "min_fit", 50),
            max_apply=getattr(args, "max_apply", 50),
            pause_seconds=getattr(args, "pause_seconds", 15),
            max_consecutive_fails=getattr(args, "max_consecutive_fails", 5),
            auto_recover_after_fail=bool(getattr(args, "auto_recover", False) or env_auto_recover),
        )
    elif args.command == "gui":
        launch_gui()
    elif args.command == "show-config":
        cmd_show_config()
    else:
        parser.print_help()
