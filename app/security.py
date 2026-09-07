import hashlib
import secrets


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_invite() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)
