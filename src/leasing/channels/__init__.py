from leasing.channels.base import ConsoleSender, ProviderRejected, Sender, SendReceipt
from leasing.config import Settings


def build_senders(settings: Settings) -> dict[str, Sender]:
    senders: dict[str, Sender] = {"sms": ConsoleSender("sms"), "email": ConsoleSender("email")}
    if settings.sms_backend == "twilio":
        from leasing.channels.providers import TwilioSmsSender

        senders["sms"] = TwilioSmsSender(
            settings.twilio_account_sid,
            settings.twilio_auth_token,
            settings.twilio_messaging_service_sid,
        )
    if settings.email_backend == "smtp":
        from leasing.channels.providers import SmtpEmailSender

        senders["email"] = SmtpEmailSender(
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_username,
            settings.smtp_password,
            settings.email_from,
            subject="Your apartment inquiry",
        )
    return senders


__all__ = ["ConsoleSender", "ProviderRejected", "SendReceipt", "Sender", "build_senders"]
