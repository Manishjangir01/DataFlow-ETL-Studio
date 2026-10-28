"""Encrypted storage for API keys, tokens and database passwords.

Secrets entered in the UI are encrypted immediately with a Fernet key (AES-128-CBC +
HMAC-SHA256) and only the ciphertext is stored in pipeline configurations. A secret can
alternatively reference an environment variable, in which case nothing is stored at all.

Decrypted values are registered with the redaction registry so they can never be
printed in logs or error messages.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal, Mapping

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.security.redaction import register_secret
from src.utils.exceptions import SecretError

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
KEY_FILE_NAME = "secret.key"


class SecretRef(BaseModel):
    """Reference to a secret: Fernet ciphertext or the name of an environment variable."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["encrypted", "env"]
    value: str = Field(min_length=1, max_length=8192)

    @model_validator(mode="after")
    def _check_env_name(self) -> "SecretRef":
        if self.kind == "env" and not _ENV_NAME.match(self.value):
            raise ValueError(
                "Environment variable names may only contain letters, digits and underscores."
            )
        return self

    def describe(self) -> str:
        """Human-readable description that never reveals the secret."""
        if self.kind == "env":
            return f"from environment variable {self.value}"
        return "saved (encrypted)"


class SecretBox:
    """Encrypts and decrypts secrets with a Fernet key."""

    def __init__(self, key: str | bytes) -> None:
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise SecretError(
                "DATAFLOW_SECRET_KEY is not a valid Fernet key.",
                hint="Generate one with: python -m src.cli generate-key",
            ) from exc

    def encrypt(self, plaintext: str) -> SecretRef:
        if not plaintext:
            raise SecretError("Cannot store an empty secret.")
        register_secret(plaintext)
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return SecretRef(kind="encrypted", value=token)

    def decrypt(self, token: str) -> str:
        try:
            value = self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise SecretError(
                "A saved secret could not be decrypted.",
                hint="The encryption key changed. Re-enter the secret in the pipeline settings.",
            ) from exc
        register_secret(value)
        return value

    def resolve(self, ref: SecretRef | None, env: Mapping[str, str] | None = None) -> str | None:
        """Return the plaintext value of ``ref`` (registered for redaction)."""
        if ref is None:
            return None
        if ref.kind == "encrypted":
            return self.decrypt(ref.value)
        environment = os.environ if env is None else env
        value = environment.get(ref.value)
        if not value:
            raise SecretError(
                f"Environment variable {ref.value} is not set or is empty.",
                hint="Add it to your .env file or system environment and restart the app.",
            )
        register_secret(value)
        return value


def generate_key() -> str:
    """Return a new random Fernet key."""
    return Fernet.generate_key().decode("ascii")


def load_or_create_key(secret_key: str | None, config_dir: Path) -> tuple[str, str]:
    """Return ``(key, origin)``; creates ``config/secret.key`` when no key is configured.

    ``origin`` is ``"environment"`` or ``"file"`` and is shown on the Settings page.
    """
    if secret_key:
        return secret_key, "environment"
    key_file = config_dir / KEY_FILE_NAME
    if key_file.is_file():
        return key_file.read_text(encoding="ascii").strip(), "file"
    config_dir.mkdir(parents=True, exist_ok=True)
    key = generate_key()
    # Create the file with owner-only permissions where the OS supports it.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(key_file, flags, 0o600)
    except FileExistsError:  # created concurrently by another process
        return key_file.read_text(encoding="ascii").strip(), "file"
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(key)
    return key, "file"
