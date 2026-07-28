from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import AppConfig, load_config
from .manifest import UploadManifest, load_manifest, render_manifest_preview
from .models import Lesson, SendStatus
from .persistence import SQLiteSendItemRepository
from .planning import plan_albums
from .reconciliation import TelegramDeliveryReconciler
from .sender import ManualReviewRequired, TelethonLessonSender


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lesson-video-uploader",
        description="Надсилання всіх MP4 одного уроку одним Telegram-альбомом.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    preview = subcommands.add_parser("preview", help="Переглянути план без надсилання")
    preview.add_argument("manifest", type=Path)
    preview.add_argument(
        "--expand",
        action="store_true",
        help="Показати впорядковані файли кожного уроку",
    )
    send = subcommands.add_parser("send", help="Надіслати уроки через Telethon")
    send.add_argument("manifest", type=Path)
    send.add_argument("--config", type=Path, default=Path("config.toml"))
    send.add_argument(
        "--database",
        type=Path,
        default=Path(".lesson-video-uploader") / "deliveries.sqlite3",
    )
    reconcile = subcommands.add_parser(
        "reconcile",
        help="Перевірити невідому доставку за останніми повідомленнями Telegram",
    )
    reconcile.add_argument("manifest", type=Path)
    reconcile.add_argument("--config", type=Path, default=Path("config.toml"))
    reconcile.add_argument(
        "--database",
        type=Path,
        default=Path(".lesson-video-uploader") / "deliveries.sqlite3",
    )
    return parser


def _load_optional_config(path: Path) -> AppConfig:
    return load_config(path) if path.is_file() else load_config()


def _telegram_credentials(config: AppConfig) -> tuple[int, str]:
    if config.api_id is None:
        raise ValueError("telegram.api_id is required in config.toml")
    api_hash = os.environ.get(config.api_hash_env)
    if not api_hash:
        raise ValueError(
            f"set the {config.api_hash_env} environment variable with Telegram api_hash"
        )
    return config.api_id, api_hash


def _telegram_client_class():
    try:
        from telethon import TelegramClient
    except ModuleNotFoundError as error:
        raise ValueError(
            "Telethon is not installed; run: python -m pip install -e ."
        ) from error
    return TelegramClient


def _delivery_summary(result: Lesson) -> str:
    if result.status is not SendStatus.SENT:
        raise ManualReviewRequired(
            f"Delivery status is {result.status.value}; inspect Telegram before retrying."
        )
    album_count = len(plan_albums(result))
    if album_count == 1:
        return (
            f"Надіслано {len(result.telegram_message_ids)} "
            "відео одним альбомом."
        )
    return (
        f"Надіслано {len(result.telegram_message_ids)} відео "
        f"{album_count} альбомами."
    )


async def _send(
    manifest: UploadManifest,
    config: AppConfig,
    database_path: Path,
) -> None:
    api_id, api_hash = _telegram_credentials(config)
    TelegramClient = _telegram_client_class()

    repository = SQLiteSendItemRepository(database_path)
    async with TelegramClient(config.session, api_id, api_hash) as client:
        sender = TelethonLessonSender(
            client,
            repository,
            album_batch_template=config.album_batch,
        )
        for lesson in manifest.lessons:
            print(f"Надсилання уроку: {lesson.caption}")

            def warn(caption: str, album_count: int) -> None:
                print(
                    f"Увага: урок «{caption}» має понад 10 відео "
                    f"і буде розділений на {album_count} альбоми."
                )

            def progress(current: int, total: int) -> None:
                percent = int(current * 100 / total) if total else 0
                print(f"\rЗагальний прогрес: {percent:3d}%", end="", flush=True)

            result = await sender.send(
                lesson,
                target_peer=manifest.target_peer,
                progress_callback=progress,
                album_split_warning=warn,
            )
            print()
            print(_delivery_summary(result))


async def _reconcile(
    manifest: UploadManifest,
    config: AppConfig,
    database_path: Path,
) -> None:
    api_id, api_hash = _telegram_credentials(config)
    TelegramClient = _telegram_client_class()
    repository = SQLiteSendItemRepository(database_path)
    async with TelegramClient(config.session, api_id, api_hash) as client:
        reconciler = TelegramDeliveryReconciler(
            client,
            repository,
            album_batch_template=config.album_batch,
        )
        for lesson in manifest.lessons:
            result = await reconciler.reconcile(
                lesson,
                target_peer=manifest.target_peer,
            )
            if result.status is SendStatus.SENT:
                print(f"{lesson.caption}: підтверджено SENT")
            elif result.status is SendStatus.PARTIALLY_CONFIRMED:
                print(
                    f"{lesson.caption}: PARTIALLY_CONFIRMED — "
                    "потрібна ручна перевірка"
                )
            else:
                print(
                    f"{lesson.caption}: DELIVERY_UNKNOWN — "
                    "однозначного збігу не знайдено"
                )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        manifest = load_manifest(args.manifest)
        if args.command == "preview":
            print(render_manifest_preview(manifest, expand=args.expand))
            return 0
        config = _load_optional_config(args.config)
        if args.command == "send":
            asyncio.run(_send(manifest, config, args.database))
        else:
            asyncio.run(_reconcile(manifest, config, args.database))
        return 0
    except (OSError, ValueError, ManualReviewRequired) as error:
        print(f"Помилка: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
