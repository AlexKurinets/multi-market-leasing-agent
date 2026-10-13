import smtplib
from email.message import EmailMessage

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

from leasing.channels.base import ProviderRejected, SendReceipt


class TwilioSmsSender:
    channel = "sms"

    def __init__(self, account_sid: str, auth_token: str, messaging_service_sid: str) -> None:
        self._client = TwilioClient(account_sid, auth_token)
        self._service = messaging_service_sid

    def send(self, to: str, body: str) -> SendReceipt:
        try:
            message = self._client.messages.create(
                to=to, messaging_service_sid=self._service, body=body
            )
        except TwilioRestException as exc:  # Twilio responded with an error: not sent
            raise ProviderRejected(f"Twilio {exc.status} {exc.code}: {exc.msg}") from exc
        return SendReceipt(message.sid)


class SmtpEmailSender:
    channel = "email"

    def __init__(
        self, host: str, port: int, username: str, password: str, sender: str, subject: str
    ) -> None:
        self._host, self._port = host, port
        self._username, self._password = username, password
        self._sender, self._subject = sender, subject

    def send(self, to: str, body: str) -> SendReceipt:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self._sender, to, self._subject
        message.set_content(body)
        try:
            with smtplib.SMTP(self._host, self._port, timeout=20) as smtp:
                smtp.starttls()
                if self._username:
                    smtp.login(self._username, self._password)
                refused = smtp.send_message(message)
        except smtplib.SMTPRecipientsRefused as exc:
            raise ProviderRejected(str(exc)) from exc
        if refused:
            raise ProviderRejected(f"Recipients refused: {refused}")
        return SendReceipt(message["Message-ID"] or f"smtp-{to}")
