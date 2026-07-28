from __future__ import annotations

from typing import Any, Protocol


class CredentialStore(Protocol):
    def get_secret(self) -> str | None: ...
    def set_secret(self, secret: str) -> None: ...
    def delete_secret(self) -> None: ...


class SecretStoreError(RuntimeError):
    """A secret could not be accessed through the configured keyring."""


class KeyringSecretStore:
    SERVICE_NAME = "LessonVideoUploader"
    TELEGRAM_API_HASH = "telegram_api_hash"

    def __init__(
        self,
        *,
        profile_id: str = "main",
        credential_name: str = TELEGRAM_API_HASH,
        backend: Any | None = None,
    ) -> None:
        self.profile_id = self._validated_part(profile_id, "profile ID")
        self.credential_name = self._validated_part(
            credential_name,
            "credential name",
        )
        self._injected_backend = backend

    @property
    def _backend(self) -> Any:
        if self._injected_backend is not None:
            return self._injected_backend
        import keyring

        return keyring

    @staticmethod
    def _validated_part(value: str, label: str) -> str:
        normalized = value.strip()
        if not normalized or ":" in normalized:
            raise ValueError(f"{label} має бути непорожнім і не містити ':'")
        return normalized

    def _account(
        self,
        profile_id: str | None = None,
        credential_name: str | None = None,
    ) -> str:
        profile = self._validated_part(
            profile_id if profile_id is not None else self.profile_id,
            "profile ID",
        )
        credential = self._validated_part(
            credential_name
            if credential_name is not None
            else self.credential_name,
            "credential name",
        )
        return f"{profile}:{credential}"

    @staticmethod
    def _access_error(operation: str) -> SecretStoreError:
        return SecretStoreError(
            "Не вдалося "
            f"{operation} секрет захищеного сховища Windows. "
            "Перевірте доступність системного сховища облікових даних."
        )

    def _set(self, account: str, value: str) -> None:
        try:
            self._backend.set_password(self.SERVICE_NAME, account, value)
        except self._backend.errors.KeyringError as error:
            raise self._access_error("зберегти") from error

    def _get(self, account: str) -> str | None:
        try:
            return self._backend.get_password(self.SERVICE_NAME, account)
        except self._backend.errors.KeyringError as error:
            raise self._access_error("прочитати") from error

    def _delete(self, account: str) -> None:
        try:
            self._backend.delete_password(self.SERVICE_NAME, account)
        except self._backend.errors.PasswordDeleteError:
            pass
        except self._backend.errors.KeyringError as error:
            raise self._access_error("видалити") from error

    def set_telegram_api_hash(self, profile_id: str, api_hash: str) -> None:
        secret = api_hash.strip()
        if not secret:
            raise ValueError("Telegram API hash не може бути порожнім")
        self._set(
            self._account(profile_id, self.TELEGRAM_API_HASH),
            secret,
        )

    def get_telegram_api_hash(self, profile_id: str) -> str | None:
        return self._get(self._account(profile_id, self.TELEGRAM_API_HASH))

    def delete_telegram_api_hash(self, profile_id: str) -> None:
        self._delete(self._account(profile_id, self.TELEGRAM_API_HASH))

    def set_secret(self, secret: str) -> None:
        normalized = secret.strip()
        if not normalized:
            raise ValueError("Секрет не може бути порожнім")
        self._set(self._account(), normalized)

    def get_secret(self) -> str | None:
        return self._get(self._account())

    def delete_secret(self) -> None:
        self._delete(self._account())


def resolve_api_hash(
    profile_id: str,
    store: CredentialStore | KeyringSecretStore | None = None,
) -> str:
    secret_store = store or KeyringSecretStore()
    profile_getter = getattr(secret_store, "get_telegram_api_hash", None)
    secret = (
        profile_getter(profile_id)
        if callable(profile_getter)
        else secret_store.get_secret()
    )
    normalized = (secret or "").strip()
    if not normalized:
        raise ValueError(
            "Telegram API hash не знайдено у захищеному сховищі. "
            "Збережіть його в налаштуваннях."
        )
    return normalized
