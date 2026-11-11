# Changelog

All notable changes to Triat are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Security

- Dependencies relocked: brotli 1.2.1 (CVE-2026-111497, decoder out-of-bounds write), certifi 2026.10.23 (PYSEC-2026-4320, emSign root removal) and, in the development lock, filelock 4.2.10 (GHSA advisories). The Flatpak requirements follow the lock, and CI now audits both lock files as written.

### Added

- A website, [kerstz.github.io/triat](https://kerstz.github.io/triat/).
- A new visual identity: logo set (app icon, mark in colour, dark, light and mono, small sizes), and a refreshed README.
- Community files: `CONTRIBUTING.md`, `SECURITY.md` (private reporting), `CODE_OF_CONDUCT.md`, issue forms, a pull request template, and a French README.
- A privacy section in the README listing the only hosts Triat contacts.

### Changed

- The legal notice now names SoundCloud for the development version and explains the download rule in plain words.
- The README describes only what the published code does.

## [0.1.0] - 2026-10-16

Initial public release, as `main` stood on 2026-10-16. The repository itself went public on 2026-09-24; everything committed between those two dates is part of this entry. No git tag has been cut yet.

### Added

- Search YouTube and play songs, with a queue, shuffle and repeat. Playback goes through mpv.
- A radio that continues when the queue runs out, with recommendations that fall back to your local listening history.
- Playlists, favourites, history and automatic playlists. Import a `.txt` of YouTube links or an `.m3u`, and export playlists.
- Synced lyrics from LRCLIB, a 10-band equaliser and SponsorBlock.
- Downloads for offline listening.
- A mini player, a full screen view and MPRIS support for media keys and `playerctl`.
- Optional YouTube cookies for your own recommendations, and optional ListenBrainz scrobbling.
- Wi-Fi sync with [Triat for Android](https://github.com/kerstz/triat-android): pairing with a code, encrypted exchange, discovery on the local network.
- `triat doctor`, which checks yt-dlp, libmpv, cookies, YouTube Music, SponsorBlock, the library and the sync.
- A command line: `search`, `play`, `cookies`, `import`, `playlists`, `export`, `stats`, `doctor`.
- An install script for Linux with a dedicated Python environment, with optional Hyprland and top bar integration.
- A hardened Flatpak manifest, built locally for now.
- Dependencies locked with hashes, and a CI workflow with tests, a dependency audit and a daily check that YouTube extraction still works.
- A legal notice, a non-affiliation statement and third-party credits (`NOTICE.md`).

### Changed

- Sync, playback, recommendations and interface reworked after a full audit, with many more tests.
- Sync keys are stored in the system keyring, with a protected file as fallback.
- Safer install and uninstall: your library is never touched by the installer.

### Security

- Media and images are only fetched from known hosts.
- The sync exchange is encrypted with AES-GCM, with keys derived at pairing.
