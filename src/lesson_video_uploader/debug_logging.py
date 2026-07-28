from __future__ import annotations

import traceback
from datetime import datetime, timezone
from pathlib import Path


def _default_debug_log_path() -> Path:
    from platformdirs import user_log_path

    return (
        user_log_path("LessonVideoUploader", appauthor=False)
        / "debug.log"
    )


def write_debug_exception(
    error: BaseException,
    *,
    secrets: tuple[str, ...] = (),
    log_path: Path | None = None,
) -> Path:
    destination = log_path or _default_debug_log_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    rendered = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    for secret in secrets:
        if secret:
            rendered = rendered.replace(secret, "[REDACTED]")
    timestamp = datetime.now(timezone.utc).isoformat()
    with destination.open("a", encoding="utf-8") as stream:
        stream.write(f"[{timestamp}]\n{rendered}\n")
    return destination
