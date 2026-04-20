"""
Strážní démon nad `night_loop.py`.

Každých 5 minut zkontroluje:
  - běží nějaký python.exe s `night_loop` v command line?
  - updatoval se některý `run_*.log` v posledních 15 minutách?

Pokud ne, considers the loop dead/hung a SPUSTÍ NOVOU instanci. Pokud byl nalezen
živý PID ale log je stale (>15 min), zabije ho a spustí čerstvě.

Watchdog sám končí po STOP_HOUR (default 08:00) a nepouští po tom nic dalšího.
Během chodu drží Windows v „awake" režimu (SetThreadExecutionState).

Spouštění:
  python tools/watchdog.py

Nebo přes AUTO.bat (doporučeno — pustí watchdog místo night_loop přímo).
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LOG_DIR = HERE / "night_logs"
LOG_DIR.mkdir(exist_ok=True)

CHECK_INTERVAL_SECONDS = 5 * 60
STALE_LOG_THRESHOLD_SECONDS = 15 * 60

STOP_HOUR = 8
STOP_MINUTE = 0

WATCHDOG_LOG = LOG_DIR / "watchdog.log"

# Windows API — drží systém awake bez nutnosti admin práv.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_AWAYMODE_REQUIRED = 0x00000040


def _prevent_sleep() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(
                _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_AWAYMODE_REQUIRED
            )
        except Exception:
            pass


def _allow_sleep() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        except Exception:
            pass


def _log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with WATCHDOG_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _find_night_loop_pid() -> int | None:
    """Najde PID python.exe procesu běžícího `night_loop`."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.Name -eq 'python.exe' -and "
                    "$_.CommandLine -like '*night_loop*' } | "
                    "Select-Object -First 1 -ExpandProperty ProcessId"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as exc:
        _log(f"WARN: find_night_loop_pid exception: {exc}")
        return None
    raw = (out.stdout or "").strip()
    if raw.isdigit():
        return int(raw)
    return None


def _newest_log_mtime() -> float:
    """Čas poslední modifikace nejnovějšího run_*.log."""
    candidates = sorted(LOG_DIR.glob("run_*.log"), key=os.path.getmtime)
    if not candidates:
        return 0.0
    return os.path.getmtime(candidates[-1])


def _spawn_night_loop() -> int | None:
    """Spustí novou instanci night_loop.py detached, vrátí PID."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(HERE / "night_loop.py")],
            cwd=str(ROOT),
            creationflags=creationflags,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        _log(f"Spuštěna nová night_loop.py (PID {proc.pid})")
        return proc.pid
    except Exception as exc:
        _log(f"ERROR: nepodařilo se spustit night_loop.py: {exc}")
        return None


def _kill_pid(pid: int) -> None:
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                timeout=15,
                capture_output=True,
            )
            _log(f"Zabit proces {pid}")
        except Exception as exc:
            _log(f"WARN: taskkill PID {pid} selhal: {exc}")


def _past_stop_hour() -> bool:
    now = datetime.now().time()
    stop = dtime(hour=STOP_HOUR, minute=STOP_MINUTE)
    return now >= stop and now < dtime(hour=STOP_HOUR + 12)


def main() -> int:
    _prevent_sleep()
    _log("=== Watchdog startuje ===")
    _log(f"Check každých {CHECK_INTERVAL_SECONDS // 60} min, "
         f"stale threshold {STALE_LOG_THRESHOLD_SECONDS // 60} min, "
         f"stop hodina {STOP_HOUR:02d}:{STOP_MINUTE:02d}")

    # Přivítání: pokud smyčka už běží, použijeme ji; jinak nahodíme.
    existing = _find_night_loop_pid()
    if existing:
        _log(f"Nalezena běžící night_loop (PID {existing}), nechávám ji.")
    else:
        _log("Žádná night_loop neběží — startuji novou.")
        _spawn_night_loop()

    while True:
        if _past_stop_hour():
            _log(f"Dosažena stop hodina {STOP_HOUR:02d}:{STOP_MINUTE:02d}. Konec watchdogu.")
            break

        try:
            time.sleep(CHECK_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            _log("Ctrl+C — watchdog ukončuju.")
            break

        pid = _find_night_loop_pid()
        now_ts = time.time()
        log_mtime = _newest_log_mtime()
        log_age = now_ts - log_mtime if log_mtime else 9e9

        if pid is None:
            _log("Heartbeat: night_loop NEBĚŽÍ → restartuju.")
            _spawn_night_loop()
            continue

        if log_age > STALE_LOG_THRESHOLD_SECONDS:
            _log(
                f"Heartbeat: PID {pid} žije, ALE log je starý "
                f"{int(log_age)}s (> {STALE_LOG_THRESHOLD_SECONDS}s). "
                "Zabíjím a restartuju."
            )
            _kill_pid(pid)
            # Zabít i subprocess main.py run (child)
            if sys.platform == "win32":
                subprocess.run(
                    [
                        "taskkill",
                        "/F",
                        "/IM",
                        "python.exe",
                        "/FI",
                        "WINDOWTITLE eq JobHunter*",
                    ],
                    check=False,
                    capture_output=True,
                )
            # Krátká pauza, pak nová.
            time.sleep(5)
            _spawn_night_loop()
            continue

        _log(
            f"Heartbeat OK: PID {pid} žije, log aktualní "
            f"(stáří {int(log_age)}s)."
        )

    _allow_sleep()
    return 0


if __name__ == "__main__":
    sys.exit(main())
