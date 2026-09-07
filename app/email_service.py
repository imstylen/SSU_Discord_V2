import smtplib
import ssl
from email.message import EmailMessage

from app.config import Settings


class EmailError(Exception):
    pass


class EmailService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send_invite(self, email: str, token: str):
        s = self.settings
        if not s.smtp_host:
            raise EmailError("SMTP is not configured")
        message = EmailMessage()
        message["Subject"] = "Your Sunny Side Up Discord membership"
        message["From"] = s.smtp_from
        message["To"] = email
        message.set_content(
            "Welcome to Sunny Side Up!\n\nYour SSU membership is ready. "
            "Connect your Discord account using your personal registration link:\n\n"
            f"{s.app_url}/join/{token}\n\n"
            "This link does not expire. Keep it private: it connects a Discord account "
            "to your membership. A replacement link invalidates this one.\n\n"
            "Connecting Discord will also add you to the SSU server automatically.\n"
            "Need help? Contact SSU.\n"
        )
        try:
            factory = smtplib.SMTP_SSL if s.smtp_use_ssl else smtplib.SMTP
            kwargs = {"timeout": 15}
            if s.smtp_use_ssl:
                kwargs["context"] = ssl.create_default_context()
            with factory(s.smtp_host, s.smtp_port, **kwargs) as smtp:
                if s.smtp_use_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if s.smtp_username:
                    smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            raise EmailError("Registration email could not be sent") from exc
