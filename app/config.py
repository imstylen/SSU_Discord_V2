from urllib.parse import urlparse

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    app_name: str = "SSU Membership"
    app_url: str = "https://members.ssu-apps.link"
    database_url: str = "sqlite:///./data/app.db"
    admin_password: SecretStr
    session_secret: SecretStr
    session_cookie_secure: bool = True
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = "community@example.com"
    smtp_use_ssl: bool = True
    smtp_use_starttls: bool = False
    discord_client_id: str = ""
    discord_client_secret: SecretStr = SecretStr("")
    discord_bot_token: SecretStr = SecretStr("")
    discord_guild_id: str = ""
    discord_verified_role_id: str = ""
    discord_invite_url: str = "https://discord.gg/replace-me"
    reconcile_interval_seconds: int = 300

    @model_validator(mode="after")
    def validate_settings(self):
        for name, minimum in (("admin_password", 7), ("session_secret", 32)):
            value = getattr(self, name).get_secret_value()
            if len(value) < minimum or value.startswith("replace-"):
                raise ValueError(
                    f"{name.upper()} must be a unique secret of at least {minimum} chars"
                )
        parsed = urlparse(self.app_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("APP_URL must be an absolute HTTP(S) URL")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("APP_URL must contain only the origin")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("APP_URL must use HTTPS outside local development")
        # if not self.session_cookie_secure and parsed.hostname not in {
        #     "localhost",
        #     "127.0.0.1",
        #     "::1",
        # }:
        #     raise ValueError("Insecure session cookies are allowed only on localhost")
        invite = urlparse(self.discord_invite_url)
        if invite.scheme != "https" or invite.hostname not in {"discord.gg", "discord.com"}:
            raise ValueError("DISCORD_INVITE_URL must be an HTTPS Discord invite")
        if self.smtp_use_ssl and self.smtp_use_starttls:
            raise ValueError("Choose SMTP SSL or STARTTLS, not both")
        if self.reconcile_interval_seconds < 10:
            raise ValueError("RECONCILE_INTERVAL_SECONDS must be at least 10")
        self.app_url = self.app_url.rstrip("/")
        return self

    @property
    def redirect_uri(self):
        return f"{self.app_url}/auth/discord/callback"
