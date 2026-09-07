# SSU Membership v1

A small, server-rendered membership dashboard and Discord identity bridge, implementing [Plan.md](Plan.md) with the requested automatic Discord joining extension. An SSU employee manually manages membership after receiving Squarespace emails. SQLite is the source of truth for entitlement; the Discord `Verified` role enforces it.

## Local setup

Requires Python 3.12+ (3.12 is the tested version).

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe scripts/configure.py --local
```

Open `.env` to find the generated `ADMIN_PASSWORD` and fill in your SMTP and Discord settings. The setup script never overwrites an existing `.env`. Secrets and databases are ignored by Git. No real credentials are included.

Start the web process from this directory:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --no-access-log
```

Visit **http://localhost:8010/admin** (use `localhost`, matching `APP_URL`). In a second terminal, after Discord configuration:

```powershell
.\.venv\Scripts\python.exe -m bot.main
```

On Linux, use `.venv/bin/python` instead of `.venv\Scripts\python.exe`. Run both processes from the same directory so they resolve `.env` and the relative database URL identically. An absolute `DATABASE_URL` is recommended in production. The schema is created on startup and SQLite WAL is enabled. There is one business table and no worker queue.

You can use the dashboard before connecting external services: members are saved even if SMTP delivery fails, with a visible error and a resend action. Discord OAuth and bot operations require real configuration.

## VS Code debugging

Open this project folder in VS Code and install the recommended Python and Python Debugger extensions if prompted. In **Run and Debug**, choose a configuration and press **F5**:

- **SSU: Web app** starts the dashboard at `http://localhost:8010/admin` with Python and Jinja breakpoints enabled.
- **SSU: Discord bot** starts the bot using your `.env` credentials.
- **SSU: Web + Bot** starts both; stopping the compound stops both processes.
- **SSU: Tests** runs the mocked test suite under the debugger.

These configurations use the project’s `.venv` on Windows, macOS, and Linux. Web and bot load `.env` and override only the local URL and cookie settings for HTTP development. Register `http://localhost:8010/auth/discord/callback` in Discord for local OAuth testing. Stop any terminal-launched web server on port 8010 before starting the web debugger. Restart debugging after Python changes; auto-reload is intentionally disabled for predictable breakpoints.

## Discord setup

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications). Copy its application ID and client secret into `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET`.
2. Register the exact redirect URI `${APP_URL}/auth/discord/callback`. For local development it is `http://localhost:8010/auth/discord/callback`; production is `https://members.ssu-apps.link/auth/discord/callback`.
3. Create the bot, put its token in `DISCORD_BOT_TOKEN`, and enable **Server Members Intent**. Message Content Intent is unnecessary. See [discord.py’s intent guide](https://discordpy.readthedocs.io/en/stable/intents.html).
4. Install the bot in the SSU server with the **bot** scope and **Manage Roles + Create Invite** permissions (combined permission integer `268435457`). Create Invite is required by Discord’s automatic joining endpoint. It does not need Administrator, channel management, kick, or ban permissions.
5. Enable Developer Mode in Discord, copy the server ID and `Verified` role ID into `.env`, and position the bot’s highest role **above Verified**. See [Discord role permissions](https://docs.discord.com/developers/topics/permissions).
6. Configure subscriber channels manually: `@everyone` can access welcome/how-to-join/support, while `Verified` grants subscriber channel access. Audit other roles and channel overrides so they do not accidentally grant subscriber access.
7. Create one permanent Discord invite and set `DISCORD_INVITE_URL`. It is only a recovery path if automatic joining fails or a linked member later leaves the server.

Member OAuth requests [`identify` and `guilds.join`](https://docs.discord.com/developers/topics/oauth2). After validating and committing the identity, the app uses the short-lived user access token with Discord’s [Add Guild Member API](https://docs.discord.com/developers/resources/guild#add-guild-member), then applies `Verified`. The bot and OAuth client must belong to the same Discord application. Already-joined users are handled idempotently. The completion page opens the server directly; no separate invite acceptance is needed in the normal flow. Access and refresh tokens are never persisted. Role enforcement uses the bot-authenticated [guild member role API](https://docs.discord.com/developers/resources/guild#add-guild-member-role). Discord may still require its own screening or account eligibility checks.

## SMTP

Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, and any required authentication. For implicit TLS (usually port 465), use `SMTP_USE_SSL=true` and `SMTP_USE_STARTTLS=false`. For STARTTLS (usually port 587), reverse those booleans. Both cannot be enabled together. Configure the sender’s SPF/DKIM with your mail provider and verify delivery to a test mailbox.

The app sends a plain-text registration email over standard SMTP. It commits the member and token hash before sending. SMTP failures leave the member available for retry. A resend always invalidates the previous link, including after a failed delivery; the raw token is never stored or logged.

## Employee workflows

- **Create member:** enter the subscription email; it is trimmed, lowercased, validated, and deduplicated. A private, non-expiring link is emailed.
- **Search:** email, Discord username, display name, or user ID. The directory groups active/inactive records and paginates at 50 records.
- **Edit email:** available for every member. For unlinked members, changing the address also rotates and emails a new link so the old address cannot claim the membership. Linked accounts keep their existing Discord association.
- **Resend invite:** for active, unlinked members. Rotates the link. Linked members require an explicit reset; inactive members can be reactivated first.
- **Deactivate:** immediately commits `active=false`, keeps the Discord association, and removes only `Verified`. The person stays in the server.
- **Reactivate:** restores entitlement and synchronizes the existing Discord account without another OAuth flow.
- **Reset Discord link:** opens a confirmation page identifying the old account and destination email. Removes the old role, clears the association, rotates the registration link, and emails it. Membership activity is unchanged. If role removal fails, the old association is kept and the employee must retry; this prevents untracked access. A stale confirmation cannot reset a newly linked account.

The member follows the email link and clicks **Connect Discord & join**. Consent links their identity, adds them to the server, and grants `Verified`. Already-joined members also work. A claimed link shows the existing connection and cannot switch identities. Inactive memberships cannot claim links or use the completion page to obtain an invite. If automatic joining fails (for example, missing bot permissions or a Discord account/server limit), the link remains claimed and the completion page offers the permanent invite. The bot can retry role synchronization, but cannot retry OAuth joining after the user token is discarded.

“Linked” in the directory means identity connected, not a cached assertion about Discord role or presence. No `verified` or `in_discord` database fields are stored.

## Reliability and security

- Random registration tokens use `secrets.token_urlsafe(32)`; only SHA-256 hashes are stored. Rotation invalidates pending OAuth for an older link too.
- OAuth state is random, signed into the browser session, checked for a 10-minute lifetime, and consumed at callback. The database enforces unique email and Discord identity. The account claim is atomic, including concurrent callbacks.
- Admin login has CSRF protection, constant-time password comparison, a basic per-IP 10-attempt/15-minute throttle, and an absolute seven-day session lifetime. Changing the admin password invalidates existing admin sessions. Run one Uvicorn worker; the throttle is in-process and resets on restart.
- Production cookies are HttpOnly, Secure, and SameSite=Lax. Insecure cookies are allowed only for explicit localhost development. Pages use no external assets, no script, and `no-store`; production adds HSTS. Public registration/OAuth pages use `no-referrer` to protect links and codes. Admin pages use `same-origin` so browser form POSTs retain their Origin header for CSRF validation without sending referrers to other sites.
- Role synchronization always rereads entitlement under a short SQLite write reservation shared by both processes. This serializes Discord changes with resets and entitlement updates. Database lock waits run in threads, not on the async event loops. Discord requests have timeouts and a bounded rate-limit retry; longer rate limits/outages are retried by reconciliation.
- The bot handles joins instantly and reconciles all linked records on startup after gateway readiness and every five minutes. Failures are isolated per member; unexpected cycle failures do not terminate the loop. Unknown Discord accounts are left alone, as specified in the plan. Reserve `Verified` for app-managed memberships; reconciliation does not strip it from unrelated/untracked accounts.
- Entitlement commits survive a Discord outage. Failed synchronization is visible to the employee and retried while the bot is running. A reset deliberately retains its association on a failed role removal.
- Do not enable URL access logs at the web server or proxy: registration paths and OAuth callback queries contain credentials. Supplied commands/services disable Uvicorn access logs and omit Caddy access logging. Application logs identify operations by internal member ID and do not print email bodies, passwords, tokens, or API response bodies.

## Ubuntu deployment

Use Ubuntu with Python 3.12+, Caddy, and systemd. Install Python’s venv support and Caddy through your usual system package process. Point DNS for `members.ssu-apps.link` to the server and allow inbound ports 80/443 for Caddy. Port 8010 binds to loopback only.

Place the project at `/opt/ssu-membership`, then:

```bash
sudo useradd --system --home /opt/ssu-membership --shell /usr/sbin/nologin ssu-membership
cd /opt/ssu-membership
sudo python3 -m venv .venv
sudo .venv/bin/python -m pip install -r requirements.lock
sudo .venv/bin/python -m pip install --no-deps -e .
sudo .venv/bin/python scripts/configure.py
sudo install -d -o ssu-membership -g ssu-membership -m 700 data backups
sudo chown root:ssu-membership .env
sudo chmod 640 .env
```

Edit `.env` with production settings. Keep `APP_URL=https://members.ssu-apps.link`, `SESSION_COOKIE_SECURE=true`, and use `DATABASE_URL=sqlite:////opt/ssu-membership/data/app.db`. Configure the SMTP and Discord values above. Keep code and the virtual environment read-only to the service user; only `data` and `backups` need write access.

Install and start the supplied units:

```bash
sudo cp deploy/ssu-membership-*.service deploy/ssu-membership-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ssu-membership-web ssu-membership-bot ssu-membership-backup.timer
```

Add the site block from `deploy/Caddyfile` to `/etc/caddy/Caddyfile` (preserve other existing sites), then:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl --fail https://members.ssu-apps.link/healthz
sudo journalctl -u ssu-membership-web -u ssu-membership-bot --since '10 minutes ago'
```

`/healthz` checks the web process and database, not SMTP delivery or gateway connectivity. Confirm the bot’s “connected” log and verify there are no role configuration errors. Perform a test-member smoke check: email delivery → OAuth → join → Verified → deactivate → role removed → reactivate → role restored → reset. Automated tests use mocks and do not establish production connectivity. The deployment files are supplied for your server; local implementation does not publish the application or change your Discord server.

## Backups and restore

The daily systemd timer uses SQLite’s online backup API, checks integrity, and retains 30 days of generated backups. It runs a short-lived maintenance command, not an additional long-running application process. Run manually:

```bash
.venv/bin/python -m app.backup --destination backups --retention-days 30
```

Copy these backups to your private off-server backup destination using your existing backup tooling; local disk backups alone do not survive disk loss. They contain membership email addresses and Discord IDs. Back up `.env` separately in a secure secrets store. Do not copy a live `app.db` alone: committed transactions may still be in its WAL file.

Restore only with **both application services stopped**:

1. Stop `ssu-membership-web` and `ssu-membership-bot` and create an extra online backup of the current database.
2. Preserve the entire existing `data` directory (including `app.db-wal` and `app.db-shm` if present) in a separate recovery location, then create a fresh `data` directory with service-user ownership and mode 700.
3. Copy the selected verified `.sqlite3` backup into the fresh directory as `app.db`; give it service-user ownership and mode 600. Never mix an older database with newer WAL/SHM files.
4. Start both services, check `/healthz`, and inspect reconciliation logs. Discord will be synchronized to the restored entitlement snapshot. Review membership changes made since that backup before restoring production access.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

Tests use a temporary real SQLite database and mocked HTTP/SMTP behavior. They cover the plan’s essential lifecycle cases, authentication/CSRF, OAuth expiry and conflicts, concurrent claims/role updates, failure recovery, token invalidation, backups, and HTTP error handling. `requirements.lock` records the tested runtime dependency versions; development tools are installed with `.[dev]`.

Out of scope, as planned: Squarespace APIs/webhooks, automatic cancellation, member passwords/dashboards, multiple admins, payments/tiers, channel management, Redis/Celery/Postgres, Docker, and a frontend SPA.
