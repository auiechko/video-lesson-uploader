from __future__ import annotations

import string
import tomllib
from dataclasses import dataclass
from pathlib import Path
import json

from .planning import DEFAULT_ALBUM_BATCH_TEMPLATE


@dataclass(frozen=True, slots=True)
class AppConfig:
    album_batch: str = DEFAULT_ALBUM_BATCH_TEMPLATE
    api_id: int | None = None
    api_hash_env: str = "TELEGRAM_API_HASH"
    session: str = "lesson-video-uploader"
    phone: str = ""


def load_config(path: Path | None = None) -> AppConfig:
    if path is None:
        return AppConfig()
    with path.open("rb") as file:
        raw = tomllib.load(file)
    caption = raw.get("caption", {})
    if not isinstance(caption, dict):
        raise ValueError("[caption] must be a TOML table")
    template = caption.get("album_batch", DEFAULT_ALBUM_BATCH_TEMPLATE)
    if not isinstance(template, str):
        raise ValueError("caption.album_batch must be a string")
    fields = {
        field_name
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name is not None
    }
    required = {"album_number", "album_count"}
    if not required.issubset(fields):
        missing = ", ".join(sorted(required - fields))
        raise ValueError(f"caption.album_batch is missing {missing}")
    telegram = raw.get("telegram", {})
    if not isinstance(telegram, dict):
        raise ValueError("[telegram] must be a TOML table")
    api_id = telegram.get("api_id")
    if api_id is not None and (
        not isinstance(api_id, int) or isinstance(api_id, bool) or api_id <= 0
    ):
        raise ValueError("telegram.api_id must be a positive integer")
    api_hash_env = telegram.get("api_hash_env", "TELEGRAM_API_HASH")
    session = telegram.get("session", "lesson-video-uploader")
    phone = telegram.get("phone", "")
    if not isinstance(api_hash_env, str) or not api_hash_env.strip():
        raise ValueError("telegram.api_hash_env must be a non-empty string")
    if not isinstance(session, str) or not session.strip():
        raise ValueError("telegram.session must be a non-empty string")
    if not isinstance(phone, str):
        raise ValueError("telegram.phone must be a string")
    return AppConfig(
        album_batch=template,
        api_id=api_id,
        api_hash_env=api_hash_env.strip(),
        session=session.strip(),
        phone=phone.strip(),
    )


def save_config(path: Path, config: AppConfig) -> None:
    """Persist non-secret settings as UTF-8 TOML."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[telegram]"]
    if config.api_id is not None:
        lines.append(f"api_id = {config.api_id}")
    lines.extend([
        f"api_hash_env = {json.dumps(config.api_hash_env, ensure_ascii=False)}",
        f"session = {json.dumps(config.session, ensure_ascii=False)}",
        f"phone = {json.dumps(config.phone, ensure_ascii=False)}",
        "",
        "[caption]",
        f"album_batch = {json.dumps(config.album_batch, ensure_ascii=False)}",
        "",
    ])
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(path)
