"""
Noční smyčka: pouští `python main.py run` opakovaně s bezpečnými limity.

Jednotlivý běh má max-apply 30, mezi běhy je pauza 40 minut.
Skript jede dokud:
  - nedosáhne `MAX_ITERATIONS` iterací, NEBO
  - čas nepřekročí `STOP_HOUR:STOP_MINUTE` (default 08:00), NEBO
  - 3 běhy v řadě vůbec nic neodešlou (došla nabídka / ban), NEBO
  - ho nezabiješ (Ctrl+C / zavření okna).

Logy každého běhu jdou do tools/night_logs/run_<i>_<timestamp>.log a zároveň na stdout.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path


# Windows API: drží systém "awake" dokud proces běží (nevyžaduje admin).
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_AWAYMODE_REQUIRED = 0x00000040


def _prevent_windows_sleep() -> None:
    """Řekne Windows, ať systém neusne, dokud tenhle proces běží."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_AWAYMODE_REQUIRED
        )
    except Exception:
        pass


def _allow_windows_sleep() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LOG_DIR = HERE / "night_logs"
LOG_DIR.mkdir(exist_ok=True)

# --- Konfigurace ---
MAX_ITERATIONS = 16
SLEEP_BETWEEN_RUNS_SECONDS = 40 * 60

STOP_HOUR = 8
STOP_MINUTE = 0

RUN_ARGS = [
    "run",
    "--limit", "2000",
    "--max-apply", "30",
    "--pause-seconds", "20",
    "--max-consecutive-fails", "5",
    "--min-fit", "50",
]


def _should_stop_by_clock() -> bool:
    now = datetime.now().time()
    stop = dtime(hour=STOP_HOUR, minute=STOP_MINUTE)
    if now < dtime(hour=STOP_HOUR - 1):
        return False
    return now >= stop


def _count_applied(log_path: Path) -> int:
    """Spočítá 'OK:' řádky v logu — kolik reálně odešlo."""
    try:
        txt = log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    return sum(1 for ln in txt.splitlines() if ln.startswith("OK: "))


def _run_once(iteration: int) -> int:
    """Vrátí počet úspěšně odeslaných inzerátů v této iteraci."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"run_{iteration:02d}_{ts}.log"
    print(f"\n=========  ITERACE {iteration}  =========")
    print(f"Start: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"Log:   {log_path}")
    print("Příkaz:", "python main.py", " ".join(RUN_ARGS))
    print("----------------------------------------")

    env_py = sys.executable

    import os as _os

    env = _os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    with log_path.open("w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [env_py, "-u", "main.py", *RUN_ARGS],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        try:
            if proc.stdout is None:
                proc.wait()
            else:
                for line in proc.stdout:
                    print(line, end="")
                    log_file.write(line)
                    log_file.flush()
        except KeyboardInterrupt:
            proc.terminate()
            raise
        finally:
            proc.wait()

    applied = _count_applied(log_path)
    print(f"\nIterace {iteration} hotová. Reálně odesláno: {applied}")
    return applied


_BANNER = r"""
==============================================================
   JOB HUNTER - AUTO mode smyčka
   Made by Wux with <3
=============================================================="""


def main() -> int:
    _prevent_windows_sleep()
    print(_BANNER)
    print(f"Start: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(
        f"Plán: max {MAX_ITERATIONS} iterací, mezi běhy {SLEEP_BETWEEN_RUNS_SECONDS // 60} min, "
        f"stop v {STOP_HOUR:02d}:{STOP_MINUTE:02d}"
    )
    print("Windows sleep zamčen (SetThreadExecutionState).")

    zeros_in_row = 0
    total_applied = 0

    for i in range(1, MAX_ITERATIONS + 1):
        if _should_stop_by_clock():
            print(
                f"\nStop: dosažena hraniční hodina {STOP_HOUR:02d}:{STOP_MINUTE:02d}. "
                f"Celkem odesláno: {total_applied}"
            )
            break

        try:
            applied = _run_once(i)
        except KeyboardInterrupt:
            print("\nPřerušeno uživatelem (Ctrl+C). Končím.")
            return 130
        except Exception as exc:
            print(f"\nVýjimka v iteraci {i}: {exc.__class__.__name__}: {exc}")
            applied = 0

        total_applied += applied
        zeros_in_row = 0 if applied > 0 else zeros_in_row + 1

        if zeros_in_row >= 3:
            print(
                "\n3 iterace v řadě nic neodeslaly (pravděpodobně vyčerpané nabídky "
                "nebo jobs.cz brzdí). Ukončuji smyčku."
            )
            break

        if i < MAX_ITERATIONS:
            wake = datetime.now().timestamp() + SLEEP_BETWEEN_RUNS_SECONDS
            print(
                f"\nSpím {SLEEP_BETWEEN_RUNS_SECONDS // 60} min do "
                f"{datetime.fromtimestamp(wake):%H:%M:%S}…"
            )
            time.sleep(SLEEP_BETWEEN_RUNS_SECONDS)

    print(f"\nCelkem odesláno za noc: {total_applied}")
    print(f"Skončil jsem {datetime.now():%Y-%m-%d %H:%M:%S}")
    _allow_windows_sleep()
    return 0


if __name__ == "__main__":
    sys.exit(main())
