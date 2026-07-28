from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any


_PROFILE_ID_PATTERN = re.compile(r"^[\w.-]+$")


def _default_app_data_dir() -> Path:
    from platformdirs import user_config_path

    return user_config_path("LessonVideoUploader", appauthor=False)


def telegram_session_path(
    profile_id: str,
    *,
    base_dir: Path | None = None,
) -> Path:
    profile = profile_id.strip()
    if (
        not profile
        or profile in {".", ".."}
        or _PROFILE_ID_PATTERN.fullmatch(profile) is None
    ):
        raise ValueError("Некоректний profile ID для Telethon session")
    root = (base_dir or _default_app_data_dir()).expanduser().resolve()
    return root / "profiles" / profile / "telegram" / "telegram"


def create_telegram_client(
    client_factory: Callable[..., Any],
    *,
    profile_id: str,
    api_id: object,
    api_hash: object,
    base_dir: Path | None = None,
) -> Any:
    if isinstance(api_id, bool):
        raise ValueError("Telegram API ID має бути додатним цілим числом")
    try:
        normalized_api_id = int(api_id)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise ValueError(
            "Telegram API ID має бути додатним цілим числом"
        ) from error
    if normalized_api_id <= 0:
        raise ValueError("Telegram API ID має бути додатним цілим числом")

    normalized_api_hash = str(api_hash).strip()
    if not normalized_api_hash:
        raise ValueError("Telegram API hash не може бути порожнім")

    session_path = telegram_session_path(profile_id, base_dir=base_dir)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    return client_factory(
        session=session_path,
        api_id=int(normalized_api_id),
        api_hash=str(normalized_api_hash),
    )
