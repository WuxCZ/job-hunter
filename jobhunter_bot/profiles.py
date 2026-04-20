from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from jobhunter_bot.config import AppConfig


@dataclass
class UserProfile:
    name: str
    cv_path: str
    locality: str = "brno"
    query: str = "IT"
    radius_km: int = 30
    jobs_storage_state_path: str = "storage-state.json"
    # Firemní / Alma Career formuláře (jméno, e-mail, telefon u přihlášky)
    applicant_full_name: str = ""
    applicant_email: str = ""
    applicant_phone: str = ""
    # Plat — pokud formulář požaduje mzdové očekávání (prázdné = nevyplňovat)
    applicant_salary: str = "50000"


class ProfileStore:
    def __init__(self, path: str = "profiles.json") -> None:
        self.path = Path(path)

    def load(self, fallback_cfg: AppConfig) -> tuple[list[UserProfile], str]:
        if not self.path.exists():
            default = UserProfile(
                name="Default",
                cv_path="",
                jobs_storage_state_path=fallback_cfg.storage_state_path,
            )
            self.save([default], "Default")
            return [default], "Default"

        raw = json.loads(self.path.read_text(encoding="utf-8"))

        def _profile_item(item: dict) -> UserProfile:
            return UserProfile(
                name=item["name"],
                cv_path=item.get("cv_path", ""),
                locality=item.get("locality", "brno"),
                query=item.get("query", "IT"),
                radius_km=int(item.get("radius_km", 30)),
                jobs_storage_state_path=item.get("jobs_storage_state_path", fallback_cfg.storage_state_path),
                applicant_full_name=item.get("applicant_full_name", ""),
                applicant_email=item.get("applicant_email", ""),
                applicant_phone=item.get("applicant_phone", ""),
                applicant_salary=str(item.get("applicant_salary", "50000") or ""),
            )

        profiles = [_profile_item(dict(p)) for p in raw.get("profiles", [])]
        active = raw.get("active_profile", profiles[0].name if profiles else "Default")
        if not profiles:
            default = UserProfile(
                name="Default",
                cv_path="",
                jobs_storage_state_path=fallback_cfg.storage_state_path,
            )
            profiles = [default]
            active = default.name
            self.save(profiles, active)
        return profiles, active

    def save(self, profiles: list[UserProfile], active_profile: str) -> None:
        payload = {
            "active_profile": active_profile,
            "profiles": [asdict(profile) for profile in profiles],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
