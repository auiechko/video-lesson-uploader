from __future__ import annotations

import ctypes
import os
from typing import Any, Protocol


class CredentialStore(Protocol):
    def get_secret(self) -> str | None: ...
    def set_secret(self, secret: str) -> None: ...
    def delete_secret(self) -> None: ...


def _windows_api() -> tuple[Any, type[ctypes.Structure]]:
    """Load Advapi32 and describe the CREDENTIALW layout it expects.

    Both are built on first use instead of at import time: ``ctypes.wintypes``
    raises on any non-Windows interpreter, and this module has to stay
    importable there so the Linux test run can reach the desktop code.
    """
    if os.name != "nt":
        raise RuntimeError("Windows Credential Manager is available only on Windows")
    from ctypes import wintypes

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", wintypes.LPVOID),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
    ]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [wintypes.LPVOID]
    api.CredFree.restype = None
    return api, _CREDENTIALW


class WindowsCredentialStore:
    """Store the Telegram API hash in Windows Credential Manager."""

    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168

    def __init__(
        self,
        target_name: str = "lesson-video-uploader/telegram-api-hash",
        username: str = "telegram",
    ) -> None:
        self.target_name = target_name
        self.username = username

    def get_secret(self) -> str | None:
        api, credential_type = _windows_api()
        pointer = ctypes.POINTER(credential_type)()
        if not api.CredReadW(
            self.target_name,
            self._CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            error = ctypes.get_last_error()
            if error == self._ERROR_NOT_FOUND:
                return None
            raise ctypes.WinError(error)
        try:
            credential = pointer.contents
            raw = ctypes.string_at(
                credential.CredentialBlob,
                credential.CredentialBlobSize,
            )
            return raw.decode("utf-16-le")
        finally:
            api.CredFree(pointer)

    def set_secret(self, secret: str) -> None:
        if not secret:
            raise ValueError("secret cannot be empty")
        api, credential_type = _windows_api()
        encoded = secret.encode("utf-16-le")
        blob = ctypes.create_string_buffer(encoded)
        credential = credential_type()
        credential.Type = self._CRED_TYPE_GENERIC
        credential.TargetName = self.target_name
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = ctypes.cast(
            blob,
            ctypes.POINTER(ctypes.c_byte),
        )
        credential.Persist = self._CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = self.username
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def delete_secret(self) -> None:
        api, _ = _windows_api()
        if not api.CredDeleteW(
            self.target_name,
            self._CRED_TYPE_GENERIC,
            0,
        ):
            error = ctypes.get_last_error()
            if error != self._ERROR_NOT_FOUND:
                raise ctypes.WinError(error)
