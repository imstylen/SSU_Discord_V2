import logging

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.discord_api import DiscordError
from app.email_service import EmailError
from app.models import Member, utcnow
from app.security import new_invite

logger = logging.getLogger(__name__)


class MembershipError(Exception):
    def __init__(self, message, member_id=None):
        super().__init__(message)
        self.member_id = member_id


def normalized_email(value):
    try:
        return validate_email(value.strip().lower(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise MembershipError("Enter a valid email address.") from exc


class MembershipService:
    def __init__(self, database, discord, email):
        self.db = database
        self.discord = discord
        self.email = email

    def get(self, member_id):
        with self.db.session() as session:
            member = session.get(Member, member_id)
            if member is None:
                raise MembershipError("Member not found.")
            return member

    def by_hash(self, invite_hash):
        with self.db.session() as session:
            return session.scalar(select(Member).where(Member.invite_token_hash == invite_hash))

    def deliver(self, member_id, email, token, invite_hash):
        try:
            self.email.send_invite(email, token)
        except EmailError:
            logger.warning("Invite delivery failed for member %s", member_id)
            return False
        with self.db.write() as session:
            member = session.get(Member, member_id)
            if member.invite_token_hash == invite_hash and member.email == email:
                member.invite_sent_at = utcnow()
        return True

    def create(self, email):
        email = normalized_email(email)
        token, invite_hash = new_invite()
        with self.db.write() as session:
            existing = session.scalar(select(Member).where(Member.email == email))
            if existing:
                raise MembershipError("A member with this email already exists.", existing.id)
            member = Member(email=email, invite_token_hash=invite_hash)
            session.add(member)
            session.flush()
        sent = self.deliver(member.id, member.email, token, invite_hash)
        return member.id, sent

    def edit_email(self, member_id, email):
        email = normalized_email(email)
        with self.db.write() as session:
            member = session.get(Member, member_id)
            if member is None:
                raise MembershipError("Member not found.")
            existing = session.scalar(
                select(Member).where(Member.email == email, Member.id != member_id)
            )
            if existing:
                raise MembershipError("A member with this email already exists.", existing.id)
            changed = member.email != email
            member.email = email
            # An old address must not retain an unclaimed membership credential.
            if changed and not member.discord_user_id:
                token, member.invite_token_hash = new_invite()
                member.invite_sent_at = None
        if changed and not member.discord_user_id:
            return self.deliver(member.id, email, token, member.invite_token_hash)
        return None

    def resend(self, member_id):
        with self.db.write() as session:
            member = session.get(Member, member_id)
            if member is None:
                raise MembershipError("Member not found.")
            if member.discord_user_id or not member.active:
                raise MembershipError("Invites can only be resent to active, unlinked members.")
            token, member.invite_token_hash = new_invite()
            member.invite_sent_at = None
        return self.deliver(member.id, member.email, token, member.invite_token_hash)

    def sync(self, member_id):
        with self.db.write() as session:
            member = session.get(Member, member_id)
            return self.discord.sync_member(member) if member else "unknown"

    def sync_discord_user(self, discord_user_id):
        with self.db.write() as session:
            member = session.scalar(select(Member).where(Member.discord_user_id == discord_user_id))
            return self.discord.sync_member(member) if member else "unknown"

    def safe_sync(self, member_id):
        try:
            return self.sync(member_id)
        except DiscordError:
            logger.warning(
                "Discord synchronization failed for member %s; reconciliation will retry", member_id
            )
            return "failed"

    def set_active(self, member_id, active):
        with self.db.write() as session:
            member = session.get(Member, member_id)
            if member is None:
                raise MembershipError("Member not found.")
            member.active = active
            member.deactivated_at = None if active else utcnow()
        # Commit entitlement even when Discord is unavailable.
        return self.safe_sync(member_id)

    def reset(self, member_id, expected_hash):
        with self.db.write() as session:
            member = session.get(Member, member_id)
            if member is None:
                raise MembershipError("Member not found.")
            if member.invite_token_hash != expected_hash or not member.discord_user_id:
                raise MembershipError(
                    "This member changed. Review the member before resetting again."
                )
            try:
                self.discord.remove_verified_role(member.discord_user_id)
            except DiscordError as exc:
                raise MembershipError(
                    "Discord role removal failed. The link was kept so access remains tracked. "
                    "Please retry the reset when Discord is available."
                ) from exc
            member.discord_user_id = None
            member.discord_username = None
            member.discord_global_name = None
            member.discord_linked_at = None
            token, member.invite_token_hash = new_invite()
            member.invite_sent_at = None
        return self.deliver(member.id, member.email, token, member.invite_token_hash)

    def link(self, invite_hash, user, access_token=None):
        try:
            with self.db.write() as session:
                member = session.scalar(
                    select(Member).where(Member.invite_token_hash == invite_hash)
                )
                if member is None or not member.active:
                    raise MembershipError(
                        "This registration link is unavailable. Please contact SSU."
                    )
                if member.discord_user_id:
                    raise MembershipError(
                        "This membership is already connected. Contact SSU to change accounts."
                    )
                member.discord_user_id = user["id"]
                member.discord_username = user["username"]
                member.discord_global_name = user.get("global_name")
                member.discord_linked_at = utcnow()
                session.flush()
        except IntegrityError as exc:
            raise MembershipError(
                "This Discord account is already connected to another SSU membership. "
                "Please contact SSU for assistance."
            ) from exc
        if access_token:
            # Commit identity first so the gateway join handler can find it.
            # Recheck under the shared lock in case an admin reset/deactivated it.
            try:
                with self.db.write() as session:
                    current = session.get(Member, member.id)
                    if (
                        not current.active
                        or current.discord_user_id != user["id"]
                        or current.invite_token_hash != invite_hash
                    ):
                        return member.id, "changed"
                    self.discord.join_guild(current.discord_user_id, access_token)
            except DiscordError:
                logger.warning("Automatic Discord join failed for member %s", member.id)
                # No token is persisted for retry. The completion page offers an invite.
                self.safe_sync(member.id)
                return member.id, "join_failed"
        return member.id, self.safe_sync(member.id)

    def reconcile(self):
        with self.db.session() as session:
            ids = list(
                session.scalars(select(Member.id).where(Member.discord_user_id.is_not(None)))
            )
        failures = 0
        for member_id in ids:
            failures += self.safe_sync(member_id) == "failed"
        logger.info("Reconciled %s linked memberships; %s failures", len(ids), failures)
        return len(ids), failures
