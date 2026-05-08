"""Profile memory dashboard plugin backend.

Provides safe, profile-scoped read/update access for USER.md and MEMORY.md and
read-only visibility into SOUL.md. The routes intentionally return only profile
IDs/labels and relative file paths — never absolute local paths or raw backup
locations.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from hermes_constants import get_default_hermes_root, get_hermes_home

router = APIRouter()

_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|private[_-]?key|authorization)\s*[:=]\s*[^\s]+|"
    r"sk-[A-Za-z0-9][A-Za-z0-9_-]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}"
)

_ALLOWED_FILES: Dict[str, Dict[str, Any]] = {
    "user": {
        "label": "USER.md",
        "relative_path": Path("memories") / "USER.md",
        "description": "Stable facts about the user: preferences, communication style, durable workflow context.",
        "editable": True,
    },
    "memory": {
        "label": "MEMORY.md",
        "relative_path": Path("memories") / "MEMORY.md",
        "description": "Durable environment/project facts. Use skills/docs for procedures and avoid transient task progress.",
        "editable": True,
    },
    "soul": {
        "label": "SOUL.md",
        "relative_path": Path("SOUL.md"),
        "description": "Profile identity and operating doctrine. Shown read-only in this MVP because it is high-impact system-prompt context.",
        "editable": False,
    },
}


class MemoryFileUpdate(BaseModel):
    content: str = Field(default="", max_length=512_000)


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Resolved path escaped the selected profile.") from exc


def _validate_profile_id(profile: str) -> str:
    profile = (profile or "default").strip()
    if profile == "default":
        return profile
    if not _PROFILE_RE.fullmatch(profile) or profile in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid profile id.")
    return profile


def _profiles_root() -> Path:
    return get_default_hermes_root() / "profiles"


def _profile_home(profile: str) -> Path:
    profile = _validate_profile_id(profile)
    if profile == "default":
        home = get_hermes_home()
    else:
        home = _profiles_root() / profile
    if not home.exists() or not home.is_dir():
        raise HTTPException(status_code=404, detail="Profile not found.")
    return home.resolve()


def _profile_file(profile: str, file_key: str) -> Path:
    if file_key not in _ALLOWED_FILES:
        raise HTTPException(status_code=404, detail="Unknown memory file.")
    home = _profile_home(profile)
    target = (home / _ALLOWED_FILES[file_key]["relative_path"]).resolve()
    try:
        target.relative_to(home)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Resolved path escaped the selected profile.") from exc
    if target.exists() and not target.is_file():
        raise HTTPException(status_code=400, detail="Target is not a file.")
    return target


def _detect_warnings(content: str) -> List[str]:
    warnings: List[str] = []
    if _SECRET_RE.search(content or ""):
        warnings.append(
            "Potential secret-like text detected. Keep API keys, tokens, passwords, cookies, and private keys out of memory files."
        )
    return warnings


def _file_payload(profile: str, file_key: str) -> Dict[str, Any]:
    meta = _ALLOWED_FILES[file_key]
    home = _profile_home(profile)
    path = _profile_file(profile, file_key)
    exists = path.exists()
    content = path.read_text(encoding="utf-8") if exists else ""
    stat = path.stat() if exists else None
    return {
        "key": file_key,
        "label": meta["label"],
        "description": meta["description"],
        "relative_path": _safe_relative(path, home),
        "editable": bool(meta["editable"]),
        "exists": exists,
        "content": content,
        "size": stat.st_size if stat else 0,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat() if stat else None,
        "warnings": _detect_warnings(content),
    }


def _list_profiles() -> List[Dict[str, str]]:
    profiles: List[Dict[str, str]] = [{"id": "default", "label": "default"}]
    root = _profiles_root()
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir() or not _PROFILE_RE.fullmatch(child.name):
                continue
            profiles.append({"id": child.name, "label": child.name})
    return profiles


def _write_file_with_backup(profile: str, file_key: str, content: str) -> Dict[str, Any]:
    meta = _ALLOWED_FILES[file_key]
    if not meta.get("editable"):
        raise HTTPException(status_code=403, detail="This file is read-only in the dashboard.")

    home = _profile_home(profile)
    target = _profile_file(profile, file_key)
    target.parent.mkdir(parents=True, exist_ok=True)

    backup_info: Dict[str, Any] = {"created": False, "relative_path": None}
    if target.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = home / ".dashboard-memory-backups"
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup = (backup_dir / f"{target.name}.{stamp}.bak").resolve()
        try:
            backup.relative_to(home)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="Backup path escaped the selected profile.") from exc
        shutil.copy2(target, backup)
        backup_info = {"created": True, "relative_path": _safe_relative(backup, home)}

    target.write_text(content, encoding="utf-8")
    return {"ok": True, "file": _file_payload(profile, file_key), "backup": backup_info, "warnings": _detect_warnings(content)}


@router.get("/profiles")
async def get_profiles():
    return {"profiles": _list_profiles()}


@router.get("/files")
async def get_files(profile: str = "default"):
    profile = _validate_profile_id(profile)
    _profile_home(profile)
    return {
        "profile": profile,
        "guidance": {
            "user": "USER.md is for stable declarative facts about the user, not instructions or temporary task state.",
            "memory": "MEMORY.md is for durable environment/project facts; procedures belong in skills or docs.",
            "soul": "SOUL.md is profile identity and is read-only here to reduce accidental system-prompt changes.",
        },
        "files": [_file_payload(profile, key) for key in _ALLOWED_FILES],
    }


@router.put("/files/{profile}/{file_key}")
async def put_file(profile: str, file_key: str, body: MemoryFileUpdate):
    profile = _validate_profile_id(profile)
    if file_key not in _ALLOWED_FILES:
        raise HTTPException(status_code=404, detail="Unknown memory file.")
    return _write_file_with_backup(profile, file_key, body.content)
