#!/usr/bin/env python3
"""
Tail nejnovějšího tools/night_logs/run_*.log v UTF-8 (bez PowerShell Get-Content).

Výstup je čitelný v okně cmd i při UTF-8 logu z noční smyčky.

  --until-end   Sleduj dokud v logu nepřijde konec jedné session (### JOBHUNTER_RUN_END
                nebo ### JOBHUNTER_NIGHT_ITER_END), pak ukonči skript s kódem 0.
                Vhodné jako „debug okno“, které skončí až s koncem běhu main.py run.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import deque
from pathlib import Path

TAIL_LINES = 80
POLL_SECONDS = 0.5

# Řádek z cli.cmd_run (main.py run) nebo z tools/night_loop po skončení child procesu.
_END_MARKERS = (
    "### JOBHUNTER_RUN_END ",
    "### JOBHUNTER_NIGHT_ITER_END ",
)


def _newest_run_log(log_dir: Path) -> Path | None:
    runs = sorted(log_dir.glob("run_*.log"), key=lambda p: p.stat().st_mtime)
    return runs[-1] if runs else None


def _line_is_session_end(line: str) -> bool:
    s = line.strip()
    return any(m in s for m in _END_MARKERS)


def main() -> int:
    ap = argparse.ArgumentParser(description="Live tail Job Hunter run_*.log")
    ap.add_argument(
        "--until-end",
        action="store_true",
        help="Skonči po prvním řádku s JOBHUNTER_*_END (jeden dokončený běh v logu).",
    )
    args = ap.parse_args()
    until_end = bool(args.until_end)

    here = Path(__file__).resolve().parent
    log_dir = here / "night_logs"
    if not log_dir.is_dir():
        print(f"Složka neexistuje: {log_dir}", file=sys.stderr)
        return 1

    path = _newest_run_log(log_dir)
    if path is None:
        print("Žádný run_*.log. Spusť AUTO.bat v kořeni projektu.", file=sys.stderr)
        return 1

    print()
    print("=" * 62)
    print("  JOB HUNTER - LIVE log viewer    Made by Wux with <3")
    print("=" * 62)
    print()
    if until_end:
        print("Režim: sleduji dokud neskončí zapsaný běh (řádek ### JOBHUNTER_*_END).")
    else:
        print("Po každém 'OK:' / 'FAIL:' / 'SKIP:' uvidíš nový řádek.")
        print("Zavření okna tohle sledování zastaví (smyčka běží nezávisle).")
    print()
    print(f"Sleduji run: {path.name}")
    wd = log_dir / "watchdog.log"
    if wd.is_file():
        print(f"Watchdog log: {wd} (tady se tailuje jen run log)")
    print()
    print("-" * 62)
    print()

    while True:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            dq: deque[str] = deque(maxlen=TAIL_LINES)
            for line in f:
                dq.append(line.rstrip("\n\r"))
            for ln in dq:
                print(ln)
                if until_end and _line_is_session_end(ln):
                    print()
                    print("[Konec běhu v logu — sledování končí.]")
                    print()
                    return 0
            sys.stdout.flush()

            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    if until_end and _line_is_session_end(line):
                        print()
                        print("[Konec běhu v logu — sledování končí.]")
                        print()
                        return 0
                else:
                    time.sleep(POLL_SECONDS)
                    newer = _newest_run_log(log_dir)
                    if newer is not None and newer.resolve() != path.resolve():
                        path = newer
                        print(
                            f"\n=== Přepínám na novější log: {path.name} ===\n",
                            flush=True,
                        )
                        break


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[Konec]", file=sys.stderr)
        raise SystemExit(0) from None
