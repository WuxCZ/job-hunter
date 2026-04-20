from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import webbrowser
from pathlib import Path

try:
    from screeninfo import get_monitors
except Exception:  # pragma: no cover
    get_monitors = None


def _chrome_executable() -> str | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which("chrome") or shutil.which("msedge") or shutil.which("chromium")


def _window_args_second_monitor() -> list[str]:
    if get_monitors is None:
        return ["--window-size=1280,800"]
    monitors = get_monitors()
    if not monitors:
        return ["--window-size=1280,800"]
    if len(monitors) >= 2:
        m = monitors[1]
    else:
        m = monitors[0]
    x = int(m.x) + 24
    y = int(m.y) + 24
    w = max(900, min(1400, int(m.width * 0.92)))
    h = max(700, min(1000, int(m.height * 0.88)))
    return [f"--window-position={x},{y}", f"--window-size={w},{h}"]


def terminate_listing_preview(
    proc: subprocess.Popen | None,
    profile_dir: Path | None,
) -> None:
    """Ukončí izolované Chrome okno náhledu a smače dočasný profil."""
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    if profile_dir is not None and profile_dir.is_dir():
        shutil.rmtree(profile_dir, ignore_errors=True)


def open_listing_preview(url: str) -> tuple[subprocess.Popen | None, Path | None]:
    """
    Otevře náhled inzerátu v samostatné instanci Chrome (dočasný user-data-dir),
    aby šlo proces spolehlivě ukončit po schválení / přeskočení.

    Volat jen z hlavního vlákna GUI (kvůli subprocess / DPI).

    Vrátí (Popen nebo None, cesta k profilu nebo None). Bez Chrome použije
    výchozí prohlížeč — ten už neumíme programově zavřít.
    """
    profile_dir = Path(tempfile.mkdtemp(prefix="jobhunter_preview_"))
    chrome = _chrome_executable()
    window = _window_args_second_monitor()
    if chrome and Path(chrome).exists():
        try:
            cmd = [
                chrome,
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--new-window",
                *window,
                url,
            ]
            creation = 0
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                creation = subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation,
            )
            return proc, profile_dir
        except OSError:
            shutil.rmtree(profile_dir, ignore_errors=True)

    try:
        webbrowser.open_new_tab(url)
    except Exception:
        pass
    shutil.rmtree(profile_dir, ignore_errors=True)
    return None, None


class ListingPreviewer:
    """Kompatibilní wrapper — otevírání řeší GUI přes open_listing_preview."""

    def open_listing(self, url: str) -> None:
        open_listing_preview(url)

    def close(self) -> None:
        pass
