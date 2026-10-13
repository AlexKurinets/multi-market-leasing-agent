import itertools
from dataclasses import dataclass, field
from typing import Protocol


class ProviderRejected(Exception):
    """The provider answered and did not accept the message, so it was definitely not sent."""


@dataclass(frozen=True)
class SendReceipt:
    provider_id: str


class Sender(Protocol):
    channel: str

    def send(self, to: str, body: str) -> SendReceipt:
        """Send or raise. ``ProviderRejected`` means not sent; any other exception means the
        outcome is unknown (for example a timeout after the provider accepted the request)."""
        ...


@dataclass
class ConsoleSender:
    """Prints instead of sending. Used for local runs, the demo chat and tests."""

    channel: str
    sent: list[tuple[str, str]] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def send(self, to: str, body: str) -> SendReceipt:
        self.sent.append((to, body))
        print(f"[{self.channel} -> {to}] {body}")
        return SendReceipt(f"console-{self.channel}-{next(self._ids)}")
