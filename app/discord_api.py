import time

import httpx

from app.config import Settings


class DiscordError(Exception):
    pass


class DiscordService:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=5.0)

    def close(self):
        self.client.close()

    def request(self, method: str, path: str, *, missing_ok=False, json=None):
        s = self.settings
        if not all(
            (s.discord_bot_token.get_secret_value(), s.discord_guild_id, s.discord_verified_role_id)
        ):
            raise DiscordError("Discord bot is not configured")
        try:
            for attempt in range(2):
                response = self.client.request(
                    method,
                    f"https://discord.com/api/v10{path}",
                    headers={"Authorization": f"Bot {s.discord_bot_token.get_secret_value()}"},
                    json=json,
                )
                if response.status_code == 429 and attempt == 0:
                    delay = float(response.json().get("retry_after", 1))
                    if 0 <= delay <= 2:
                        time.sleep(delay)
                        continue
                if response.status_code == 404 and missing_ok:
                    # Unknown Member is safe; Unknown Guild/Role is a configuration failure.
                    if response.json().get("code") == 10007:
                        return None
                response.raise_for_status()
                return response
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise DiscordError(
                "Discord request failed; check connectivity and bot permissions"
            ) from exc

    def get_member(self, discord_user_id: str):
        response = self.request(
            "GET",
            f"/guilds/{self.settings.discord_guild_id}/members/{discord_user_id}",
            missing_ok=True,
        )
        if response is None:
            return None
        try:
            member = response.json()
            if not isinstance(member.get("roles"), list):
                raise ValueError("Missing roles")
            return member
        except (ValueError, AttributeError) as exc:
            raise DiscordError("Invalid Discord member response") from exc

    def role_path(self, discord_user_id: str):
        return (
            f"/guilds/{self.settings.discord_guild_id}/members/{discord_user_id}"
            f"/roles/{self.settings.discord_verified_role_id}"
        )

    def join_guild(self, discord_user_id: str, access_token: str):
        # Bot auth must be from the same application that issued the user token.
        # Discord returns 201 (joined) or 204 (already joined).
        self.request(
            "PUT",
            f"/guilds/{self.settings.discord_guild_id}/members/{discord_user_id}",
            json={"access_token": access_token},
        )

    def add_verified_role(self, discord_user_id: str):
        self.request("PUT", self.role_path(discord_user_id), missing_ok=True)

    def remove_verified_role(self, discord_user_id: str):
        self.request("DELETE", self.role_path(discord_user_id), missing_ok=True)

    def sync_member(self, member):
        if not member.discord_user_id:
            return "unlinked"
        guild_member = self.get_member(member.discord_user_id)
        if guild_member is None:
            return "absent"
        has_role = self.settings.discord_verified_role_id in guild_member["roles"]
        if member.active and not has_role:
            self.add_verified_role(member.discord_user_id)
        elif not member.active and has_role:
            self.remove_verified_role(member.discord_user_id)
        return "active" if member.active else "inactive"
