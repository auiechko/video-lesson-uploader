from __future__ import annotations

import os
import re
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .media_tools import probe_mp4
from .zoom_recordings import MetadataProbe, Mp4Metadata

_FOLDER_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}\.\d{2}\.\d{2})(?:\s|$)"
)


class ZoomPreflightStatus(StrEnum):
    READY = "READY"
    UNRENDERED_RECORDING = "UNRENDERED_RECORDING"
    RENDERING_IN_PROGRESS = "RENDERING_IN_PROGRESS"
    VIDEO_NOT_AVAILABLE_LOCALLY = "VIDEO_NOT_AVAILABLE_LOCALLY"
    INVALID_VIDEO = "INVALID_VIDEO"
    ZOOM_FOLDER_NOT_FOUND = "ZOOM_FOLDER_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class PreflightSettings:
    minimum_video_size_mb: float = 5
    video_stability_check_seconds: float = 5
    timezone_name: str = "Europe/Kyiv"

    def __post_init__(self) -> None:
        if self.minimum_video_size_mb < 0:
            raise ValueError("minimum_video_size_mb cannot be negative")
        if self.video_stability_check_seconds < 0:
            raise ValueError(
                "video_stability_check_seconds cannot be negative"
            )


@dataclass(frozen=True, slots=True)
class PreflightFolderResult:
    folder: Path
    start: datetime
    status: ZoomPreflightStatus
    found_files: tuple[Path, ...]
    video_paths: tuple[Path, ...]
    metadata: tuple[Mp4Metadata, ...]
    reason: str
    recommended_action: str


@dataclass(frozen=True, slots=True)
class ZoomPreflightResult:
    root: Path
    date_from: date
    date_to: date
    folders: tuple[PreflightFolderResult, ...]

    @property
    def blocking_folders(self) -> tuple[PreflightFolderResult, ...]:
        return tuple(
            folder
            for folder in self.folders
            if folder.status is not ZoomPreflightStatus.READY
        )

    @property
    def ready_folders(self) -> tuple[PreflightFolderResult, ...]:
        return tuple(
            folder
            for folder in self.folders
            if folder.status is ZoomPreflightStatus.READY
        )

    @property
    def is_passed(self) -> bool:
        return not self.blocking_folders


class ZoomPreflightService:
    def __init__(
        self,
        *,
        metadata_probe: MetadataProbe = probe_mp4,
        size_reader: Callable[[Path], int] | None = None,
        local_availability_checker: Callable[[Path], bool] | None = None,
        sleeper: Callable[[float], object] = time.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.metadata_probe = metadata_probe
        self.size_reader = size_reader or (lambda path: path.stat().st_size)
        self.local_availability_checker = (
            local_availability_checker or _is_available_locally
        )
        self.sleeper = sleeper
        self.now = now or (lambda: datetime.now(timezone.utc))

    def check(
        self,
        root: Path,
        date_from: date,
        date_to: date,
        settings: PreflightSettings | None = None,
        *,
        manual_folder_starts: Mapping[Any, datetime] | None = None,
    ) -> ZoomPreflightResult:
        active_settings = settings or PreflightSettings()
        if date_to < date_from:
            raise ValueError("Кінцева дата не може бути раніше початкової")
        timezone_value = _timezone(active_settings.timezone_name)
        root = root.expanduser()
        if not root.is_dir():
            raise ValueError(f"Не знайдено директорію Zoom: {root}")
        manual_starts = {
            _normalized_path(Path(path)): start
            for path, start in (manual_folder_starts or {}).items()
        }
        selected: list[tuple[Path, datetime]] = []
        for folder in root.iterdir():
            if not folder.is_dir():
                continue
            start = manual_starts.get(_normalized_path(folder))
            if start is None:
                start = _folder_start(folder, timezone_value)
            elif start.tzinfo is None:
                raise ValueError(
                    f"Ручний час Zoom-папки має містити timezone: {folder}"
                )
            else:
                start = start.astimezone(timezone_value)
            if start is not None and date_from <= start.date() <= date_to:
                selected.append((folder, start))
        results = self._check_folders(
            tuple(selected),
            active_settings,
        )
        return ZoomPreflightResult(
            root=root,
            date_from=date_from,
            date_to=date_to,
            folders=results,
        )

    def recheck_blocked(
        self,
        previous: ZoomPreflightResult,
        settings: PreflightSettings | None = None,
    ) -> ZoomPreflightResult:
        active_settings = settings or PreflightSettings()
        refreshed = self._check_folders(
            tuple(
                (item.folder, item.start)
                for item in previous.blocking_folders
            ),
            active_settings,
        )
        by_path = {
            item.folder: item
            for item in (*previous.ready_folders, *refreshed)
        }
        return ZoomPreflightResult(
            root=previous.root,
            date_from=previous.date_from,
            date_to=previous.date_to,
            folders=tuple(
                sorted(by_path.values(), key=lambda item: item.start)
            ),
        )

    def _check_folders(
        self,
        folders: tuple[tuple[Path, datetime], ...],
        settings: PreflightSettings,
    ) -> tuple[PreflightFolderResult, ...]:
        candidates = {
            path: _video_files(path)
            for path, _start in folders
        }
        first_sizes = {
            video: self._safe_size(video)
            for videos in candidates.values()
            for video in videos
        }
        if first_sizes and settings.video_stability_check_seconds:
            self.sleeper(settings.video_stability_check_seconds)
        second_sizes = {
            video: self._safe_size(video)
            for video in first_sizes
        }
        return tuple(
            self._check_folder(
                folder,
                start,
                candidates[folder],
                first_sizes,
                second_sizes,
                settings,
            )
            for folder, start in folders
        )

    def _check_folder(
        self,
        folder: Path,
        start: datetime,
        videos: tuple[Path, ...],
        first_sizes: dict[Path, int | None],
        second_sizes: dict[Path, int | None],
        settings: PreflightSettings,
    ) -> PreflightFolderResult:
        files = tuple(
            sorted(
                (path for path in folder.iterdir() if path.is_file()),
                key=lambda path: path.name.casefold(),
            )
        )
        if not videos:
            return PreflightFolderResult(
                folder=folder,
                start=start,
                status=ZoomPreflightStatus.UNRENDERED_RECORDING,
                found_files=files,
                video_paths=(),
                metadata=(),
                reason="У Zoom-папці ще немає готового video*.mp4.",
                recommended_action=(
                    "Завершіть конвертацію в Zoom і повторіть перевірку."
                ),
            )

        metadata_items: list[Mp4Metadata] = []
        minimum_bytes = settings.minimum_video_size_mb * 1024 * 1024
        for video in videos:
            if not self.local_availability_checker(video):
                return self._problem(
                    folder,
                    start,
                    files,
                    videos,
                    ZoomPreflightStatus.VIDEO_NOT_AVAILABLE_LOCALLY,
                    f"Файл недоступний локально: {video.name}",
                    (
                        'Зробіть файл доступним локально через OneDrive: '
                        '"Завжди зберігати на цьому пристрої".'
                    ),
                )
            first_size = first_sizes.get(video)
            second_size = second_sizes.get(video)
            if first_size is None or second_size is None:
                return self._problem(
                    folder,
                    start,
                    files,
                    videos,
                    ZoomPreflightStatus.RENDERING_IN_PROGRESS,
                    f"Файл тимчасово недоступний для читання: {video.name}",
                    "Закрийте Zoom або дочекайтеся конвертації.",
                )
            if first_size != second_size:
                return self._problem(
                    folder,
                    start,
                    files,
                    videos,
                    ZoomPreflightStatus.RENDERING_IN_PROGRESS,
                    f"Розмір {video.name} ще змінюється.",
                    "Дочекайтеся завершення конвертації.",
                )
            if second_size <= minimum_bytes:
                return self._problem(
                    folder,
                    start,
                    files,
                    videos,
                    ZoomPreflightStatus.INVALID_VIDEO,
                    (
                        f"{video.name} менший за технічний мінімум "
                        f"{settings.minimum_video_size_mb:g} МБ."
                    ),
                    "Перевірте конвертацію або виберіть правильний MP4.",
                )
            try:
                with video.open("rb") as stream:
                    stream.read(1)
                metadata = self.metadata_probe(video)
            except OSError as error:
                return self._problem(
                    folder,
                    start,
                    files,
                    videos,
                    ZoomPreflightStatus.RENDERING_IN_PROGRESS,
                    f"Файл заблокований або тимчасово недоступний: {error}",
                    "Закрийте Zoom або дочекайтеся завершення конвертації.",
                )
            except (RuntimeError, ValueError) as error:
                recent = self._is_recent(video, settings)
                return self._problem(
                    folder,
                    start,
                    files,
                    videos,
                    (
                        ZoomPreflightStatus.RENDERING_IN_PROGRESS
                        if recent
                        else ZoomPreflightStatus.INVALID_VIDEO
                    ),
                    f"FFmpeg не може прочитати {video.name}: {error}",
                    (
                        "Дочекайтеся завершення конвертації."
                        if recent
                        else "Перевірте або повторно конвертуйте MP4."
                    ),
                )
            if (
                metadata.duration_seconds <= 0
                or not metadata.has_video_stream
            ):
                return self._problem(
                    folder,
                    start,
                    files,
                    videos,
                    ZoomPreflightStatus.INVALID_VIDEO,
                    f"У {video.name} немає валідного відеопотоку.",
                    "Перевірте або повторно конвертуйте MP4.",
                )
            metadata_items.append(metadata)

        return PreflightFolderResult(
            folder=folder,
            start=start,
            status=ZoomPreflightStatus.READY,
            found_files=files,
            video_paths=videos,
            metadata=tuple(metadata_items),
            reason="Усі MP4 стабільні, локальні та валідні.",
            recommended_action="Можна переходити до matching.",
        )

    def _safe_size(self, path: Path) -> int | None:
        try:
            return self.size_reader(path)
        except OSError:
            return None

    def _is_recent(
        self,
        path: Path,
        settings: PreflightSettings,
    ) -> bool:
        try:
            modified = datetime.fromtimestamp(
                path.stat().st_mtime,
                tz=timezone.utc,
            )
        except OSError:
            return True
        return (
            self.now() - modified
        ).total_seconds() <= settings.video_stability_check_seconds

    @staticmethod
    def _problem(
        folder: Path,
        start: datetime,
        files: tuple[Path, ...],
        videos: tuple[Path, ...],
        status: ZoomPreflightStatus,
        reason: str,
        action: str,
    ) -> PreflightFolderResult:
        return PreflightFolderResult(
            folder=folder,
            start=start,
            status=status,
            found_files=files,
            video_paths=videos,
            metadata=(),
            reason=reason,
            recommended_action=action,
        )


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"Невідомий часовий пояс: {name}") from error


def _folder_start(
    folder: Path,
    timezone_value: ZoneInfo,
) -> datetime | None:
    match = _FOLDER_PATTERN.match(folder.name)
    if match is None:
        return None
    return datetime.strptime(
        match.group("timestamp"),
        "%Y-%m-%d %H.%M.%S",
    ).replace(tzinfo=timezone_value)


def _video_files(folder: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            (
                path
                for path in folder.iterdir()
                if (
                    path.is_file()
                    and path.name.casefold().startswith("video")
                    and path.suffix.casefold() == ".mp4"
                )
            ),
            key=lambda path: path.name.casefold(),
        )
    )


def _normalized_path(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve()))


def _is_available_locally(path: Path) -> bool:
    try:
        file_stat = path.stat()
    except OSError:
        return False
    attributes = getattr(file_stat, "st_file_attributes", 0)
    placeholder_flags = (
        getattr(stat, "FILE_ATTRIBUTE_OFFLINE", 0)
        | getattr(stat, "FILE_ATTRIBUTE_RECALL_ON_OPEN", 0)
        | getattr(stat, "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS", 0)
    )
    return not bool(attributes & placeholder_flags)
