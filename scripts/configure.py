"""Create .env once with fresh secrets. Never overwrite an existing configuration."""

import argparse
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", help="Use localhost HTTP for development")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    template = (root / ".env.example").read_text(encoding="utf-8")
    template = template.replace("replace-with-a-long-unique-password", secrets.token_urlsafe(32))
    template = template.replace(
        "replace-with-a-random-secret-at-least-32-characters", secrets.token_urlsafe(48)
    )
    if args.local:
        template = template.replace("https://members.ssu-apps.link", "http://localhost:8010")
        template = template.replace("SESSION_COOKIE_SECURE=true", "SESSION_COOKIE_SECURE=false")
    try:
        with (root / ".env").open("x", encoding="utf-8") as output:
            output.write(template)
        (root / ".env").chmod(0o600)
    except FileExistsError:
        raise SystemExit(".env already exists; left unchanged. Edit it directly.") from None
    print("Created .env with fresh secrets. Open it for your admin password and set SMTP/Discord.")


if __name__ == "__main__":
    main()
