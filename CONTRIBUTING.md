# Contributing to Triat

Thank you for your interest in Triat, a quiet music player for Linux. It is a personal project maintained by one person, so please be patient.

## Ways to help

### Report a bug

Open an issue with the bug report form. Before you do, check that nobody has reported it already. The form asks for:

- the output of `triat doctor`
- your distribution and how you installed Triat (install script, Flatpak, from the source)
- what you did, what you expected and what happened

Never paste cookies, tokens or the content of `cookies.txt` in an issue. If something looks like a security problem, do not open a public issue: see [SECURITY.md](SECURITY.md).

When playback suddenly breaks, YouTube has probably changed something. Update yt-dlp first (`~/.local/lib/triat/venv/bin/pip install -U yt-dlp`), then report it if it still fails.

### Suggest a feature

Open an issue with the feature request form. Say what you want to do and why, not only how. Triat aims to stay small and quiet, so some ideas will be declined.

### Send a pull request

Small, focused changes are welcome: a fix, a test, a translation of the docs. For anything bigger, open an issue first so we can agree on the direction before you spend time on it.

1. Fork the repository and create a branch.
2. Set up the environment:

   ```sh
   python -m venv --system-site-packages .venv
   .venv/bin/pip install -r requirements.txt pytest ruff
   ```

   PyGObject, GTK 4, libadwaita and libmpv come from your distribution. See the README for the package names.
3. Run the checks before you push:

   ```sh
   .venv/bin/python -m pytest -q
   ruff check triat tests
   ```

4. Open the pull request and fill in the template.

Notes on the code:

- Python 3.13 or newer.
- Code comments and interface text are mostly in French. Follow the language of the file you touch.
- Keep the app local. A change must not add telemetry, analytics or a new network host without a clear reason written in the pull request.
- Do not commit cookies, tokens, personal playlists or APKs.

## Using AI to contribute

A large part of this project was written with AI assistance. You may use it too, as long as you read, run and understand what you send. Say so in the pull request if the change is mostly generated.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By taking part, you agree to follow it.

## License

By contributing, you agree that your work is released under the [MIT License](LICENSE) of this project.
