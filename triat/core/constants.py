"""Constantes partagées par plusieurs modules du cœur."""

from __future__ import annotations

from .. import __version__

# Trousseau de clés (Secret Service) : cookies YouTube et jeton ListenBrainz.
KEYRING_SERVICE = "triat"

# User-Agent « navigateur » : les API YouTube et les images le demandent.
BROWSER_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# User-Agent honnête, pour les services tiers qui le demandent (SponsorBlock).
APP_USER_AGENT = f"Triat/{__version__} (+lecteur de musique de bureau)"
