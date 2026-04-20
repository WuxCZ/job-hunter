from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from jobhunter_bot.ai import build_message, evaluate_fit, stream_job_panel_summary
from jobhunter_bot.browser_apply import apply_to_job, init_session
from jobhunter_bot.config import AppConfig, load_config
from jobhunter_bot.db import Database
from jobhunter_bot.email_monitor import poll_inbox
from jobhunter_bot.jobs_history import fetch_applied_rpd_urls
from jobhunter_bot.preview import ListingPreviewer, open_listing_preview, terminate_listing_preview
from jobhunter_bot.urlnorm import normalize_job_url
from jobhunter_bot.profiles import ProfileStore, UserProfile
from jobhunter_bot.scraper import build_jobs_search_url, fetch_job_detail, scrape_jobs


class JobHunterModernGUI:
    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.root.title("JobHunter Pro")
        self.root.geometry("1320x830")

        self.cfg: AppConfig = load_config()
        self.db = Database(self.cfg.db_path)
        self.profile_store = ProfileStore()
        self.profiles, self.active_profile_name = self.profile_store.load(self.cfg)
        self.previewer = ListingPreviewer()
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.pending_decision: str | None = None
        self.waiting_for_decision = threading.Event()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self._failure_reason_full: dict[str, str] = {}
        self._preview_proc = None
        self._preview_profile_dir: Path | None = None

        self.profile_var = ctk.StringVar(value=self.active_profile_name)
        self.locality_var = ctk.StringVar(value="")
        self.query_var = ctk.StringVar(value="")
        self.radius_var = ctk.IntVar(value=30)
        self.cv_var = ctk.StringVar(value="")
        self.open_preview_var = ctk.BooleanVar(value=True)
        self.mode_var = ctk.StringVar(value="manual")
        self.limit_var = ctk.IntVar(value=20)
        self.dry_run_var = ctk.BooleanVar(value=True)
        self.dry_run_ignore_db_var = ctk.BooleanVar(value=False)
        self.browser_debug_var = ctk.BooleanVar(value=False)
        self.browser_slow_mo_var = ctk.StringVar(value="600")
        self.applicant_name_var = ctk.StringVar(value="")
        self.applicant_email_var = ctk.StringVar(value="")
        self.applicant_phone_var = ctk.StringVar(value="")
        self.applicant_salary_var = ctk.StringVar(value="50000")
        # --- Safe mode: rozumné brzdy, aby bot nedělal spam a nespadl do banu ---
        self.safe_mode_var = ctk.BooleanVar(value=True)
        self.min_fit_var = ctk.IntVar(value=50)
        self.max_apply_var = ctk.IntVar(value=50)
        self.pause_seconds_var = ctk.IntVar(value=15)
        self.max_consecutive_fails_var = ctk.IntVar(value=5)
        self.status_var = ctk.StringVar(value="Připraven")

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self._build_ui()
        self._load_active_profile_to_form()
        self._load_history(log_result=False)
        self._load_failures(log_result=False)
        self._pump_events()
        self.root.after(400, self._enforce_cv_on_first_run)

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self.root, corner_radius=12)
        header.pack(fill=tk.X, padx=14, pady=(12, 10))

        ctk.CTkLabel(header, text="Profil", font=ctk.CTkFont(size=15, weight="bold")).pack(
            side=tk.LEFT, padx=(12, 8), pady=10
        )
        self.profile_combo = ctk.CTkComboBox(
            header,
            values=[p.name for p in self.profiles],
            variable=self.profile_var,
            width=220,
            command=lambda _value: self._on_profile_changed(),
        )
        self.profile_combo.pack(side=tk.LEFT, pady=10)

        ctk.CTkButton(
            header,
            text="Uložit profil",
            command=lambda: self._save_profile_from_form(confirm_dialog=False),
            width=120,
        ).pack(side=tk.LEFT, padx=8)
        ctk.CTkButton(header, text="Logout Jobs", command=self._logout_jobs, width=120).pack(side=tk.LEFT, padx=8)
        self.status_chip = ctk.CTkLabel(
            header,
            textvariable=self.status_var,
            fg_color="#1f6aa5",
            corner_radius=8,
            width=130,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.status_chip.pack(side=tk.RIGHT, padx=12, pady=10)

        self.tabs = ctk.CTkTabview(self.root, corner_radius=12)
        self.tabs.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 14))
        self.tabs.add("Dashboard")
        self.tabs.add("Selhání")
        self.tabs.add("Nastavení")

        self._build_dashboard(self.tabs.tab("Dashboard"))
        self._build_failures_tab(self.tabs.tab("Selhání"))
        self._build_settings(self.tabs.tab("Nastavení"))

    def _build_dashboard(self, parent: ctk.CTkFrame) -> None:
        controls = ctk.CTkFrame(parent, corner_radius=10)
        controls.pack(fill=tk.X, padx=8, pady=8)

        ctk.CTkLabel(controls, text="Režim", font=ctk.CTkFont(size=14, weight="bold")).pack(
            side=tk.LEFT, padx=(12, 8), pady=10
        )
        ctk.CTkSegmentedButton(
            controls,
            values=["manual", "auto"],
            variable=self.mode_var,
            width=170,
        ).pack(side=tk.LEFT, padx=(0, 12))

        ctk.CTkLabel(controls, text="Limit").pack(side=tk.LEFT)
        ctk.CTkEntry(controls, textvariable=self.limit_var, width=58).pack(side=tk.LEFT, padx=(6, 12))
        ctk.CTkCheckBox(controls, text="Dry run", variable=self.dry_run_var).pack(side=tk.LEFT, padx=(0, 8))
        ctk.CTkCheckBox(
            controls,
            text="Dry: ignorovat duplicity v DB",
            variable=self.dry_run_ignore_db_var,
        ).pack(side=tk.LEFT, padx=(0, 12))
        ctk.CTkCheckBox(controls, text="Preview na 2. monitor", variable=self.open_preview_var).pack(
            side=tk.LEFT, padx=(0, 12)
        )
        ctk.CTkButton(controls, text="Start", command=self.start, width=90).pack(side=tk.LEFT, padx=4)
        ctk.CTkButton(controls, text="Stop", command=self.stop, width=90, fg_color="#a93f55").pack(side=tk.LEFT, padx=4)
        ctk.CTkButton(
            controls,
            text="Obnovit historii",
            command=self._on_refresh_history_clicked,
            width=130,
        ).pack(side=tk.LEFT, padx=6)

        debug_row = ctk.CTkFrame(parent, fg_color="transparent")
        debug_row.pack(fill=tk.X, padx=8, pady=(0, 2))
        ctk.CTkCheckBox(
            debug_row,
            text="Debug: zpomalit prohlížeč (slow-mo)",
            variable=self.browser_debug_var,
        ).pack(side=tk.LEFT, padx=(4, 8))
        ctk.CTkLabel(debug_row, text="ms:").pack(side=tk.LEFT)
        ctk.CTkEntry(debug_row, textvariable=self.browser_slow_mo_var, width=56).pack(side=tk.LEFT, padx=(4, 12))

        safe_row = ctk.CTkFrame(parent, fg_color="transparent")
        safe_row.pack(fill=tk.X, padx=8, pady=(4, 6))
        ctk.CTkCheckBox(
            safe_row,
            text="Safe mode (doporučeno)",
            variable=self.safe_mode_var,
        ).pack(side=tk.LEFT, padx=(4, 14))
        ctk.CTkLabel(safe_row, text="min fit").pack(side=tk.LEFT)
        ctk.CTkEntry(safe_row, textvariable=self.min_fit_var, width=48).pack(side=tk.LEFT, padx=(4, 10))
        ctk.CTkLabel(safe_row, text="max odeslání").pack(side=tk.LEFT)
        ctk.CTkEntry(safe_row, textvariable=self.max_apply_var, width=54).pack(side=tk.LEFT, padx=(4, 10))
        ctk.CTkLabel(safe_row, text="pauza mezi pokusy (s)").pack(side=tk.LEFT)
        ctk.CTkEntry(safe_row, textvariable=self.pause_seconds_var, width=48).pack(side=tk.LEFT, padx=(4, 10))
        ctk.CTkLabel(safe_row, text="stop po FAILech v řadě").pack(side=tk.LEFT)
        ctk.CTkEntry(safe_row, textvariable=self.max_consecutive_fails_var, width=40).pack(
            side=tk.LEFT, padx=(4, 10)
        )

        content = ctk.CTkFrame(parent, corner_radius=10)
        content.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))
        content.grid_columnconfigure(0, weight=0)
        content.grid_columnconfigure(1, weight=3)
        content.grid_columnconfigure(2, weight=2)
        content.grid_rowconfigure(1, weight=1)

        summary_frame = ctk.CTkFrame(content, corner_radius=10, width=300)
        summary_frame.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(10, 6), pady=10)
        summary_frame.grid_propagate(False)
        ctk.CTkLabel(
            summary_frame,
            text="Souhrn pozice",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=10, pady=(8, 6))
        self.summary_box = ctk.CTkTextbox(
            summary_frame,
            width=280,
            wrap="word",
            font=ctk.CTkFont(family="Segoe UI", size=15),
            text_color=("#e8eef8", "#e8eef8"),
            activate_scrollbars=True,
        )
        self.summary_box.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.summary_box.insert("1.0", "Po načtení nabídky se zde zobrazí firma, lokalita a stručný popis.")
        self.summary_box.configure(state="disabled")

        pending = ctk.CTkFrame(content, corner_radius=10)
        pending.grid(row=0, column=1, sticky="nsew", padx=(6, 6), pady=10)
        ctk.CTkLabel(pending, text="Čeká na schválení", font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=10, pady=(8, 6)
        )
        self.pending_box = ctk.CTkTextbox(pending, height=110, wrap="word", font=ctk.CTkFont(size=14))
        self.pending_box.pack(fill=tk.X, padx=10, pady=(0, 6))
        self.pending_box.insert("1.0", "Žádná čekající položka")
        self.pending_box.configure(state="disabled")

        ctk.CTkLabel(
            pending,
            text="Odpověď, která se odešle do formuláře",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=10, pady=(2, 2))
        self.pending_message_box = ctk.CTkTextbox(
            pending,
            height=160,
            wrap="word",
            font=ctk.CTkFont(size=14),
        )
        self.pending_message_box.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.pending_message_box.insert("1.0", "")
        self.pending_message_box.configure(state="disabled")

        action_row = ctk.CTkFrame(pending, fg_color="transparent")
        action_row.pack(fill=tk.X, padx=10, pady=(0, 10))
        ctk.CTkButton(action_row, text="Schválit", command=lambda: self._set_decision("approve"), width=120).pack(
            side=tk.LEFT, padx=4
        )
        ctk.CTkButton(action_row, text="Přeskočit", command=lambda: self._set_decision("skip"), width=120).pack(
            side=tk.LEFT, padx=4
        )
        ctk.CTkButton(
            action_row,
            text="Stop vše",
            command=lambda: self._set_decision("stop"),
            width=120,
            fg_color="#a93f55",
        ).pack(side=tk.LEFT, padx=4)

        log_frame = ctk.CTkFrame(content, corner_radius=10)
        log_frame.grid(row=1, column=1, sticky="nsew", padx=(6, 6), pady=(0, 10))
        ctk.CTkLabel(log_frame, text="Živý log", font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=10, pady=(8, 6)
        )
        self.log = ctk.CTkTextbox(log_frame, wrap="word", font=ctk.CTkFont(size=14))
        self.log.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        hist_frame = ctk.CTkFrame(content, corner_radius=10)
        hist_frame.grid(row=0, column=2, rowspan=2, sticky="nsew", padx=(6, 10), pady=10)
        ctk.CTkLabel(hist_frame, text="Historie", font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=10, pady=(8, 6)
        )

        tree_host = tk.Frame(hist_frame, bg="#1a1a1a")
        tree_host.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        style = ttk.Style(tree_host)
        style.theme_use("clam")
        style.configure("Modern.Treeview", background="#1f1f1f", foreground="#f0f0f0", fieldbackground="#1f1f1f")
        style.configure("Modern.Treeview.Heading", background="#2f2f2f", foreground="#67b0ff")
        self.tree = ttk.Treeview(
            tree_host,
            columns=("status", "title", "company"),
            show="headings",
            style="Modern.Treeview",
        )
        self.tree.heading("status", text="Stav")
        self.tree.heading("title", text="Pozice")
        self.tree.heading("company", text="Firma")
        self.tree.column("status", width=95, anchor="center")
        self.tree.column("title", width=330)
        self.tree.column("company", width=210)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self._tree_host = tree_host

    def _build_failures_tab(self, parent: ctk.CTkFrame) -> None:
        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.pack(fill=tk.X, padx=10, pady=(10, 6))
        ctk.CTkLabel(
            top,
            text="Selhání odeslání — každý FAIL se ukládá (DB + složka diagnostiky u příspěvku v logu).",
            font=ctk.CTkFont(size=14),
            anchor="w",
        ).pack(side=tk.LEFT, padx=(0, 12))
        ctk.CTkButton(top, text="Obnovit", command=self._on_refresh_failures_clicked, width=100).pack(
            side=tk.RIGHT, padx=4
        )
        ctk.CTkButton(
            top,
            text="Vymazat seznam",
            command=self._on_clear_failures_clicked,
            width=130,
            fg_color="#6b3a44",
        ).pack(side=tk.RIGHT, padx=4)

        fh = tk.Frame(parent, bg="#1a1a1a")
        fh.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 12))
        st = ttk.Style(fh)
        st.theme_use("clam")
        st.configure("Fail.Treeview", background="#1f1f1f", foreground="#f0f0f0", fieldbackground="#1f1f1f")
        st.configure("Fail.Treeview.Heading", background="#3a2a2a", foreground="#ff8a8a")
        self.fail_tree = ttk.Treeview(
            fh,
            columns=("when", "title", "company", "reason"),
            show="headings",
            style="Fail.Treeview",
        )
        self.fail_tree.heading("when", text="Kdy (UTC)")
        self.fail_tree.heading("title", text="Pozice")
        self.fail_tree.heading("company", text="Firma")
        self.fail_tree.heading("reason", text="Důvod (zkráceno — dvojklik = detail)")
        self.fail_tree.column("when", width=128, anchor="center")
        self.fail_tree.column("title", width=280)
        self.fail_tree.column("company", width=200)
        self.fail_tree.column("reason", width=520)
        self.fail_tree.pack(fill=tk.BOTH, expand=True)
        self.fail_tree.bind("<Double-1>", self._on_failures_double_click)
        self._failures_host = fh

    def _fmt_utc_short(self, iso_s: str) -> str:
        try:
            dt = datetime.fromisoformat(iso_s.replace("Z", "+00:00"))
            return dt.strftime("%d.%m.%Y %H:%M")
        except Exception:
            return (iso_s or "")[:19]

    def _on_refresh_failures_clicked(self, *_args: object) -> None:
        self._load_failures(log_result=True)

    def _on_clear_failures_clicked(self) -> None:
        if not messagebox.askyesno(
            "Vymazat selhání",
            "Opravdu smazat celý seznam selhání z databáze?",
        ):
            return
        try:
            self.db.clear_apply_failures()
            self._load_failures(log_result=True)
        except Exception as exc:
            messagebox.showerror("Selhání", str(exc))

    def _on_failures_double_click(self, _event: object) -> None:
        sel = self.fail_tree.selection()
        if not sel:
            return
        vals = self.fail_tree.item(sel[0], "values")
        tags = self.fail_tree.item(sel[0], "tags")
        url = tags[0] if tags else ""
        reason_full = getattr(self, "_failure_reason_full", {}).get(sel[0], "")
        if not reason_full and len(vals) >= 4:
            reason_full = vals[3]
        messagebox.showinfo(
            "Detail selhání",
            f"URL:\n{url}\n\nDůvod:\n{reason_full}",
        )

    def _load_failures(self, *, log_result: bool = True) -> None:
        try:
            rows = self.db.get_recent_failures(limit=400)
            for item in self.fail_tree.get_children():
                self.fail_tree.delete(item)
            self._failure_reason_full = {}
            for row in rows:
                rfull = row["reason"] or ""
                rshort = (rfull[:220] + "…") if len(rfull) > 220 else rfull
                iid = self.fail_tree.insert(
                    "",
                    tk.END,
                    values=(
                        self._fmt_utc_short(row["failed_at"] or ""),
                        (row["title"] or "")[:120],
                        (row["company"] or "")[:80],
                        rshort,
                    ),
                    tags=(row["job_url"] or "",),
                )
                self._failure_reason_full[iid] = rfull
            self.fail_tree.yview_moveto(0)
            self.fail_tree.update_idletasks()
            self._failures_host.update_idletasks()
            self.root.update_idletasks()
        except Exception as exc:
            if log_result:
                self._log(f"Selhání: tabulku se nepodařilo obnovit — {exc}")
            return
        if log_result:
            self._log(f"Seznam selhání: {len(rows)} záznamů.")

    def _build_settings(self, parent: ctk.CTkFrame) -> None:
        profile_card = ctk.CTkFrame(parent, corner_radius=10)
        profile_card.pack(fill=tk.X, padx=10, pady=(10, 8))
        ctk.CTkLabel(profile_card, text="Profil a přihlášení", font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, columnspan=5, sticky="w", padx=12, pady=(10, 8)
        )
        ctk.CTkLabel(profile_card, text="Název profilu").grid(row=1, column=0, sticky="w", padx=12, pady=(0, 10))
        ctk.CTkEntry(profile_card, textvariable=self.profile_var, width=220).grid(row=1, column=1, padx=8, pady=(0, 10))
        ctk.CTkButton(profile_card, text="Přidat profil", command=self._add_profile, width=110).grid(
            row=1, column=2, padx=6, pady=(0, 10)
        )
        ctk.CTkButton(profile_card, text="Login do Jobs.cz", command=self._login_jobs, width=130).grid(
            row=1, column=3, padx=6, pady=(0, 10)
        )

        filter_card = ctk.CTkFrame(parent, corner_radius=10)
        filter_card.pack(fill=tk.X, padx=10, pady=8)
        ctk.CTkLabel(filter_card, text="Filtry vyhledávání", font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, columnspan=7, sticky="w", padx=12, pady=(10, 8)
        )
        ctk.CTkLabel(filter_card, text="Lokalita").grid(row=1, column=0, sticky="w", padx=12, pady=(0, 10))
        ctk.CTkEntry(filter_card, textvariable=self.locality_var, width=220).grid(row=1, column=1, padx=8, pady=(0, 10))
        ctk.CTkLabel(filter_card, text="Dotaz").grid(row=1, column=2, sticky="w", padx=8, pady=(0, 10))
        ctk.CTkEntry(filter_card, textvariable=self.query_var, width=180).grid(row=1, column=3, padx=8, pady=(0, 10))
        ctk.CTkLabel(filter_card, text="Radius km").grid(row=1, column=4, sticky="w", padx=8, pady=(0, 10))
        ctk.CTkEntry(filter_card, textvariable=self.radius_var, width=80).grid(row=1, column=5, padx=8, pady=(0, 10))

        cv_card = ctk.CTkFrame(parent, corner_radius=10)
        cv_card.pack(fill=tk.X, padx=10, pady=8)
        ctk.CTkLabel(cv_card, text="CV profilu", font=ctk.CTkFont(size=16, weight="bold")).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=12, pady=(10, 8)
        )
        ctk.CTkLabel(cv_card, text="Cesta k CV (PDF)").grid(row=1, column=0, sticky="w", padx=12, pady=(0, 12))
        ctk.CTkEntry(cv_card, textvariable=self.cv_var).grid(row=1, column=1, sticky="ew", padx=8, pady=(0, 12))
        ctk.CTkButton(cv_card, text="Nahrát / změnit CV", command=self._pick_cv_file, width=160).grid(
            row=1, column=2, padx=8, pady=(0, 12)
        )
        cv_card.grid_columnconfigure(1, weight=1)

        contact_card = ctk.CTkFrame(parent, corner_radius=10)
        contact_card.pack(fill=tk.X, padx=10, pady=8)
        contact_card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            contact_card,
            text="Kontakt v přihláškách (nastav ručně)",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(10, 4))

        help_contact = (
            "Tyto údaje bot vyplní do firemních formulářů (Alma Career, vlastní microsite…) po kliknutí na "
            "„Odpovědět“ na Jobs.cz. Nečtou se z názvu PDF ani z textu CV — musíš je zadat tady.\n\n"
            "Každý profil má vlastní kontakt: přepni profil nahoře, uprav pole a ulož.\n\n"
            "E-mail: když pole necháš prázdné, použije se adresa z pošty v .env (IMAP_USER), případně "
            "proměnná APPLICANT_EMAIL. Telefon často stačí u částiny inzerátů, u jiných je povinný."
        )
        ctk.CTkLabel(
            contact_card,
            text=help_contact,
            font=ctk.CTkFont(size=13),
            text_color=("gray30", "gray65"),
            anchor="w",
            justify="left",
            wraplength=920,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 10))

        ctk.CTkLabel(contact_card, text="Jméno a příjmení").grid(
            row=2, column=0, sticky="nw", padx=12, pady=(0, 4)
        )
        ctk.CTkEntry(
            contact_card,
            textvariable=self.applicant_name_var,
            width=420,
            placeholder_text="např. Marek Šolc",
        ).grid(row=2, column=1, sticky="ew", padx=8, pady=(0, 0))
        ctk.CTkLabel(
            contact_card,
            text="Jak máš v životopisu — včetně diakritiky.",
            font=ctk.CTkFont(size=12),
            text_color=("gray40", "gray60"),
            anchor="w",
        ).grid(row=3, column=1, sticky="w", padx=8, pady=(0, 6))

        ctk.CTkLabel(contact_card, text="E-mail").grid(row=4, column=0, sticky="nw", padx=12, pady=(4, 4))
        ctk.CTkEntry(
            contact_card,
            textvariable=self.applicant_email_var,
            width=420,
            placeholder_text="např. jmeno@email.cz",
        ).grid(row=4, column=1, sticky="ew", padx=8, pady=(4, 0))
        ctk.CTkLabel(
            contact_card,
            text="Volitelné — prázdné = e-mail z konfigurace (.env / IMAP).",
            font=ctk.CTkFont(size=12),
            text_color=("gray40", "gray60"),
            anchor="w",
        ).grid(row=5, column=1, sticky="w", padx=8, pady=(0, 6))

        ctk.CTkLabel(contact_card, text="Telefon").grid(row=6, column=0, sticky="nw", padx=12, pady=(4, 4))
        ctk.CTkEntry(
            contact_card,
            textvariable=self.applicant_phone_var,
            width=420,
            placeholder_text="volitelné, např. +420 123 456 789",
        ).grid(row=6, column=1, sticky="ew", padx=8, pady=(4, 0))
        ctk.CTkLabel(
            contact_card,
            text="Mezinárodní tvar (+420 …) bývá bez chyb.",
            font=ctk.CTkFont(size=12),
            text_color=("gray40", "gray60"),
            anchor="w",
        ).grid(row=7, column=1, sticky="w", padx=8, pady=(0, 8))

        ctk.CTkLabel(contact_card, text="Mzdové očekávání (Kč)").grid(
            row=8, column=0, sticky="nw", padx=12, pady=(4, 4)
        )
        ctk.CTkEntry(
            contact_card,
            textvariable=self.applicant_salary_var,
            width=420,
            placeholder_text="např. 50000",
        ).grid(row=8, column=1, sticky="ew", padx=8, pady=(4, 0))
        ctk.CTkLabel(
            contact_card,
            text=(
                "Použije se jen pokud formulář má pole pro mzdu. Zadávej číslo bez mezer "
                "a bez jednotek (bot doplní hodnotu 1:1). Prázdné = nevyplňovat."
            ),
            font=ctk.CTkFont(size=12),
            text_color=("gray40", "gray60"),
            anchor="w",
            justify="left",
            wraplength=420,
        ).grid(row=9, column=1, sticky="w", padx=8, pady=(0, 8))

        ctk.CTkButton(
            contact_card,
            text="Uložit kontakt a celý profil",
            command=lambda: self._save_profile_from_form(confirm_dialog=True),
            width=280,
        ).grid(row=10, column=0, columnspan=2, sticky="w", padx=12, pady=(4, 14))

    def _log(self, text: str) -> None:
        self.log.insert("end", f"{text}\n")
        self.log.see("end")

    def _set_pending_text(self, text: str) -> None:
        self.pending_box.configure(state="normal")
        self.pending_box.delete("1.0", "end")
        self.pending_box.insert("1.0", text)
        self.pending_box.configure(state="disabled")

    def _set_pending_message(self, text: str) -> None:
        self.pending_message_box.configure(state="normal")
        self.pending_message_box.delete("1.0", "end")
        self.pending_message_box.insert("1.0", text or "")
        self.pending_message_box.configure(state="disabled")

    def _set_pending_payload(self, info: str, message: str) -> None:
        self._set_pending_text(info)
        self._set_pending_message(message)

    def _set_summary_text(self, text: str) -> None:
        self.summary_box.configure(state="normal")
        self.summary_box.delete("1.0", "end")
        self.summary_box.insert("1.0", text)
        self.summary_box.configure(state="disabled")

    def _summary_stream_begin(self) -> None:
        self._summary_first_chunk = True
        self.summary_box.configure(state="normal")
        self.summary_box.delete("1.0", "end")
        self.summary_box.insert("1.0", "⏳ Gemini připravuje souhrn…\n\n")
        self.summary_box.configure(state="disabled")

    def _summary_stream_append(self, text: str) -> None:
        if not text:
            return
        self.summary_box.configure(state="normal")
        if getattr(self, "_summary_first_chunk", False):
            self.summary_box.delete("1.0", "end")
            self._summary_first_chunk = False
        self.summary_box.insert("end", text)
        self.summary_box.see("end")
        self.summary_box.configure(state="disabled")

    def _get_active_profile(self) -> UserProfile:
        profile_name = self.profile_var.get().strip()
        for p in self.profiles:
            if p.name == profile_name:
                return p
        return self.profiles[0]

    def _load_active_profile_to_form(self) -> None:
        p = self._get_active_profile()
        self.locality_var.set(p.locality)
        self.query_var.set(p.query)
        self.radius_var.set(p.radius_km)
        self.cv_var.set(p.cv_path)
        self.profile_var.set(p.name)
        self.applicant_name_var.set(getattr(p, "applicant_full_name", "") or "")
        self.applicant_email_var.set(getattr(p, "applicant_email", "") or "")
        self.applicant_phone_var.set(getattr(p, "applicant_phone", "") or "")
        self.applicant_salary_var.set(str(getattr(p, "applicant_salary", "") or ""))

    def _validate_cv_path(self, cv_path: str, show_message: bool) -> bool:
        if not cv_path:
            if show_message:
                messagebox.showerror("Chybí CV", "Každý profil musí mít nahraný vlastní životopis (PDF).")
            return False
        path = Path(cv_path)
        if path.suffix.lower() != ".pdf":
            if show_message:
                messagebox.showerror("Neplatný formát", "Životopis musí být ve formátu PDF.")
            return False
        if not path.exists():
            if show_message:
                messagebox.showerror("Soubor nenalezen", f"CV nebylo nalezeno:\n{cv_path}")
            return False
        return True

    def _save_profile_from_form(self, confirm_dialog: bool = False) -> bool:
        profile_name = self.profile_var.get().strip()
        if not profile_name:
            messagebox.showwarning("Profil", "Název profilu nesmí být prázdný.")
            return False
        p = self._get_active_profile()
        p.locality = self.locality_var.get().strip() or "brno"
        p.query = self.query_var.get().strip()
        p.radius_km = max(1, int(self.radius_var.get() or 1))
        cv_path = self.cv_var.get().strip()
        if not self._validate_cv_path(cv_path, show_message=True):
            return False
        p.cv_path = cv_path
        p.name = profile_name
        p.applicant_full_name = self.applicant_name_var.get().strip()
        p.applicant_email = self.applicant_email_var.get().strip()
        p.applicant_phone = self.applicant_phone_var.get().strip()
        p.applicant_salary = self.applicant_salary_var.get().strip()
        self.profile_store.save(self.profiles, profile_name)
        self.profile_combo.configure(values=[profile.name for profile in self.profiles])
        self._log(f"Profil uložen: {p.name}")
        if confirm_dialog:
            messagebox.showinfo(
                "Profil uložen",
                "Údaje včetně kontaktu pro přihlášky jsou uložené v souboru profiles.json.",
            )
        return True

    def _on_profile_changed(self) -> None:
        self._load_active_profile_to_form()

    def _add_profile(self) -> None:
        new_name = self.profile_var.get().strip()
        if not new_name:
            messagebox.showwarning("Profil", "Zadej název nového profilu.")
            return
        if any(p.name == new_name for p in self.profiles):
            messagebox.showinfo("Profil", "Profil už existuje.")
            return
        self.profiles.append(
            UserProfile(
                name=new_name,
                cv_path="",
                locality=self.locality_var.get().strip() or "brno",
                query=self.query_var.get().strip() or "IT",
                radius_km=max(1, int(self.radius_var.get() or 1)),
                jobs_storage_state_path=f"storage-state-{new_name.lower().replace(' ', '-')}.json",
                applicant_full_name="",
                applicant_email="",
                applicant_phone="",
                applicant_salary="50000",
            )
        )
        self.profile_combo.configure(values=[p.name for p in self.profiles])
        self.profile_var.set(new_name)
        self.cv_var.set("")
        self.profile_store.save(self.profiles, new_name)
        self._log(f"Vytvořen profil: {new_name} (čeká na nahrání CV)")

    def _pick_cv_file(self) -> None:
        file_path = filedialog.askopenfilename(
            title="Vyber životopis (PDF)",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if file_path:
            self.cv_var.set(file_path)
            self._log("Vybrán nový životopis pro profil.")

    def _login_jobs(self) -> None:
        p = self._get_active_profile()
        if not self._save_profile_from_form():
            return
        if not self._validate_cv_path(p.cv_path, show_message=True):
            return
        init_session(p.jobs_storage_state_path)
        self._log(f"Session uložená pro profil {p.name}")

    def _logout_jobs(self) -> None:
        p = self._get_active_profile()
        path = Path(p.jobs_storage_state_path)
        if path.exists():
            path.unlink()
            self._log(f"Odhlášeno z Jobs.cz: {p.name}")
        else:
            self._log(f"Session nenalezena: {p.jobs_storage_state_path}")

    def _on_refresh_history_clicked(self, *_args: object) -> None:
        """CTkButton může předat argument do callbacku — *args to ignoruje."""
        self._load_history(log_result=True)

    def _load_history(self, *, log_result: bool = True) -> None:
        try:
            rows = self.db.get_recent_applications(limit=500)
            for item in self.tree.get_children():
                self.tree.delete(item)
            for row in rows:
                self.tree.insert(
                    "",
                    tk.END,
                    values=(
                        row["status"] or "",
                        (row["title"] or "")[:240],
                        (row["company"] or "")[:160],
                    ),
                )
            self.tree.yview_moveto(0)
            self.tree.update_idletasks()
            self._tree_host.update_idletasks()
            self.root.update_idletasks()
        except Exception as exc:
            self._log(f"Historie: nepodařilo se obnovit tabulku — {exc}")
            messagebox.showerror("Historie", str(exc))
            return
        if log_result:
            self._log(f"Historie obnovena ({len(rows)} záznamů).")

    def _browser_slow_mo_ms(self) -> int:
        """Playwright slow_mo — zpomalí klikání a další kroky v prohlížeči (0 = vypnuto)."""
        if not self.browser_debug_var.get():
            return 0
        try:
            v = int(str(self.browser_slow_mo_var.get()).strip())
        except ValueError:
            v = 600
        return max(0, min(10_000, v))

    def _close_preview_browser(self) -> None:
        """Zavře okno náhledu inzerátu (Chrome s dočasným profilem), pokud běží."""
        terminate_listing_preview(self._preview_proc, self._preview_profile_dir)
        self._preview_proc = None
        self._preview_profile_dir = None

    def _sync_close_preview_from_worker(self) -> None:
        """Worker vlákno: zavře náhled v hlavním vláknu před Playwright (auto režim)."""
        done = threading.Event()

        def _run() -> None:
            try:
                self._close_preview_browser()
            finally:
                done.set()

        self.root.after(0, _run)
        done.wait(timeout=6.0)

    def _set_decision(self, decision: str) -> None:
        self._close_preview_browser()
        self.pending_decision = decision
        self.waiting_for_decision.set()

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Info", "Run already active.")
            return
        if not self._save_profile_from_form():
            return
        profile = self._get_active_profile()
        if not self._validate_cv_path(profile.cv_path, show_message=True):
            return
        self.stop_event.clear()
        self.status_var.set("Běží")
        self.events.put(("log", f"Start run: profil={profile.name}"))
        self.worker = threading.Thread(target=self._run_worker, daemon=True)
        self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.waiting_for_decision.set()
        self._close_preview_browser()
        self.events.put(("log", "Stop vyžádán uživatelem."))
        self.status_var.set("Zastavuji")

    def _run_worker(self) -> None:
        try:
            dry = bool(self.dry_run_var.get())
            profile = self._get_active_profile()
            if not self._validate_cv_path(profile.cv_path, show_message=False):
                raise ValueError("Profil nemá platné CV v PDF. Nahraj CV v Nastavení.")

            if not dry:
                matched = poll_inbox(
                    db=self.db,
                    host=self.cfg.imap_host,
                    port=self.cfg.imap_port,
                    username=self.cfg.imap_user,
                    password=self.cfg.imap_password,
                    folder=self.cfg.imap_folder,
                    limit=150,
                )
                self.events.put(("log", f"Kontrola inboxu: spárované odpovědi={matched}"))

                history_log: list[str] = []
                site_applied_urls = fetch_applied_rpd_urls(
                    profile.jobs_storage_state_path, log=history_log
                )
                for line in history_log:
                    self.events.put(("log", f"Historie Jobs.cz: {line}"))
                self.events.put(
                    ("log", f"Historie Jobs.cz (účet): nalezeno odpovědí={len(site_applied_urls)}")
                )
                if len(site_applied_urls) == 0:
                    self.events.put(
                        (
                            "log",
                            "Historie Jobs.cz: 0 URL — zkontroluj výše uvedené diagnostické řádky "
                            "(typicky: session propršela → klikni 'Login do Jobs.cz').",
                        )
                    )
            else:
                matched = 0
                site_applied_urls = set()
                self.events.put(
                    (
                        "log",
                        "DRY RUN — žádné zápisy do DB: přeskočeny kontrola inboxu a synchronizace historie Jobs.cz.",
                    )
                )
                if bool(self.dry_run_ignore_db_var.get()):
                    self.events.put(
                        (
                            "log",
                            "DRY RUN — ignoruji duplicity v lokální DB (znovu projdu i dříve zpracované URL).",
                        )
                    )

            search_url = build_jobs_search_url(profile.locality, profile.query, profile.radius_km)
            listings = scrape_jobs(
                search_url,
                self.cfg.request_timeout_seconds,
                max_listings=int(self.limit_var.get() or 20),
            )
            self.events.put(("log", f"Načteno pozic: {len(listings)} | {search_url}"))
            mode = self.mode_var.get()

            # --- Safe-mode parametry (platí i v manual režimu, aby měl uživatel brzdy) ---
            safe_mode = bool(self.safe_mode_var.get())
            min_fit = max(0, int(self.min_fit_var.get() or 0)) if safe_mode else 0
            max_apply = max(1, int(self.max_apply_var.get() or 9999)) if safe_mode else 9999
            pause_seconds = max(0, int(self.pause_seconds_var.get() or 0)) if safe_mode else 0
            max_consecutive_fails = (
                max(1, int(self.max_consecutive_fails_var.get() or 5)) if safe_mode else 10_000
            )
            if safe_mode:
                self.events.put(
                    (
                        "log",
                        f"Safe mode: min fit={min_fit}, max odeslání={max_apply}, "
                        f"pauza={pause_seconds}s, stop po {max_consecutive_fails} FAILech v řadě.",
                    )
                )
            apply_attempts = 0
            consecutive_fails = 0
            import time as _time

            for listing in listings:
                if self.stop_event.is_set():
                    break

                canon = normalize_job_url(listing.url)
                if canon in site_applied_urls:
                    if not dry:
                        self.db.mark_skipped(listing)
                    self.events.put(("log", f"SKIP (už v historii Jobs.cz): {listing.title}"))
                    continue

                if not dry:
                    self.db.upsert_listing(listing)
                ignore_db_dupes = dry and bool(self.dry_run_ignore_db_var.get())
                if not ignore_db_dupes and self.db.should_skip_listing(listing.url):
                    self.events.put(("log", f"SKIP duplicita / DB: {listing.title}"))
                    continue

                detail = fetch_job_detail(listing.url, self.cfg.request_timeout_seconds)
                if not detail.title:
                    detail.title = listing.title
                if not detail.company and listing.company:
                    detail.company = listing.company

                if self.open_preview_var.get():
                    self.events.put(("preview", listing.url))

                self.events.put(("summary_start", ""))
                if self.stop_event.is_set():
                    continue
                try:
                    for chunk in stream_job_panel_summary(
                        self.cfg.gemini_api_key,
                        self.cfg.gemini_model,
                        listing,
                        detail,
                    ):
                        if self.stop_event.is_set():
                            break
                        if chunk:
                            self.events.put(("summary_chunk", chunk))
                except Exception as exc:
                    self.events.put(("summary_chunk", f"(Souhrn: {exc})\n\n" + detail.format_text()))

                score, reason, fit_details = evaluate_fit(listing)

                if safe_mode and score < min_fit:
                    if not dry:
                        self.db.mark_skipped(listing)
                    self.events.put(
                        (
                            "log",
                            f"SKIP (fit {score} < min {min_fit}): {listing.title}",
                        )
                    )
                    self._sync_close_preview_from_worker()
                    continue

                if apply_attempts >= max_apply:
                    self.events.put(
                        (
                            "log",
                            f"Safe mode: dosažen limit max odeslání = {max_apply}. Ukončuji běh.",
                        )
                    )
                    break

                if apply_attempts > 0 and pause_seconds > 0:
                    self.events.put(
                        ("log", f"Safe mode: pauza {pause_seconds}s před dalším pokusem…")
                    )
                    for _ in range(pause_seconds * 2):
                        if self.stop_event.is_set():
                            break
                        _time.sleep(0.5)
                    if self.stop_event.is_set():
                        break

                self._sync_close_preview_from_worker()

                slow_mo = self._browser_slow_mo_ms()
                if slow_mo:
                    self.events.put(("log", f"Debug: prohlížeč slow-mo={slow_mo} ms"))
                email_for_apply = (profile.applicant_email or "").strip() or os.getenv(
                    "APPLICANT_EMAIL", ""
                ).strip() or (self.cfg.imap_user or "").strip()
                name_for_apply = (profile.applicant_full_name or "").strip() or os.getenv(
                    "APPLICANT_FULL_NAME", ""
                ).strip()
                phone_for_apply = (profile.applicant_phone or "").strip() or os.getenv(
                    "APPLICANT_PHONE", ""
                ).strip()
                salary_for_apply = (getattr(profile, "applicant_salary", "") or "").strip() or os.getenv(
                    "APPLICANT_SALARY", ""
                ).strip()
                message = build_message(
                    self.cfg.gemini_api_key,
                    self.cfg.gemini_model,
                    listing,
                    sender_name=name_for_apply,
                )
                if not name_for_apply:
                    self.events.put(
                        (
                            "log",
                            "Varování: chybí jméno pro přihlášku — doplň v Nastavení nebo APPLICANT_FULL_NAME v .env.",
                        )
                    )

                approval_cb = None
                if mode == "manual":
                    def approval_cb() -> str:  # noqa: E306
                        self.pending_decision = None
                        info = (
                            f"{listing.title}\n"
                            f"Firma: {listing.company or detail.company or '-'}\n"
                            f"Skóre shody: {score}/100 — {reason}\n"
                            f"URL: {listing.url}\n"
                            "Prohlížeč má formulář vyplněný (kontakt, zpráva, CV, souhlasy). "
                            "Zkontroluj si ho a vyber Schválit / Přeskočit / Stop vše."
                        )
                        self.events.put(("pending_full", (info, message or "")))
                        self.waiting_for_decision.clear()
                        self.waiting_for_decision.wait()
                        return self.pending_decision or "skip"

                apply_info: list[str] = []
                try:
                    ok, apply_err = apply_to_job(
                        listing=listing,
                        cv_path=profile.cv_path,
                        storage_state_path=profile.jobs_storage_state_path,
                        message=message,
                        dry_run=dry,
                        browser_slow_mo_ms=slow_mo,
                        applicant_full_name=name_for_apply,
                        applicant_email=email_for_apply,
                        applicant_phone=phone_for_apply,
                        applicant_salary=salary_for_apply,
                        gemini_api_key=self.cfg.gemini_api_key,
                        gemini_model=self.cfg.gemini_model,
                        info_log=apply_info,
                        approval_callback=approval_cb,
                    )
                except Exception as apply_exc:
                    for line in apply_info:
                        self.events.put(("log", line))
                    try:
                        self.db.record_apply_failure(
                            listing, f"crash: {apply_exc.__class__.__name__}: {apply_exc}"
                        )
                    except Exception:
                        pass
                    self.events.put(("failures", ""))
                    self.events.put(
                        (
                            "log",
                            f"FAIL (výjimka): {listing.title} — {apply_exc.__class__.__name__}: {apply_exc}",
                        )
                    )
                    continue
                for line in apply_info:
                    self.events.put(("log", line))

                if apply_err == "__manual_stop__":
                    self.stop_event.set()
                    self.events.put(("log", f"Stop uživatelem při náhledu: {listing.title}"))
                    break
                if apply_err == "__manual_skip__":
                    if not dry:
                        self.db.mark_skipped(listing)
                    self.events.put(
                        (
                            "log",
                            f"SKIP manuálně po náhledu{' (dry — DB beze změny)' if dry else ' (uloženo)'}: {listing.title}",
                        )
                    )
                    continue

                apply_attempts += 1

                # Retry once na server chybu (jobs.cz občas vrátí „We run into some problem")
                if not ok and apply_err and "server chyba" in apply_err.lower() and not dry:
                    self.events.put(
                        (
                            "log",
                            f"Retry za 60s: {listing.title} (server chyba jobs.cz)",
                        )
                    )
                    for _ in range(120):
                        if self.stop_event.is_set():
                            break
                        _time.sleep(0.5)
                    if not self.stop_event.is_set():
                        retry_info: list[str] = []
                        try:
                            ok, apply_err = apply_to_job(
                                listing=listing,
                                cv_path=profile.cv_path,
                                storage_state_path=profile.jobs_storage_state_path,
                                message=message,
                                dry_run=dry,
                                browser_slow_mo_ms=slow_mo,
                                applicant_full_name=name_for_apply,
                                applicant_email=email_for_apply,
                                applicant_phone=phone_for_apply,
                                applicant_salary=salary_for_apply,
                                gemini_api_key=self.cfg.gemini_api_key,
                                gemini_model=self.cfg.gemini_model,
                                info_log=retry_info,
                                approval_callback=None,  # retry bez manuálního schválení
                                skip_gemini_form_check=True,  # nepotřebujeme znovu validovat
                            )
                        except Exception as retry_exc:
                            apply_err = f"retry selhal: {retry_exc}"
                            ok = False
                        for line in retry_info:
                            self.events.put(("log", f"[retry] {line}"))

                if dry and ok:
                    self.events.put(
                        ("log", f"DRY RUN: {listing.title} — {apply_err or 'bez zápisu do DB'}")
                    )
                elif ok:
                    self.db.mark_applied(listing)
                    consecutive_fails = 0
                    self.events.put(("log", f"OK odesláno (potvrzeno): {listing.title} (fit {score})"))
                else:
                    consecutive_fails += 1
                    try:
                        self.db.record_apply_failure(listing, apply_err)
                    except Exception:
                        pass
                    self.events.put(("failures", ""))
                    self.events.put(
                        ("log", f"FAIL odeslání: {listing.title} (fit {score}) — {apply_err}")
                    )
                    if safe_mode and consecutive_fails >= max_consecutive_fails:
                        self.events.put(
                            (
                                "log",
                                f"Safe mode: {consecutive_fails} FAILů v řadě → HARD STOP. "
                                "Zkontroluj diagnostiku v záložce Selhání.",
                            )
                        )
                        break

            self.events.put(("log", "Běh dokončen."))
        except Exception as exc:
            self.events.put(("log", f"ERROR: {exc}"))
        finally:
            self.events.put(("state", "Připraven"))
            self.events.put(("refresh", ""))

    def _pump_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log(payload)
            elif kind == "pending":
                self._set_pending_text(payload)
                self._set_pending_message("")
            elif kind == "pending_full":
                info_text, message_text = payload
                self._set_pending_payload(info_text, message_text)
            elif kind == "summary":
                self._set_summary_text(payload)
            elif kind == "summary_start":
                self._summary_stream_begin()
            elif kind == "summary_chunk":
                self._summary_stream_append(payload)
            elif kind == "preview":
                try:
                    self._close_preview_browser()
                    proc, pdir = open_listing_preview(payload)
                    self._preview_proc = proc
                    self._preview_profile_dir = pdir
                except Exception as exc:
                    self._log(f"Preview: {exc}")
            elif kind == "state":
                self.status_var.set(payload)
                if payload == "Připraven":
                    self._set_pending_text("Žádná čekající položka")
                    self._set_pending_message("")
            elif kind == "failures":
                self._load_failures(log_result=False)
            elif kind == "refresh":
                self._load_history(log_result=False)
                self._load_failures(log_result=False)
        self.root.after(200, self._pump_events)

    def _enforce_cv_on_first_run(self) -> None:
        profile = self._get_active_profile()
        if not self._validate_cv_path(profile.cv_path, show_message=False):
            messagebox.showinfo(
                "Povinné nahrání CV",
                "Nahraj prosím CV pro aktivní profil v záložce Nastavení.",
            )


def launch_gui() -> None:
    root = ctk.CTk()
    app = JobHunterModernGUI(root)

    def _on_close() -> None:
        app.stop()
        app._close_preview_browser()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()
