# Third-party notices

Triat itself is released under the MIT License (see `LICENSE`). It relies on
the following components, which keep their own licenses. Versions are pinned
in `requirements.lock`.

## Python libraries (installed with pip)

| Component | License |
|---|---|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense |
| [yt-dlp-ejs](https://github.com/yt-dlp/ejs) | Unlicense AND MIT AND ISC |
| [ytmusicapi](https://github.com/sigma67/ytmusicapi) | MIT |
| [python-mpv](https://github.com/jaseg/python-mpv) | GPL-2.0-or-later OR LGPL-2.1-or-later |
| [zeroconf](https://github.com/python-zeroconf/python-zeroconf) | LGPL-2.1-or-later |
| [keyring](https://github.com/jaraco/keyring) | MIT |
| [SecretStorage](https://github.com/mitya57/secretstorage) | BSD-3-Clause |
| [cryptography](https://github.com/pyca/cryptography) | Apache-2.0 OR BSD-3-Clause |
| [qrcode](https://github.com/lincolnloop/python-qrcode) | BSD |
| [requests](https://github.com/psf/requests) | Apache-2.0 |
| [urllib3](https://github.com/urllib3/urllib3) | MIT |
| [certifi](https://github.com/certifi/python-certifi) | MPL-2.0 |
| [ifaddr](https://github.com/pydron/ifaddr) | MIT |

## System libraries (from the distribution or the Flatpak runtime)

| Component | License |
|---|---|
| [PyGObject](https://gitlab.gnome.org/GNOME/pygobject) | LGPL-2.1-or-later |
| GTK 4, libadwaita | LGPL-2.1-or-later |
| [libmpv](https://mpv.io) | GPL-2.0-or-later, or LGPL-2.1-or-later when built with `-Dgpl=false` |
| FFmpeg | LGPL-2.1-or-later or GPL, depending on the build |

## Fonts

Doto, Geist and Geist Mono, under the SIL Open Font License 1.1
(`data/fonts/OFL-*.txt`).

## Data and services

| Service | Use | Terms |
|---|---|---|
| [SponsorBlock](https://sponsor.ajay.app) | Segments to skip, fetched at run time | Data under [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) |
| [LRCLIB](https://lrclib.net) | Synced lyrics | See lrclib.net |
| [ListenBrainz](https://listenbrainz.org) | Optional scrobbling | See listenbrainz.org |
| YouTube / YouTube Music | Search and playback | YouTube Terms of Service. Triat is not affiliated with Google LLC. |

## SoundCloud

- « SoundCloud » est cité à titre descriptif. Triat n'a aucune affiliation
  avec SoundCloud et n'est ni approuvé ni soutenu par lui.
- La prise en charge est **expérimentale** et **désactivée par défaut**. Elle
  s'active dans Réglages › Lecture › Sources, après un avertissement.
- Elle utilise l'API web non documentée de SoundCloud. L'identifiant client
  est découvert à l'exécution et n'est jamais stocké dans le dépôt.
- Triat ne télécharge rien. Aucun contenu chiffré (DRM) n'est demandé ni lu,
  et les extraits Go+ ne sont pas lus.
- Les pochettes SoundCloud ne vont pas dans le cache disque : des fichiers
  temporaires privés, effacés à la fermeture, servent pendant la session.
- Les pistes SoundCloud ne sont jamais synchronisées avec Android.
- Source : `docs/review/SPEC-SOUNDCLOUD-DESKTOP.md`, §7.
