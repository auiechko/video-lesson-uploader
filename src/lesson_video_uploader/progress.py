from __future__ import annotations


class LessonUploadProgress:
    """Turn Telethon's per-file byte counts into progress across one lesson.

    Telethon reports ``(sent, total)`` for each file separately, so a bare
    percentage restarts at zero for every video: a ten-video album walks the
    bar from 0 to 100 ten times. File sizes are known before the upload
    starts, which is enough to place each per-file report inside the lesson.
    """

    def __init__(self, video_sizes: tuple[int, ...]) -> None:
        if not video_sizes:
            raise ValueError("at least one video is required")
        if any(size <= 0 for size in video_sizes):
            raise ValueError("video size must be positive")
        self.video_sizes = video_sizes
        self.total_bytes = sum(video_sizes)
        self._completed_bytes = 0

    def observe(self, current: int, total: int) -> tuple[int, int]:
        """Report one Telethon callback and return lesson-wide byte counts.

        A file is treated as finished once it reports its own total, which is
        what advances the baseline. Equal-sized videos therefore stay
        distinguishable, unlike any scheme based on watching the counter
        reset.
        """
        if total <= 0:
            raise ValueError("progress total must be positive")
        sent = min(max(current, 0), total)
        position = min(self._completed_bytes + sent, self.total_bytes)
        if sent >= total:
            self._completed_bytes = min(
                self._completed_bytes + total,
                self.total_bytes,
            )
        return position, self.total_bytes
