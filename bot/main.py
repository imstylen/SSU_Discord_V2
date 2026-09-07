import asyncio
import logging

import discord
from discord.ext import tasks

from app.config import Settings
from app.database import Database
from app.discord_api import DiscordService
from app.email_service import EmailService
from app.membership import MembershipService

logger = logging.getLogger(__name__)


class MembershipBot(discord.Client):
    def __init__(self, settings, membership, **kwargs):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        super().__init__(intents=intents, **kwargs)
        self.settings = settings
        self.membership = membership
        self.reconcile_members.change_interval(seconds=settings.reconcile_interval_seconds)

    async def setup_hook(self):
        self.reconcile_members.start()

    async def on_ready(self):
        guild = self.get_guild(int(self.settings.discord_guild_id))
        if guild is None:
            logger.error("Bot is not in the configured guild")
            return
        role = guild.get_role(int(self.settings.discord_verified_role_id))
        if guild.me and not guild.me.guild_permissions.create_instant_invite:
            logger.error("Bot needs Create Invite permission for automatic OAuth server joining")
        if (
            role is None
            or role.is_default()
            or role.managed
            or guild.me is None
            or not guild.me.guild_permissions.manage_roles
            or role >= guild.me.top_role
        ):
            logger.error(
                "Verified role is not manageable; check role ID, Manage Roles and hierarchy"
            )
        logger.info("Membership bot connected")

    async def on_member_join(self, member):
        if str(member.guild.id) != self.settings.discord_guild_id:
            return
        try:
            await asyncio.to_thread(self.membership.sync_discord_user, str(member.id))
        except Exception as exc:
            logger.warning(
                "Join synchronization failed (%s); reconciliation will retry", type(exc).__name__
            )

    @tasks.loop(seconds=300)
    async def reconcile_members(self):
        try:
            await asyncio.to_thread(self.membership.reconcile)
        except Exception as exc:
            # Keep the loop alive through database/transport outages.
            logger.error("Reconciliation failed (%s); will retry next cycle", type(exc).__name__)

    @reconcile_members.before_loop
    async def before_reconcile(self):
        await self.wait_until_ready()

    async def close(self):
        self.reconcile_members.cancel()
        await super().close()


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    if not settings.discord_bot_token.get_secret_value() or not all(
        value.isdigit() for value in (settings.discord_guild_id, settings.discord_verified_role_id)
    ):
        raise SystemExit(
            "Set DISCORD_BOT_TOKEN, DISCORD_GUILD_ID and DISCORD_VERIFIED_ROLE_ID in .env"
        )
    db = Database(settings.database_url)
    db.initialize()
    service = DiscordService(settings)
    membership = MembershipService(db, service, EmailService(settings))
    bot = MembershipBot(settings, membership)
    try:
        bot.run(settings.discord_bot_token.get_secret_value(), log_handler=None)
    finally:
        service.close()
        db.engine.dispose()


if __name__ == "__main__":
    main()
