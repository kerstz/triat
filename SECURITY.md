# Security policy

## Supported versions

Triat is maintained by one person. Only the latest version on the `main` branch is supported. There are no long-term support branches and fixes are not backported.

| Version | Supported |
|---|---|
| Latest | Yes |
| Anything older | No, please update before you report |

## Report a vulnerability

Please do not open a public issue.

Report it privately through GitHub: open the [Security tab](https://github.com/kerstz/triat/security/advisories) of this repository and choose **Report a vulnerability**. Only the maintainers can see the report.

Useful things to include:

- what an attacker gains, and what they need to start (a local user, a malicious page, a device on the same network)
- the Triat version or commit, and your distribution
- steps to reproduce, or a proof of concept
- how you installed Triat (install script, Flatpak, from the source)

Do not include your own cookies or tokens in a report.

This is a spare-time project. Expect a first answer within about two weeks, and a fix when time allows, usually within a few weeks for a confirmed issue. These are estimates, not promises. If you want to be credited in the release notes, say so.

## In scope

- The sync with the Android app: pairing, encryption, the local HTTP server and the discovery on the network.
- Storage of cookies, session data and sync keys on your computer.
- Handling of files and links you give to Triat: imported `.txt`, `.m3u` and `cookies.txt` files, URLs, downloaded files and their paths.
- The install script and the Flatpak manifest, when they put your files or permissions at risk.

## Out of scope

- That Triat uses YouTube and its internal interfaces. This is how it works.
- Playback or download of YouTube content. That is what the app is for.
- Hardening in the abstract, with no way to exploit it.
- Attacks that need a computer already compromised or an attacker logged in as you.
- Problems in YouTube, Google, SponsorBlock, LRCLIB, mpv, yt-dlp or other third-party software. Report those to their authors.
- Automated scanner output with no demonstrated impact.

## Where to get Triat

Triat is distributed from this repository. A copy from anywhere else, including a package or store page that claims to be Triat, was not published by this project.
