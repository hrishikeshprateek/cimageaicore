"""Secrets at rest (application passwords). Fernet key from PUBLISH_SECRET_KEY, else generated once into data/.secret_key (0600)."""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: str | bytes | None = None, key_file: Path | None = None):
        raw = key or os.environ.get("PUBLISH_SECRET_KEY") or ""
        if not raw and key_file is not None:
            if key_file.exists():
                raw = key_file.read_text(encoding="utf-8").strip()
            else:
                raw = Fernet.generate_key().decode()
                key_file.parent.mkdir(parents=True, exist_ok=True)
                key_file.write_text(raw, encoding="utf-8")
                try:
                    key_file.chmod(0o600)
                except OSError:
                    pass
        if not raw:
            raise RuntimeError("no secret key: set PUBLISH_SECRET_KEY or give a key_file")
        self._f = Fernet(raw if isinstance(raw, bytes) else raw.encode())

    def encrypt(self, plain: str) -> str:
        return self._f.encrypt(plain.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._f.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("stored secret cannot be decrypted with the current key (PUBLISH_SECRET_KEY changed?)") from exc
