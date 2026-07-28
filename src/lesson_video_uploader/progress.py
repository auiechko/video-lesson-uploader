from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    student_name: str
    video_number: int
    video_count: int
    file_percent: int
    total_percent: int

    def render(self) -> str:
        return (
            f"Надсилання уроку: {self.student_name}\n"
            f"Відео: {self.video_number} з {self.video_count}\n"
            f"Загальний прогрес: {self.total_percent}%"
        )


class AlbumProgress:
    def __init__(
        self,
        *,
        student_name: str,
        video_paths: tuple[Path, ...],
        video_sizes: tuple[int, ...],
    ) -> None:
        if len(video_paths) != len(video_sizes):
            raise ValueError("each video must have one size")
        if not video_paths:
            raise ValueError("at least one video is required")
        if any(size <= 0 for size in video_sizes):
            raise ValueError("video size must be positive")
        self.student_name = student_name
        self.video_paths = video_paths
        self.video_sizes = video_sizes
        self._file_index = 0

    def start_file(self, file_index: int) -> None:
        if not 0 <= file_index < len(self.video_paths):
            raise IndexError("file index is outside the album")
        self._file_index = file_index

    def update(self, current: int, total: int) -> ProgressSnapshot:
        if total <= 0:
            raise ValueError("progress total must be positive")
        bounded_current = min(max(current, 0), total)
        completed_bytes = sum(self.video_sizes[:self._file_index])
        scaled_current = round(
            bounded_current / total * self.video_sizes[self._file_index]
        )
        total_bytes = sum(self.video_sizes)
        return ProgressSnapshot(
            student_name=self.student_name,
            video_number=self._file_index + 1,
            video_count=len(self.video_paths),
            file_percent=int(bounded_current * 100 / total),
            total_percent=int((completed_bytes + scaled_current) * 100 / total_bytes),
        )

    def completion_message(self) -> str:
        count = len(self.video_paths)
        return f"Надіслано {count} відео одним альбомом."
