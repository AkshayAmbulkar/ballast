# Security and privacy

Ballast runs on your own computer and handles one secret (a FRED API key) and, optionally, your own trade history. This page states what is protected, how, and what the limits are. Every guarantee marked ✅ is checked on each CI run by [`tests/test_security.py`](tests/test_security.py), [`tests/test_api.py`](tests/test_api.py) or [`tests/test_site.py`](tests/test_site.py).

## What is protected

| Guarantee | How |
|---|---|
| ✅ The API key is never stored in the project folder | Ballast reads secrets only from `~/.config/ballast/secrets.env` (or environment variables). It ignores a `.env` file inside the project and warns if it finds one. |
| ✅ The secrets file is private to your user account | Ballast warns if `secrets.env` is readable by other users and prints the `chmod 600` fix. |
| ✅ The API key never appears in error messages | Network errors from `requests` contain the full URL, key included. Ballast raises its own message instead and suppresses the original exception, and it redacts the key from any error text returned by FRED. |
| ✅ Secrets and private data are git-ignored | `.env`, `.env.*`, `secrets.env`, key files, database dumps and `data/private/` are ignored. This is a second line of defence; secrets should not be in the folder at all. |
| ✅ No keys or email addresses in the repository | A test scans every file for API-key-shaped strings and email addresses. |
| ✅ The database is not on your network | `docker-compose.yml` publishes PostgreSQL on `127.0.0.1` only, so other devices on your Wi-Fi cannot connect. |
| ✅ The container cannot see your files | The database uses a Docker-managed named volume. No folder from your computer is mounted into the container. |
| ✅ Tests cannot wipe real data | The test suite drops and rebuilds its database, so it refuses to run unless the database name ends in `_test`. |
| ✅ The API is not on your network | `ballast serve` listens on `127.0.0.1` and refuses any other address unless you pass `--allow-remote`. It rejects requests whose `Host` is not this computer (blocking DNS rebinding) and has no endpoint that writes. |
| ✅ Only the public sample portfolio is published | `ballast export-site` refuses to export a portfolio whose trades differ from `data/seed/trades.csv` (override: `--allow-private`, after checking) and refuses to write files containing a local path, an email address, the database URL or the FRED key. |
| ✅ The dashboard loads nothing from other websites | Fonts and code are served from the site itself; a Content-Security-Policy blocks everything else. No analytics or cookies. |

## What leaves your computer

| Destination | What is sent |
|---|---|
| Yahoo Finance (via `yfinance`) | The ticker symbols you load and your IP address |
| FRED, St. Louis Fed | Your API key, the series IDs requested and your IP address |
| Python Package Index | Package downloads during `pip install` |
| GitHub (only when you publish) | The repository files you upload, including `site/data/snapshot.json`: the sample portfolio's holdings, P&L and risk figures |

Nothing else is sent anywhere. Ballast has no telemetry.

## Known limits

- **The database password is a public default (`ballast`).** This is acceptable because the database only accepts connections from your own computer, and any program already running as your user could read Docker's data directly anyway. Change it if you ever expose the port.
- **Private trades are only as private as your computer.** Trades loaded from `data/private/` are stored in the local database unencrypted. Use full-disk encryption (FileVault on macOS, BitLocker on Windows).
- **Web uploads to GitHub ignore `.gitignore`.** Dragging files into GitHub's "Upload files" page uploads whatever you drop, hidden files included. Keeping secrets outside the project folder is what makes this safe.

## Your GitHub account

Commits record an author email. To keep your personal address private, enable **Settings → Emails → Keep my email addresses private** on GitHub. Commits made afterwards use an anonymous `@users.noreply.github.com` address.

## Reporting a problem

If you find a security issue, open an issue describing it **without** including any key, password or personal data.
