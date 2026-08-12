"""Encryption for venue credentials at rest, with a key Omni manages itself.

WHAT THIS DEFENDS AGAINST, PRECISELY
    The claim store's daily `pg_dump` lands in `/opt/omni-backups` and can be
    rsynced off-box. A leaked dump is the realistic threat here, and it is the
    one this closes: the ciphertext is in Postgres, the key is not.

    It does NOT defend against a compromised host. Anyone who can read the key
    file can read the credentials, and no application-level scheme changes
    that. Storing the key in the same database as the ciphertext would be
    encoding rather than encryption, so the separation is the whole point.

WHY THE KEY IS GENERATED RATHER THAN REQUIRED
    A secret the operator must mint by hand is a secret that ends up in a shell
    history, a note, or unset. Rails writes `master.key` on first run; Gitea,
    Vaultwarden and Home Assistant all generate theirs. This does the same.

    `OMNI_JWT_SECRET` deliberately has no default, and that rule is not in
    tension with this one: it objects to a signing key *shipped in source*,
    which would be identical across every deployment. A key minted at runtime
    into durable local storage is unique per deployment and never in the repo.

THE FAILURE MODE THIS REFUSES
    The api and scheduler containers have no persistent volume by default, and
    deployments here replace containers wholesale. A key generated into a
    container filesystem would vanish on the next deploy and every credential
    encrypted under it would become permanently unreadable.

    So generation requires a directory that survives: if the key cannot be
    written, this raises rather than minting one it is going to lose. An
    ephemeral key is worse than no key, because it fails later and silently.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from omni.ingest.protocol import Unavailable

logger = logging.getLogger("omni.credentials.keyring")

#: Where the key lives when it is not supplied through the environment. Mounted
#: as a named volume in `docker-compose.prod.yml` so it survives a container
#: replacement.
DEFAULT_KEY_PATH = Path("/var/lib/omni/credential.key")

#: Env var carrying a key directly, for operators who prefer 12-factor or a
#: secrets manager. Takes precedence over the file.
KEY_ENV = "OMNI_CREDENTIAL_KEY"

#: Env var overriding where the key file lives, mostly for tests.
KEY_PATH_ENV = "OMNI_CREDENTIAL_KEY_PATH"

_PREFIX = "enc:v1:"


def _fernet(key: bytes):
    from cryptography.fernet import Fernet

    return Fernet(key)


def key_path() -> Path:
    override = os.environ.get(KEY_PATH_ENV, "").strip()
    return Path(override) if override else DEFAULT_KEY_PATH


def _read_existing(path: Path) -> bytes | None:
    if not path.exists():
        return None
    raw = path.read_bytes().strip()
    if not raw:
        # A zero-length key file is a half-finished write, not a key. Treating
        # it as one would raise an opaque cryptography error at decrypt time.
        raise Unavailable(f"credential key at {path} is empty; refusing to use it")
    return raw


def _generate(path: Path) -> bytes:
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written 0600 before anything is encrypted under it. A key readable by
        # every process on the box is not meaningfully separated from the
        # ciphertext it protects.
        with os.fdopen(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR),
            "wb",
        ) as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        # Another worker won the race. Its key is as good as ours, and using
        # the one on disk is what keeps them agreeing.
        existing = _read_existing(path)
        if existing is None:
            raise Unavailable(f"credential key at {path} vanished mid-write") from None
        return existing
    except OSError as exc:
        raise Unavailable(
            f"cannot persist a credential key at {path} ({exc.strerror}). Refusing "
            f"to generate an ephemeral one: it would encrypt credentials that "
            f"become unreadable as soon as this process is replaced. Mount a "
            f"writable volume there, or set {KEY_ENV}."
        ) from exc

    logger.warning(
        "generated a new credential key at %s -- back this file up; credentials "
        "encrypted under it cannot be recovered without it",
        path,
    )
    return key


def load_key() -> bytes:
    """The key, from the environment or the key file, generating one if needed."""
    from_env = os.environ.get(KEY_ENV, "").strip()
    if from_env:
        return from_env.encode()

    path = key_path()
    existing = _read_existing(path)
    return existing if existing is not None else _generate(path)


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def encrypt(plaintext: str) -> str:
    """Encrypt a secret for storage. Already-encrypted input is passed through.

    The prefix makes the ciphertext self-describing, so a reader never has to
    guess whether a stored string is protected -- guessing is how a plaintext
    row gets silently accepted forever.
    """
    if is_encrypted(plaintext):
        return plaintext
    token = _fernet(load_key()).encrypt(plaintext.encode()).decode()
    return f"{_PREFIX}{token}"


def decrypt(value: str) -> str:
    """Decrypt a stored secret.

    A value without the marker is refused rather than returned as-is. Returning
    it would make an unencrypted row work exactly as well as an encrypted one,
    which removes any pressure to fix it and hides the fact that it is exposed.
    """
    from cryptography.fernet import InvalidToken

    if not is_encrypted(value):
        raise Unavailable(
            "stored credential is not encrypted; refusing to use it. Re-enter it "
            "so it can be written under the current key."
        )
    try:
        return _fernet(load_key()).decrypt(value[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        raise Unavailable(
            "stored credential cannot be decrypted with the current key. If the "
            f"key file was replaced or lost, the credential must be re-entered; "
            f"it is not recoverable. Check {key_path()} and {KEY_ENV}."
        ) from None


def encrypt_fields(payload: dict, fields: tuple[str, ...]) -> dict:
    """Return a copy of `payload` with the named fields encrypted."""
    out = dict(payload)
    for field in fields:
        value = out.get(field)
        if isinstance(value, str) and value:
            out[field] = encrypt(value)
    return out


def decrypt_fields(payload: dict, fields: tuple[str, ...]) -> dict:
    """Return a copy of `payload` with the named fields decrypted."""
    out = dict(payload)
    for field in fields:
        value = out.get(field)
        if isinstance(value, str) and value:
            out[field] = decrypt(value)
    return out
