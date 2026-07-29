from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

from .zoom_recordings import Mp4Metadata

_DURATION_PATTERN = re.compile(
    r"Duration:\s*(?P<hours>\d+):(?P<minutes>\d{2}):"
    r"(?P<seconds>\d{2}(?:\.\d+)?)"
)
_CREATION_TIME_PATTERN = re.compile(
    r"creation_time\s*[:=]\s*(?P<value>[^\s,]+)",
    re.IGNORECASE,
)

CommandRunner = Callable[..., CompletedProcess[str]]


class MediaToolError(RuntimeError):
    pass


def subprocess_window_options(
    *,
    platform_name: str | None = None,
) -> dict[str, Any]:
    if (platform_name or os.name) != "nt":
        return {}
    options: dict[str, Any] = {
        "creationflags": getattr(
            subprocess,
            "CREATE_NO_WINDOW",
            0x08000000,
        )
    }
    startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_factory is not None:
        startupinfo = startupinfo_factory()
        startupinfo.dwFlags |= getattr(
            subprocess,
            "STARTF_USESHOWWINDOW",
            0x00000001,
        )
        startupinfo.wShowWindow = 0
        options["startupinfo"] = startupinfo
    return options


def bundled_ffmpeg_path() -> str:
    try:
        import imageio_ffmpeg
    except ModuleNotFoundError as error:
        raise MediaToolError(
            "Не встановлено bundled FFmpeg. Повторіть встановлення залежностей."
        ) from error
    return imageio_ffmpeg.get_ffmpeg_exe()


def parse_ffmpeg_metadata(output: str) -> Mp4Metadata:
    duration_match = _DURATION_PATTERN.search(output)
    if duration_match is None:
        raise MediaToolError("FFmpeg не повернув тривалість MP4")
    duration = (
        int(duration_match.group("hours")) * 3600
        + int(duration_match.group("minutes")) * 60
        + float(duration_match.group("seconds"))
    )
    creation_time: datetime | None = None
    creation_match = _CREATION_TIME_PATTERN.search(output)
    if creation_match is not None:
        raw = creation_match.group("value")
        try:
            creation_time = datetime.fromisoformat(
                raw.replace("Z", "+00:00")
            )
        except ValueError:
            creation_time = None
    return Mp4Metadata(
        duration_seconds=duration,
        creation_time=creation_time,
        has_video_stream=bool(
            re.search(r"Stream\b[^\r\n]*\bVideo:", output, re.IGNORECASE)
        ),
    )


def probe_mp4(
    path: Path,
    *,
    ffmpeg_path: str | None = None,
    runner: CommandRunner = subprocess.run,
) -> Mp4Metadata:
    if not path.is_file():
        raise MediaToolError(f"MP4 не знайдено: {path}")
    executable = ffmpeg_path or bundled_ffmpeg_path()
    result = runner(
        [
            executable,
            "-hide_banner",
            "-i",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        **subprocess_window_options(),
    )
    return parse_ffmpeg_metadata(f"{result.stdout}\n{result.stderr}")


def general_preview_offsets(duration_seconds: float) -> tuple[float, ...]:
    if duration_seconds <= 0:
        raise ValueError("Video duration must be positive")
    return tuple(
        round(duration_seconds * ratio, 3)
        for ratio in (0.10, 0.35, 0.60, 0.85)
    )


def boundary_preview_offsets(
    duration_seconds: float,
    *,
    boundary_seconds: float,
) -> tuple[float, ...]:
    if duration_seconds <= 0:
        raise ValueError("Video duration must be positive")
    if not 0 <= boundary_seconds <= duration_seconds:
        raise ValueError("Boundary must be inside the video")
    return tuple(
        round(min(duration_seconds, max(0, boundary_seconds + delta)), 3)
        for delta in (-90, -15, 15, 90)
    )


def extract_preview_frames(
    source: Path,
    offsets: Iterable[float],
    *,
    output_dir: Path,
    ffmpeg_path: str | None = None,
    runner: CommandRunner = subprocess.run,
) -> tuple[Path, ...]:
    executable = ffmpeg_path or bundled_ffmpeg_path()
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for index, offset in enumerate(offsets, start=1):
        target = output_dir / f"{source.stem}-frame-{index}.png"
        result = runner(
            [
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{offset:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale=480:-1",
                "-y",
                str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
            **subprocess_window_options(),
        )
        if result.returncode != 0 or not target.is_file():
            raise MediaToolError(
                f"Не вдалося створити preview-кадр: {result.stderr.strip()}"
            )
        outputs.append(target)
    return tuple(outputs)


def split_mp4(
    source: Path,
    *,
    split_offset_seconds: float,
    output_dir: Path,
    ffmpeg_path: str | None = None,
    runner: CommandRunner = subprocess.run,
) -> tuple[Path, Path]:
    if split_offset_seconds <= 0:
        raise ValueError("Split offset must be positive")
    executable = ffmpeg_path or bundled_ffmpeg_path()
    output_dir.mkdir(parents=True, exist_ok=True)
    before = output_dir / f"{source.stem}__before.mp4"
    after = output_dir / f"{source.stem}__after.mp4"
    offset = f"{split_offset_seconds:.3f}"
    commands = (
        [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-t",
            offset,
            "-c",
            "copy",
            "-y",
            str(before),
        ],
        [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            offset,
            "-i",
            str(source),
            "-c",
            "copy",
            "-y",
            str(after),
        ],
    )
    for command in commands:
        result = runner(
            command,
            capture_output=True,
            text=True,
            check=False,
            **subprocess_window_options(),
        )
        if result.returncode != 0:
            raise MediaToolError(
                f"FFmpeg split завершився помилкою: {result.stderr.strip()}"
            )
    if not before.is_file() or not after.is_file():
        raise MediaToolError("FFmpeg не створив обидві частини MP4")
    return before, after
