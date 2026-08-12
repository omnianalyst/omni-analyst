"""Venue credentials are encrypted at rest, and the key is never lost quietly.

The dangerous failure is not a crash. It is generating a key into storage that
does not survive the next deploy: everything encrypted under it becomes
permanently unreadable, and nothing reports that until someone tries to connect
a venue weeks later.
"""

import os
import stat

import pytest

from omni.credentials import keyring
from omni.ingest.protocol import Unavailable
from omni.venue.manager import SECRET_FIELDS


@pytest.fixture(autouse=True)
def _isolated_key(tmp_path, monkeypatch):
    monkeypatch.delenv(keyring.KEY_ENV, raising=False)
    monkeypatch.setenv(keyring.KEY_PATH_ENV, str(tmp_path / "keys" / "credential.key"))
    yield


def test_a_secret_round_trips():
    token = keyring.encrypt("refresh-token-value")

    assert token != "refresh-token-value"
    assert keyring.decrypt(token) == "refresh-token-value"


def test_the_ciphertext_does_not_contain_the_secret():
    token = keyring.encrypt("hunter2-super-secret")

    assert "hunter2" not in token
    assert "super-secret" not in token


def test_the_key_file_is_created_private():
    keyring.encrypt("anything")
    path = keyring.key_path()

    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"key file is {oct(mode)}; it must not be group or world readable"


def test_a_generated_key_is_reused_rather_than_replaced():
    """Regenerating per call would make yesterday's ciphertext undecryptable."""
    first = keyring.encrypt("value")
    key_after_first = keyring.key_path().read_bytes()

    second = keyring.encrypt("value")

    assert keyring.key_path().read_bytes() == key_after_first
    assert keyring.decrypt(first) == "value"
    assert keyring.decrypt(second) == "value"


def test_it_refuses_to_generate_a_key_it_cannot_persist(tmp_path, monkeypatch):
    """An ephemeral key is worse than none: it fails later, and silently."""
    blocked = tmp_path / "readonly"
    blocked.mkdir()
    blocked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    monkeypatch.setenv(keyring.KEY_PATH_ENV, str(blocked / "nested" / "credential.key"))

    try:
        with pytest.raises(Unavailable, match="cannot persist"):
            keyring.encrypt("value")
    finally:
        blocked.chmod(stat.S_IRWXU)


def test_an_environment_key_takes_precedence_over_the_file(monkeypatch):
    from cryptography.fernet import Fernet

    supplied = Fernet.generate_key().decode()
    monkeypatch.setenv(keyring.KEY_ENV, supplied)

    token = keyring.encrypt("value")

    assert keyring.decrypt(token) == "value"
    assert not keyring.key_path().exists(), "an env key must not also mint a file"


def test_plaintext_is_refused_rather_than_silently_accepted():
    """Accepting it would make an exposed row work as well as a protected one."""
    with pytest.raises(Unavailable, match="not encrypted"):
        keyring.decrypt("raw-refresh-token")


def test_an_undecryptable_value_says_the_credential_must_be_re_entered(monkeypatch):
    from cryptography.fernet import Fernet

    token = keyring.encrypt("value")
    monkeypatch.setenv(keyring.KEY_ENV, Fernet.generate_key().decode())

    with pytest.raises(Unavailable, match="re-entered"):
        keyring.decrypt(token)


def test_an_empty_key_file_is_refused_rather_than_used(tmp_path, monkeypatch):
    path = tmp_path / "credential.key"
    path.write_bytes(b"")
    monkeypatch.setenv(keyring.KEY_PATH_ENV, str(path))

    with pytest.raises(Unavailable, match="empty"):
        keyring.encrypt("value")


def test_encrypting_twice_does_not_double_wrap():
    once = keyring.encrypt("value")
    twice = keyring.encrypt(once)

    assert once == twice
    assert keyring.decrypt(twice) == "value"


def test_field_helpers_touch_only_the_named_secrets():
    payload = {"username": "operator", "password": "s3cret", "mode": "paper"}

    stored = keyring.encrypt_fields(payload, SECRET_FIELDS["ibkr"])

    assert stored["mode"] == "paper", "non-secret fields stay readable"
    assert stored["username"] != "operator"
    assert stored["password"] != "s3cret"
    assert keyring.decrypt_fields(stored, SECRET_FIELDS["ibkr"]) == payload


def test_field_helpers_leave_absent_and_empty_values_alone():
    stored = keyring.encrypt_fields({"refresh_token": ""}, ("refresh_token",))

    assert stored["refresh_token"] == ""
    assert keyring.decrypt_fields({}, ("refresh_token",)) == {}


def test_every_venue_with_credentials_declares_which_fields_are_secret():
    """A venue missing from the map would store its secrets in the clear."""
    from omni.venue import manager

    assert set(SECRET_FIELDS) >= {"questrade", "ibkr"}
    for fields in SECRET_FIELDS.values():
        assert fields, "an empty tuple means nothing gets encrypted"
    assert manager.SECRET_FIELDS is SECRET_FIELDS


def test_the_default_key_path_is_outside_the_application_image():
    """It must live on a mounted volume, not in the container filesystem."""
    assert not str(keyring.DEFAULT_KEY_PATH).startswith("/app")
    assert os.path.dirname(str(keyring.DEFAULT_KEY_PATH)) == "/var/lib/omni"
