"""Shared pseudonymous sender/recipient identity for dispatch and authenticated statuses."""

from dataclasses import dataclass, field
import hashlib
import json

from .whatsapp_models import numeric_id


def _digest(items: list[str]) -> str:
    return hashlib.sha256(json.dumps(items, separators=(',', ':')).encode('ascii')).hexdigest()


@dataclass(frozen=True)
class WhatsAppRouting:
    waba_id: str = field(repr=False)
    phone_number_id: str = field(repr=False)

    def __post_init__(self) -> None:
        numeric_id(self.waba_id)
        numeric_id(self.phone_number_id)

    @property
    def scope(self) -> str:
        return _digest(['gunb-whatsapp-route-v1', self.waba_id, self.phone_number_id])

    def recipient_digest(self, recipient_id: str) -> str:
        return _digest(['gunb-whatsapp-recipient-v1', self.scope,
                        numeric_id(recipient_id, minimum=8, maximum=15)])
