import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import keyring
import keyring.backend
import keyring.errors
import pytest


class _MemoryKeyring(keyring.backend.KeyringBackend):
    """Trousseau en mémoire : aucun test ne touche au vrai trousseau."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self._data: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._data.get((service, username))

    def set_password(self, service, username, password):
        self._data[(service, username)] = password

    def delete_password(self, service, username):
        if self._data.pop((service, username), None) is None:
            raise keyring.errors.PasswordDeleteError(username)


@pytest.fixture(autouse=True)
def _isolated_keyring():
    previous = keyring.get_keyring()
    keyring.set_keyring(_MemoryKeyring())
    yield
    keyring.set_keyring(previous)
