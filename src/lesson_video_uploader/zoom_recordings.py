from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_ZOOM_FOLDER_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}\.\d{2}\.\d{2})(?:\s|$)"
)
_NATURAL_NUMBER_PATTERN = re.compile(r"(\d+)")


class ZoomRecordingError(ValueError):
    pass


class ZoomRecordingNotFound(ZoomRecordingError):
    pass


class ZoomRecordingAmbiguous(ZoomRecordingError):
    pass


class ZoomRecordingPendingConversion(ZoomRecordingError):
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        super().__init__(
            f"Запис Zoom ще не конвертовано в MP4: {folder.name}"
        )


class VideoTimeMethod(StrEnum):
    ZOOM_FOLDER_START = "ZOOM_FOLDER_START"
    MP4_METADATA = "MP4_METADATA"
    FILESYSTEM_TIMESTAMP = "FILESYSTEM_TIMESTAMP"
    SEQUENTIAL_DURATION = "SEQUENTIAL_DURATION"
    MANUALLY_CONFIRMED = "MANUALLY_CONFIRMED"


class VideoTimeConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ZoomFolderStatus(StrEnum):
    ZOOM_DATETIME_PARSE_ERROR = "ZOOM_DATETIME_PARSE_ERROR"


@dataclass(frozen=True, slots=True)
class Mp4Metadata:
    duration_seconds: float
    creation_time: datetime | None = None
    has_video_stream: bool = True


MetadataProbe = Callable[[Path], Mp4Metadata]


@dataclass(frozen=True, slots=True)
class ZoomVideoSegment:
    source_folder: Path
    path: Path
    sequence_number: int
    duration_seconds: float
    estimated_start: datetime
    estimated_end: datetime
    time_method: VideoTimeMethod
    confidence: VideoTimeConfidence
    file_size: int

    def __post_init__(self) -> None:
        if self.estimated_start.tzinfo is None:
            raise ValueError("Zoom video start must be timezone-aware")
        if self.estimated_end.tzinfo is None:
            raise ValueError("Zoom video end must be timezone-aware")
        if self.duration_seconds <= 0:
            raise ValueError("Zoom video duration must be positive")
        if self.estimated_end <= self.estimated_start:
            raise ValueError("Zoom video end must be after start")
        if self.sequence_number <= 0:
            raise ValueError("Zoom video sequence number must be positive")
        if self.file_size < 0:
            raise ValueError("Zoom video file size cannot be negative")


@dataclass(frozen=True, slots=True)
class ZoomRecordingFolder:
    path: Path
    start: datetime
    video_paths: tuple[Path, ...]
    has_unconverted_files: bool
    segments: tuple[ZoomVideoSegment, ...] = ()
    start_method: VideoTimeMethod = VideoTimeMethod.ZOOM_FOLDER_START


@dataclass(frozen=True, slots=True)
class ZoomFolderIssue:
    path: Path
    status: ZoomFolderStatus
    reason: str


@dataclass(frozen=True, slots=True)
class ZoomRecordingMatch:
    folder: Path
    start: datetime
    video_paths: tuple[Path, ...]
    delta_seconds: int


@dataclass(frozen=True, slots=True)
class ZoomRecordingCatalog:
    root: Path
    timezone: ZoneInfo
    folders: tuple[ZoomRecordingFolder, ...]
    issues: tuple[ZoomFolderIssue, ...] = ()

    @classmethod
    def scan(
        cls,
        root: Path,
        *,
        timezone_name: str = "Europe/Kyiv",
        metadata_probe: MetadataProbe | None = None,
        manual_folder_starts: Mapping[str, datetime] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> ZoomRecordingCatalog:
        root = root.expanduser()
        if not root.is_dir():
            raise ZoomRecordingNotFound(
                f"Не знайдено директорію записів Zoom: {root}"
            )
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError(
                f"Невідомий часовий пояс Zoom: {timezone_name}"
            ) from error

        folders: list[ZoomRecordingFolder] = []
        issues: list[ZoomFolderIssue] = []
        overrides = manual_folder_starts or {}
        for path in root.iterdir():
            if not path.is_dir():
                continue
            files = tuple(item for item in path.iterdir() if item.is_file())
            match = _ZOOM_FOLDER_PATTERN.match(path.name)
            resolved_path = str(path.resolve())
            manual_start = (
                overrides.get(resolved_path)
                or overrides.get(os.path.normcase(resolved_path))
            )
            if match is None and manual_start is None:
                if any(
                    item.suffix.casefold() in {".mp4", ".zoom"}
                    for item in files
                ):
                    issues.append(
                        ZoomFolderIssue(
                            path=path,
                            status=(
                                ZoomFolderStatus.ZOOM_DATETIME_PARSE_ERROR
                            ),
                            reason=(
                                "Назва папки не починається з "
                                "YYYY-MM-DD HH.MM.SS"
                            ),
                        )
                    )
                continue
            if manual_start is not None:
                if manual_start.tzinfo is None:
                    raise ValueError(
                        "Manual Zoom folder datetime must be timezone-aware"
                    )
                start = manual_start.astimezone(timezone)
                start_method = VideoTimeMethod.MANUALLY_CONFIRMED
            else:
                assert match is not None
                start = datetime.strptime(
                    match.group("timestamp"),
                    "%Y-%m-%d %H.%M.%S",
                ).replace(tzinfo=timezone)
                start_method = VideoTimeMethod.ZOOM_FOLDER_START
            if date_from is not None and start.date() < date_from:
                continue
            if date_to is not None and start.date() > date_to:
                continue
            videos = tuple(
                sorted(
                    (
                        item
                        for item in files
                        if item.suffix.casefold() == ".mp4"
                    ),
                    key=_video_order_key,
                )
            )
            segments = _build_segments(
                folder=path,
                folder_start=start,
                videos=videos,
                timezone=timezone,
                metadata_probe=metadata_probe,
            )
            ordered_videos = (
                tuple(segment.path for segment in segments)
                if len(segments) == len(videos)
                else videos
            )
            folders.append(
                ZoomRecordingFolder(
                    path=path,
                    start=start,
                    video_paths=ordered_videos,
                    has_unconverted_files=any(
                        item.suffix.casefold() == ".zoom"
                        for item in files
                    ),
                    segments=segments,
                    start_method=start_method,
                )
            )
        return cls(
            root=root,
            timezone=timezone,
            folders=tuple(sorted(folders, key=lambda item: item.start)),
            issues=tuple(issues),
        )

    def match(
        self,
        event_start: datetime,
        *,
        tolerance_minutes: int = 20,
    ) -> ZoomRecordingMatch:
        if (
            not isinstance(tolerance_minutes, int)
            or isinstance(tolerance_minutes, bool)
            or tolerance_minutes < 0
        ):
            raise ValueError(
                "Допуск часу Zoom має бути невід’ємним цілим числом"
            )
        normalized_start = (
            event_start.replace(tzinfo=self.timezone)
            if event_start.tzinfo is None
            else event_start.astimezone(self.timezone)
        )
        tolerance_seconds = tolerance_minutes * 60
        candidates = [
            (
                abs(int((folder.start - normalized_start).total_seconds())),
                folder,
            )
            for folder in self.folders
            if abs((folder.start - normalized_start).total_seconds())
            <= tolerance_seconds
        ]
        if not candidates:
            raise ZoomRecordingNotFound(
                "Не знайдено Zoom-папку біля "
                f"{normalized_start:%d.%m.%Y %H:%M} "
                f"(допуск ±{tolerance_minutes} хв)."
            )

        nearest_delta = min(delta for delta, _folder in candidates)
        nearest = [
            folder
            for delta, folder in candidates
            if delta == nearest_delta
        ]
        if len(nearest) > 1:
            names = ", ".join(folder.path.name for folder in nearest)
            raise ZoomRecordingAmbiguous(
                f"Знайдено кілька однаково близьких Zoom-папок: {names}"
            )

        folder = nearest[0]
        if folder.video_paths:
            return ZoomRecordingMatch(
                folder=folder.path,
                start=folder.start,
                video_paths=folder.video_paths,
                delta_seconds=nearest_delta,
            )
        if folder.has_unconverted_files:
            raise ZoomRecordingPendingConversion(folder.path)
        raise ZoomRecordingNotFound(
            f"У Zoom-папці немає MP4: {folder.path.name}"
        )


def default_zoom_recordings_dir(*, home: Path | None = None) -> Path:
    user_home = (home or Path.home()).expanduser()
    candidates = (
        user_home / "Documents" / "Zoom",
        user_home / "OneDrive" / "Documents" / "Zoom",
        user_home / "OneDrive" / "Документи" / "Zoom",
        user_home / "Документи" / "Zoom",
    )
    return next((path for path in candidates if path.is_dir()), candidates[0])


def _video_order_key(path: Path) -> tuple[int, tuple[object, ...]]:
    try:
        modified = path.stat().st_mtime_ns
    except OSError:
        modified = 0
    natural_name: tuple[object, ...] = tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in _NATURAL_NUMBER_PATTERN.split(path.name)
    )
    return modified, natural_name


def _build_segments(
    *,
    folder: Path,
    folder_start: datetime,
    videos: tuple[Path, ...],
    timezone: ZoneInfo,
    metadata_probe: MetadataProbe | None,
) -> tuple[ZoomVideoSegment, ...]:
    if metadata_probe is None:
        try:
            from .media_tools import probe_mp4
        except ImportError:
            return ()
        metadata_probe = probe_mp4
    probed: list[tuple[Path, Mp4Metadata]] = []
    for path in videos:
        try:
            metadata = metadata_probe(path)
        except (LookupError, OSError, RuntimeError, ValueError):
            continue
        if metadata.duration_seconds > 0:
            probed.append((path, metadata))
    probed.sort(
        key=lambda item: _segment_order_key(
            item[0],
            item[1],
            folder_start=folder_start,
            timezone=timezone,
        )
    )
    segments: list[ZoomVideoSegment] = []
    sequential_start = folder_start
    for sequence, (path, metadata) in enumerate(probed, start=1):
        if sequence == 1:
            estimated_start = folder_start
            method = VideoTimeMethod.ZOOM_FOLDER_START
            confidence = VideoTimeConfidence.HIGH
        else:
            metadata_start = _valid_metadata_start(
                metadata.creation_time,
                folder_start=folder_start,
                earliest=sequential_start,
                timezone=timezone,
            )
            if metadata_start is not None:
                estimated_start = metadata_start
                method = VideoTimeMethod.MP4_METADATA
                confidence = VideoTimeConfidence.HIGH
            else:
                filesystem_start = _valid_filesystem_start(
                    path,
                    folder_start=folder_start,
                    earliest=sequential_start,
                    timezone=timezone,
                )
                if filesystem_start is not None:
                    estimated_start = filesystem_start
                    method = VideoTimeMethod.FILESYSTEM_TIMESTAMP
                    confidence = VideoTimeConfidence.MEDIUM
                else:
                    estimated_start = sequential_start
                    method = VideoTimeMethod.SEQUENTIAL_DURATION
                    confidence = VideoTimeConfidence.MEDIUM
        estimated_end = estimated_start + timedelta(
            seconds=metadata.duration_seconds
        )
        segments.append(
            ZoomVideoSegment(
                source_folder=folder,
                path=path,
                sequence_number=sequence,
                duration_seconds=metadata.duration_seconds,
                estimated_start=estimated_start,
                estimated_end=estimated_end,
                time_method=method,
                confidence=confidence,
                file_size=path.stat().st_size,
            )
        )
        sequential_start = estimated_end
    return tuple(segments)


def _valid_metadata_start(
    value: datetime | None,
    *,
    folder_start: datetime,
    earliest: datetime,
    timezone: ZoneInfo,
) -> datetime | None:
    if value is None:
        return None
    normalized = (
        value.replace(tzinfo=timezone)
        if value.tzinfo is None
        else value.astimezone(timezone)
    )
    if (
        normalized.date() != folder_start.date()
        or normalized < earliest
        or normalized - earliest > timedelta(minutes=10)
    ):
        return None
    return normalized


def _valid_filesystem_start(
    path: Path,
    *,
    folder_start: datetime,
    earliest: datetime,
    timezone: ZoneInfo,
) -> datetime | None:
    try:
        file_stat = path.stat()
    except OSError:
        return None
    for timestamp in (file_stat.st_ctime, file_stat.st_mtime):
        candidate = datetime.fromtimestamp(timestamp, tz=timezone)
        if (
            candidate.date() == folder_start.date()
            and earliest <= candidate <= earliest + timedelta(minutes=10)
        ):
            return candidate
    return None


def _segment_order_key(
    path: Path,
    metadata: Mp4Metadata,
    *,
    folder_start: datetime,
    timezone: ZoneInfo,
) -> tuple[int, float, tuple[object, ...]]:
    natural_name = _video_order_key(path)[1]
    if metadata.creation_time is not None:
        metadata_time = (
            metadata.creation_time.replace(tzinfo=timezone)
            if metadata.creation_time.tzinfo is None
            else metadata.creation_time.astimezone(timezone)
        )
        if metadata_time.date() == folder_start.date():
            return 0, metadata_time.timestamp(), natural_name
    try:
        file_stat = path.stat()
    except OSError:
        return 3, 0, natural_name
    created = datetime.fromtimestamp(file_stat.st_ctime, tz=timezone)
    if created.date() == folder_start.date():
        return 1, created.timestamp(), natural_name
    modified = datetime.fromtimestamp(file_stat.st_mtime, tz=timezone)
    if modified.date() == folder_start.date():
        return 2, modified.timestamp(), natural_name
    return 3, 0, natural_name
