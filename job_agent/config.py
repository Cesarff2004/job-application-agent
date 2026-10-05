from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_yaml(name: str) -> dict:
    with open(ROOT / "config" / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings() -> dict:
    return load_yaml("settings.yaml")


def load_profile() -> dict:
    profile = load_yaml("profile.yaml")
    missing = [k for k in ("first_name", "last_name", "email", "phone") if not profile.get(k)]
    if missing:
        raise SystemExit(f"config/profile.yaml is missing: {', '.join(missing)}")
    resume = ROOT / profile.get("resume_path", "")
    if not resume.is_file():
        raise SystemExit(f"Resume not found at {resume}")
    return profile
