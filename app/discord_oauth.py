from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.discord_api import DiscordError


class DiscordOAuth:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=10.0)

    def close(self):
        self.client.close()

    def authorization_url(self, state: str):
        s = self.settings
        if not s.discord_client_id or not s.discord_client_secret.get_secret_value():
            raise DiscordError("Discord OAuth is not configured")
        return "https://discord.com/oauth2/authorize?" + urlencode(
            {
                "client_id": s.discord_client_id,
                "redirect_uri": s.redirect_uri,
                "response_type": "code",
                "scope": "identify guilds.join",
                "state": state,
                "prompt": "consent",
            }
        )

    def identify(self, code: str):
        s = self.settings
        try:
            response = self.client.post(
                "https://discord.com/api/oauth2/token",
                data={
                    "client_id": s.discord_client_id,
                    "client_secret": s.discord_client_secret.get_secret_value(),
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": s.redirect_uri,
                },
            )
            response.raise_for_status()
            tokens = response.json()
            access_token = tokens["access_token"]
            if not {"identify", "guilds.join"}.issubset(tokens.get("scope", "").split()):
                raise ValueError("Discord did not grant the required scopes")
            if not isinstance(access_token, str) or not access_token:
                raise ValueError("Invalid access token")
            response = self.client.get(
                "https://discord.com/api/v10/users/@me",
                headers={
                    "Authorization": f"Bearer {access_token}",
                },
            )
            response.raise_for_status()
            user = response.json()
            if not isinstance(user["id"], str) or not user["id"].isdigit():
                raise ValueError("Invalid user ID")
            if not isinstance(user["username"], str) or not user["username"]:
                raise ValueError("Invalid username")
            if user.get("global_name") is not None and not isinstance(user["global_name"], str):
                raise ValueError("Invalid display name")
            return {
                "id": user["id"],
                "username": user["username"],
                "global_name": user.get("global_name"),
            }, access_token
        except (httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError) as exc:
            raise DiscordError("Discord connection failed. Please try connecting again.") from exc
