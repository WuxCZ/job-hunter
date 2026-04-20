from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict

from jobhunter_bot.ai import build_message
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

        sm = max(0, min(10_000, int(browser_slow_mo_ms)))
        if sm:
            print(f"Debug: prohlížeč slow-mo={sm} ms")
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
        if ok:
            if not dry_run:
                db.mark_applied(listing)
                applied_count += 1
                print(f"OK: {listing.title}")
            else:
                print(f"DRY RUN (nic neodeslano): {listing.title}")
        else:
            failed_count += 1
            print(f"FAIL: {listing.title} — {apply_err}")

    print(
        f"Hotovo | applied={applied_count} skipped={skipped_count} failed={failed_count} dry_run={dry_run}"
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
        cmd_run(
            args.limit,
            args.dry_run,
            args.browser_slow_mo,
            ignore_db=getattr(args, "ignore_db", False),
            skip_gemini_form_check=getattr(args, "no_gemini_form_check", False),
            headless=getattr(args, "headless", False),
        )
    elif args.command == "gui":
        launch_gui()
    elif args.command == "show-config":
        cmd_show_config()
    else:
        parser.print_help()
