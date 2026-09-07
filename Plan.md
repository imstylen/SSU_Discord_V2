## Approved implementation amendment — automatic Discord joining

The user requested automatic server joining during OAuth instead of separate connect/join steps. This supersedes the original manual-join exclusions below:

* Request `identify guilds.join` in member OAuth consent.
* Validate and commit the one-to-one member identity before calling Discord’s Add Guild Member endpoint with the transient user access token and the same application's bot token.
* Join only active, successfully linked memberships, then synchronize `Verified`. Already-joined users are supported.
* Discard OAuth tokens after use; do not persist them or add a worker.
* The bot additionally needs Create Invite permission (Manage Roles + Create Invite = `268435457`).
* Show “Open SSU in Discord” after success; retain the permanent invite only as a failure/rejoin fallback.

The remainder is the original V1 baseline.

## Recommended V1 Architecture

I think we have enough decisions now to lock down the architecture.

The guiding principle should be:

> **The SQLite database is the source of truth for whether a Discord account is entitled to the `Verified` role.**

Discord does not decide whether someone is subscribed. Squarespace does not talk to the application. The SSU employee manually changes membership state in the dashboard.

```text
              ┌─────────────────┐
              │   Squarespace   │
              │   Subscription  │
              └────────┬────────┘
                       │ email notification
                       ▼
                SSU Employee
                       │
                       ▼
              ┌─────────────────┐
              │   Admin Web UI  │
              │    FastAPI      │
              └────────┬────────┘
                       │
                  ┌────▼────┐
                  │ SQLite  │
                  └────┬────┘
                       │
          ┌────────────┴────────────┐
          │                         │
          ▼                         ▼
   Discord REST API           Discord Bot
   add/remove role            watches joins
                                    │
                                    ▼
                              SSU Discord
```

### 1. Technology stack

I would use:

* Python
* FastAPI
* Jinja2
* SQLAlchemy
* SQLite
* `discord.py`
* `httpx` for Discord OAuth/API requests
* Standard SMTP for email
* Caddy
* systemd
* `.env` configuration

I would **not** introduce React, Redis, Celery, Postgres, Docker, HTMX, background workers, or a separate API service for V1.

Even HTMX isn't particularly useful here. Traditional HTML forms + POST + redirect will keep the app extremely easy to understand.

---

# 2. Member lifecycle

There are essentially four meaningful states.

| Active | Discord linked | In server | Result                         |
| ------ | -------------- | --------- | ------------------------------ |
| Yes    | No             | —         | Waiting for Discord connection |
| Yes    | Yes            | No        | Waiting for user to join       |
| Yes    | Yes            | Yes       | `Verified`                     |
| No     | Yes            | Either    | No `Verified`                  |

We don't actually need a `status` database field. These states can be derived from the member record and Discord.

The important database fields are simply:

```text
active
discord_user_id
```

---

# 3. Complete new-member flow

### Employee side

Employee receives the normal Squarespace subscription notification.

They visit:

```text
https://members.ssu-apps.link/admin
```

and log in using the single admin password.

Dashboard:

```text
SSU Community Admin

[ + Add Member ]

Search: [________________________]

ACTIVE MEMBERS

Email                 Discord            Status
----------------------------------------------------------
jane@example.com      @janedoe           Linked
bob@example.com       Not connected      Invite Sent
sam@example.com       @sammy             Linked

INACTIVE MEMBERS

old@example.com       @oldmember         Inactive
```

Employee clicks:

```text
+ Add Member
```

and enters:

```text
Email
jane@example.com

[Create Member & Send Link]
```

---

# 4. Registration link

The application generates something like:

```text
https://members.ssu-apps.link/join/x8jkGwNkc...random-token...
```

The token should be generated using Python's cryptographically secure `secrets` module.

For example, conceptually:

```python
secrets.token_urlsafe(32)
```

That's roughly 256 bits of randomness.

### Important implementation detail

Don't store the actual token in SQLite.

Store:

```text
SHA256(token)
```

Then when someone visits:

```text
/join/<token>
```

the server hashes `<token>` and looks it up.

That means a database leak doesn't expose usable registration links.

### No expiration

Per your decision:

**The registration link does not expire based on time.**

But it becomes effectively claimed once the Discord account is connected.

Before linking:

```text
Welcome to Sunny Side Up!

Your SSU membership is ready.

Connect your Discord account so we know which
Discord member belongs to your subscription.

[ Connect Discord ]
```

After linking:

```text
You're connected!

Discord account:
@janedoe

Your SSU membership is now linked.

[ Join the SSU Discord ]
```

The button uses your single permanent Discord invite.

---

# 5. Discord OAuth

Clicking **Connect Discord** starts a normal Discord OAuth authorization-code flow.

We only need:

```text
scope=identify
```

We specifically **do not need**:

```text
email
guilds
guilds.join
```

Discord's `identify` scope gives us the stable Discord user ID plus basic profile information such as username and global display name. ([Documentation - Discord][1])

Flow:

```text
/join/TOKEN
     │
     ▼
Connect Discord
     │
     ▼
/auth/discord/start
     │
     ▼
discord.com/oauth2/authorize
     │
     ▼
User authorizes
     │
     ▼
/auth/discord/callback
     │
     ▼
exchange authorization code
     │
     ▼
GET /users/@me
     │
     ▼
Discord User ID
     │
     ▼
Store on Member
```

We store:

```text
discord_user_id
discord_username
discord_global_name
discord_linked_at
```

The Discord OAuth access token has served its purpose at that point and can be discarded. We don't need refresh tokens or long-term authorization.

### OAuth security

Use OAuth `state` to protect the callback.

The app should generate a short-lived random OAuth state and put it in the user's signed session before redirecting them to Discord.

That is separate from your permanent registration token.

---

# 6. Enforcing one-to-one linking

At the database level:

```text
email UNIQUE
discord_user_id UNIQUE
```

This gives us:

```text
1 subscription email
        ↕
1 Discord account
```

If Alice's Discord account is already associated with another subscription:

```text
This Discord account is already connected
to another SSU membership.

Please contact SSU for assistance.
```

Do not silently move the Discord account.

An admin would have to explicitly reset the old association.

Emails should be normalized before storing:

```python
email.strip().lower()
```

---

# 7. Discord server behavior

The Discord server itself handles channel security.

The bot does **not** configure channels.

You manually configure Discord so that:

```text
@everyone
   │
   ├── #welcome
   ├── #how-to-join
   └── #support
```

are accessible.

And:

```text
Verified
   │
   ├── community channels
   ├── forums
   ├── voice channels
   └── everything subscriber-only
```

The `Verified` role is therefore the access-control mechanism.

---

# 8. Bot behavior when someone joins

The bot listens for:

```python
on_member_join
```

Discord requires the `GUILD_MEMBERS` Gateway intent to receive Guild Member Add events. That intent is considered privileged and needs to be enabled for the application in Discord's Developer Portal. ([Documentation - Discord][2])

When someone joins:

```text
Discord member joins
       │
       ▼
Get Discord user ID
       │
       ▼
SELECT member
WHERE discord_user_id = ?
       │
       ├── not found ────────────────► do nothing
       │
       ▼
Is active?
       │
   ┌───┴───┐
   │       │
  YES      NO
   │       │
   ▼       ▼
Add     Ensure no
Verified Verified
```

So someone can share the permanent Discord invite all they want.

A random person joining SSU simply gets:

```text
@everyone
```

and cannot access subscriber channels.

---

# 9. What happens if they connect Discord after already joining?

This is why I recommend allowing the **web application itself to call the Discord REST API using the bot token**.

After OAuth:

```text
Discord linked
      │
      ▼
Check whether Discord ID is in SSU guild
      │
   ┌──┴───┐
   │      │
 YES      NO
   │      │
   ▼      ▼
Add      Wait for
Verified them to join
```

Discord provides REST endpoints to add and remove roles from guild members, authenticated by the bot and requiring `MANAGE_ROLES`. ([Documentation - Discord][3])

This handles either ordering:

### Normal

```text
Connect Discord
→ Join server
→ bot adds Verified
```

### Already joined

```text
Join server
→ Connect Discord later
→ web app adds Verified
```

---

# 10. Deactivation

Admin clicks:

```text
[Deactivate]
```

The web app immediately does:

```text
member.active = false
member.deactivated_at = now()
COMMIT
```

Then:

```text
DELETE Discord Verified role
```

using the bot REST API.

Discord specifically supports removing individual member roles through its API. ([Documentation - Discord][3])

The person stays in Discord.

They simply lose `Verified`.

Therefore:

```text
Subscriber cancels

Squarespace
     │
     ▼
SSU receives cancellation notification
     │
     ▼
Employee finds member
     │
     ▼
Deactivate
     │
     ├── Database: active = false
     │
     └── Discord: remove Verified
```

Their Discord association stays intact.

---

# 11. Reactivation

If they resubscribe later:

```text
[Reactivate]
```

does:

```text
active = true
deactivated_at = NULL
```

If their Discord account is currently in the server:

```text
add Verified
```

They don't need another OAuth flow.

This is one reason I strongly prefer deactivate/reactivate over deleting records.

---

# 12. Reset Discord Account

We should include one administrative escape hatch:

```text
[Reset Discord Link]
```

This is useful if someone:

* linked the wrong Discord account
* lost their Discord account
* wants to move their subscription

Process:

```text
Admin clicks Reset Discord Link

        ↓

Remove Verified from old Discord account

        ↓

discord_user_id = NULL
discord_username = NULL
discord_global_name = NULL
discord_linked_at = NULL

        ↓

Generate NEW registration token

        ↓

Email new link
```

The old registration URL becomes invalid.

This should require a confirmation screen because it is somewhat destructive.

---

# 13. Resend Invite

For an unlinked user:

```text
[Resend Invite]
```

I recommend **rotating** the token.

So:

```text
Old token → invalid
New token → generated
New email → sent
```

The link still has no expiration.

This avoids storing raw tokens simply so we can reproduce an old email.

---

# 14. Database schema

I would keep the entire application to essentially **one business table**.

```text
members
────────────────────────────────────

id
email                  UNIQUE NOT NULL

active                  BOOLEAN NOT NULL

discord_user_id         UNIQUE NULL
discord_username        NULL
discord_global_name     NULL

invite_token_hash       UNIQUE NOT NULL

created_at
updated_at

invite_sent_at          NULL
discord_linked_at       NULL
deactivated_at          NULL
```

Potential SQLAlchemy model:

```python
class Member(Base):
    __tablename__ = "members"

    id = mapped_column(Integer, primary_key=True)

    email = mapped_column(String, unique=True, nullable=False)

    active = mapped_column(Boolean, default=True, nullable=False)

    discord_user_id = mapped_column(String, unique=True, nullable=True)
    discord_username = mapped_column(String, nullable=True)
    discord_global_name = mapped_column(String, nullable=True)

    invite_token_hash = mapped_column(
        String,
        unique=True,
        nullable=False,
    )

    created_at = ...
    updated_at = ...
    invite_sent_at = ...
    discord_linked_at = ...
    deactivated_at = ...
```

I specifically **wouldn't add**:

```text
subscription_status
verified
in_discord
payment_status
squarespace_id
subscription_id
```

None are necessary for V1.

`active` is the authoritative entitlement.

---

# 15. Don't store whether they're Verified

This is an important architecture choice.

Don't have:

```text
member.verified = true
```

because it creates two sources of truth.

You'd eventually get:

```text
Database says verified
Discord says not verified
```

Instead:

```text
Database:
active = entitlement

Discord:
Verified role = enforcement
```

The bot periodically makes Discord agree with the database.

---

# 16. Reconciliation loop

I would add one tiny background task to the Discord bot.

Every 5 minutes:

```text
Load linked members
       │
       ▼
For each member currently in guild:
       │
       ├── active = true
       │      └── ensure Verified exists
       │
       └── active = false
              └── ensure Verified does NOT exist
```

This solves several problems almost for free:

* Bot happened to be offline when someone joined.
* Discord disconnected temporarily.
* REST API failed during deactivation.
* An employee manually removed `Verified`.
* Someone manually gave an inactive member `Verified`.
* Server restarted during an operation.

The join event gives instant behavior.

The reconciliation loop provides eventual correctness.

SQLite remains the source of truth.

---

# 17. Discord bot permissions

The bot should have only what it needs.

### Gateway intent

```text
Server Members Intent
```

No Message Content intent needed.

### Discord permission

```text
Manage Roles
```

The bot's Discord role must also be positioned **above** the `Verified` role in the server's role hierarchy; Discord only allows a bot to assign/manage roles lower than its highest role. ([Discord Support][4])

For example:

```text
Admin
SSU Membership Bot    ← bot
Verified              ← managed role
Moderator
Member
@everyone
```

The bot does **not** need:

```text
Administrator
Manage Channels
Manage Messages
Kick Members
Ban Members
```

---

# 18. Admin authentication

No user database.

`.env`:

```bash
ADMIN_PASSWORD=some-long-password
SESSION_SECRET=some-long-random-secret
```

Admin visits:

```text
/admin/login
```

Enters password.

Successful login creates a signed session cookie.

Cookie:

```text
HttpOnly
Secure
SameSite=Lax
```

The admin session could last something like 7 days.

I would also include basic CSRF protection for admin POST forms.

---

# 19. Environment configuration

Something along these lines:

```bash
APP_NAME="SSU Membership"
APP_URL="https://members.ssu-apps.link"

DATABASE_URL="sqlite:///./data/app.db"

ADMIN_PASSWORD="..."
SESSION_SECRET="..."

SMTP_HOST="mail.example.com"
SMTP_PORT="465"
SMTP_USERNAME="..."
SMTP_PASSWORD="..."
SMTP_FROM="community@example.com"
SMTP_USE_SSL="true"

DISCORD_CLIENT_ID="..."
DISCORD_CLIENT_SECRET="..."
DISCORD_BOT_TOKEN="..."

DISCORD_GUILD_ID="..."
DISCORD_VERIFIED_ROLE_ID="..."

DISCORD_INVITE_URL="https://discord.gg/..."
```

OAuth redirect URI:

```text
https://members.ssu-apps.link/auth/discord/callback
```

---

# 20. Routes

The entire application can remain very small.

### Public

```text
GET  /join/{token}
GET  /auth/discord/start
GET  /auth/discord/callback
```

### Admin authentication

```text
GET  /admin/login
POST /admin/login
POST /admin/logout
```

### Dashboard

```text
GET  /admin
```

### Member actions

```text
POST /admin/members
POST /admin/members/{id}/email
POST /admin/members/{id}/resend
POST /admin/members/{id}/deactivate
POST /admin/members/{id}/reactivate
POST /admin/members/{id}/reset-discord
```

That's essentially the entire web application.

---

# 21. Dashboard design

I'd keep it very utilitarian.

```text
┌─────────────────────────────────────────────────────────────┐
│ SSU Membership Admin                       [Log Out]        │
│                                                             │
│ [+ Add Member]                    Search [______________]   │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│ ACTIVE                                                      │
│                                                             │
│ jane@example.com                                            │
│ Discord: @janedoe                                           │
│ Linked: Sep 7, 2026                                         │
│                              [Reset Discord] [Deactivate]   │
│                                                             │
│ bob@example.com                                             │
│ Discord: Waiting for connection                             │
│ Invite sent: Sep 7, 2026                                    │
│                        [Edit Email] [Resend] [Deactivate]   │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│ INACTIVE                                                    │
│                                                             │
│ sam@example.com                                             │
│ Discord: @sammy                                             │
│ Deactivated: Sep 1, 2026                                    │
│                                         [Reactivate]        │
└─────────────────────────────────────────────────────────────┘
```

No complicated admin SPA required.

---

# 22. Project structure

I'd build it like this:

```text
ssu-membership/
│
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── models.py
│   │
│   ├── admin_auth.py
│   ├── email_service.py
│   ├── discord_oauth.py
│   ├── discord_api.py
│   │
│   ├── routes/
│   │   ├── admin.py
│   │   └── registration.py
│   │
│   ├── templates/
│   │   ├── base.html
│   │   ├── admin_login.html
│   │   ├── admin_dashboard.html
│   │   ├── join.html
│   │   └── join_complete.html
│   │
│   └── static/
│       └── app.css
│
├── bot/
│   ├── __init__.py
│   └── main.py
│
├── tests/
│
├── data/
│   └── app.db
│
├── .env
├── .env.example
├── pyproject.toml
└── README.md
```

Both processes import the same:

```text
app.models
app.database
app.config
```

---

# 23. Processes

Exactly two processes:

### Web

```text
uvicorn app.main:app
```

### Discord bot

```text
python -m bot.main
```

Both read:

```text
.env
     │
     └── DATABASE_URL
              │
              ▼
           app.db
```

SQLite is perfectly adequate for this scale. I'd enable SQLite **WAL mode** because both the web process and bot will occasionally access the database concurrently.

---

# 24. Deployment

On Ubuntu:

```text
/opt/ssu-membership/
```

Two systemd services:

```text
ssu-membership-web.service
ssu-membership-bot.service
```

Caddy:

```text
members.ssu-apps.link {
    reverse_proxy 127.0.0.1:8010
}
```

Web process:

```text
127.0.0.1:8010
```

Nothing besides Caddy needs to be publicly exposed.

---

# 25. Failure behavior

This is where the simple architecture still needs to be reliable.

### Email fails

Member remains created.

Admin sees an error:

```text
Member was created, but the email could not be sent.

[Resend Invite]
```

### Discord API fails during deactivation

Database still gets:

```text
active = false
```

Admin gets:

```text
Member deactivated.

Discord role synchronization failed and will
be retried automatically.
```

The reconciliation loop fixes it later.

### Bot offline when user joins

User doesn't immediately receive `Verified`.

When bot reconnects/reconciliation runs:

```text
active=true
discord_user_id matches
→ Verified
```

### Discord account already used

OAuth registration is rejected.

### Email already exists

Admin receives:

```text
A member with this email already exists.

[View Member]
```

### User leaves and rejoins

`on_member_join` runs again:

```text
active?
  yes → Verified
  no  → nothing
```

---

# 26. Tests I consider essential

We don't need hundreds of tests, but these workflows should be covered.

1. Admin password required.
2. Admin can create member.
3. Email addresses are normalized.
4. Duplicate email rejected.
5. Registration token resolves member.
6. Invalid token gives safe error.
7. OAuth state must match.
8. Discord account gets linked.
9. Same Discord account cannot link twice.
10. Linked active server member receives `Verified`.
11. Join event gives active member `Verified`.
12. Join event does not verify unknown member.
13. Join event does not verify inactive member.
14. Deactivation removes role.
15. Reactivation restores role if currently in server.
16. Reset removes old role and Discord association.
17. Resend invalidates old registration token.
18. Reconciliation adds missing role.
19. Reconciliation removes unauthorized role.
20. Application survives Discord API errors gracefully.

Discord calls should be mocked for automated tests.

---

# 27. Things I would explicitly leave out of V1

This is important for keeping the project from expanding.

**Do not build:**

* Squarespace API integration
* Squarespace webhooks
* Automatic cancellation processing
* Multiple admin accounts
* Password resets
* Member passwords
* Member dashboard
* Discord email verification
* Discord server auto-join
* Unique Discord invites
* Bot channel management
* Payment records
* Subscription tiers
* Redis
* Celery
* Postgres
* Docker requirement
* React frontend
* Discord OAuth token persistence
* Refresh tokens

Those can all be added later without changing the fundamental architecture.

---

# 28. Development order

I would build it in six small stages.

### Stage 1 — Foundation

```text
FastAPI
SQLite
SQLAlchemy Member model
.env configuration
admin login
admin dashboard
add/deactivate/reactivate
```

At this point we're managing fake members with no Discord.

### Stage 2 — Registration email

Add:

```text
registration token
SMTP
/join/{token}
resend
email editing
```

Now the entire SSU → subscriber email workflow works.

### Stage 3 — Discord OAuth

Add:

```text
Connect Discord
OAuth callback
/users/@me
one-to-one validation
Discord ID storage
```

Now:

```text
Squarespace email
→ employee
→ SSU app
→ subscriber email
→ Discord identity
```

works end-to-end.

### Stage 4 — Discord role management

Add shared:

```text
DiscordService
```

with roughly:

```python
get_member(discord_user_id)

add_verified_role(discord_user_id)

remove_verified_role(discord_user_id)

sync_member(member)
```

The FastAPI app uses this for OAuth/deactivate/reactivate.

### Stage 5 — Discord bot

Add:

```text
on_member_join
5-minute reconciliation
```

Now all automated Discord behavior works.

### Stage 6 — Deployment and hardening

Add:

```text
systemd
Caddy
HTTPS
SQLite WAL
logging
backups
tests
CSRF
secure cookies
```

---

# Final V1 Flow

This is the architecture I would consider the specification:

```text
SQUARESPACE
Subscriber purchases SSU membership
             │
             ▼
SSU employee receives Squarespace email
             │
             ▼
ADMIN DASHBOARD
Employee enters subscriber email
             │
             ▼
MEMBER RECORD
active = true
Discord = NULL
             │
             ▼
EMAIL
Unique permanent registration link
             │
             ▼
MEMBER CLICKS LINK
             │
             ▼
CONNECT DISCORD
OAuth scope = identify
             │
             ▼
DATABASE
email@example.com
      ↕
Discord User ID 123456789
             │
             ├──────── Member already in server
             │                │
             │                ▼
             │           Add Verified
             │
             ▼
SHOW PERMANENT DISCORD INVITE
             │
             ▼
MEMBER JOINS
             │
             ▼
BOT on_member_join
             │
             ▼
Lookup Discord User ID
             │
             ▼
active == true?
             │
             ▼
ADD VERIFIED
             │
             ▼
Subscriber channels available
```

Cancellation:

```text
Squarespace cancellation email
             │
             ▼
Employee
             │
             ▼
[Deactivate]
             │
        ┌────┴─────┐
        ▼          ▼
active=false    Discord REST
                remove Verified
```

That gives you a **very small Patreon-like identity bridge without trying to rebuild Patreon**: SSU manually controls entitlement, the registration URL establishes `subscription email ↔ Discord user ID`, and everything after that is automatic.

[1]: https://docs.discord.com/developers/platform/oauth2-and-permissions?utm_source=chatgpt.com "OAuth2 and Permissions - Documentation - Discord"
[2]: https://docs.discord.com/developers/events/gateway-events?utm_source=chatgpt.com "Gateway Events - Documentation - Discord"
[3]: https://docs.discord.com/developers/resources/guild?utm_source=chatgpt.com "Guild Resource - Documentation - Discord"
[4]: https://support.discord.com/hc/en-us/articles/214836687-Discord-Roles-and-Permissions?utm_source=chatgpt.com "Discord Roles and Permissions – Discord"
