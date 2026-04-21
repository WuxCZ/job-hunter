#!/usr/bin/env python3
"""
Tail nejnovějšího tools/night_logs/run_*.log v UTF-8 (bez PowerShell Get-Content).

Výstup je čitelný v okně cmd i při UTF-8 logu z noční smyčky.
"""

from __future__ import annotations

import sys
import time
from collections import deque
from pathlib import Path

TAIL_LINES = 80
POLL_SECONDS = 0.5


def _newest_run_log(log_dir: Path) -> Path | None:
    runs = sorted(log_dir.glob("run_*.log"), key=lambda p: p.stat().st_mtime)
    return runs[-1] if runs else None


def main() -> int:
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
            sys.stdout.flush()

            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
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
