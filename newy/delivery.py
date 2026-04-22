from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Mapping

from .config import AppConfig
from .models import Digest


@dataclass(slots=True)
class DeliveryResult:
    ok: bool
    status: str
    external_id: str = ""
    error: str = ""
    chunks_sent: int = 0


class WhatsAppDelivery:
    def __init__(self, config: AppConfig):
        self.config = config

    def render_message(self, digest: Digest, language: str) -> str:
        mode = language.lower()
        sections: list[str] = []
        if mode in {"english", "en"}:
            sections.append(self._render_section(digest.sections["en"], "Sources"))
        elif mode in {"arabic", "ar"}:
            sections.append(self._render_section(digest.sections["ar"], "المصادر"))
        else:
            sections.append(self._render_section(digest.sections["en"], "Sources"))
            sections.append(self._render_section(digest.sections["ar"], "المصادر"))
        return "\n\n".join(section for section in sections if section).strip()

    def _render_section(self, section: dict, citation_label: str) -> str:
        bullet_lines = []
        for item in section.get("bullets", []):
            if isinstance(item, dict):
                bullet_lines.append(str(item.get("text", "")).strip())
            else:
                bullet_lines.append(str(item).strip())
        bullets = "\n".join(f"- {line}" for line in bullet_lines if line)
        citations = "\n".join(str(item) for item in section.get("citations", [])[:6])
        parts = [section.get("title", "Digest"), bullets, section.get("why", "")]
        if citations:
            parts.append(f"{citation_label}:\n{citations}")
        return "\n\n".join(part for part in parts if part)

    def send(self, to_number: str, digest: Digest, language: str) -> DeliveryResult:
        body = self.render_message(digest, language)
        chunks = [body[i : i + 1500] for i in range(0, len(body), 1500)] or [body]
        if self.config.twilio.dry_run or not (
            self.config.twilio.account_sid
            and self.config.twilio.auth_token
            and self.config.twilio.from_number
        ):
            return DeliveryResult(ok=True, status="dry_run", chunks_sent=len(chunks))

        external_ids: list[str] = []
        sent = 0
        for chunk in chunks:
            data = urllib.parse.urlencode(
                {
                    "From": self.config.twilio.from_number,
                    "To": to_number,
                    "Body": chunk,
                }
            ).encode()
            request = urllib.request.Request(
                f"https://api.twilio.com/2010-04-01/Accounts/{self.config.twilio.account_sid}/Messages.json",
                data=data,
                headers={
                    "Authorization": "Basic "
                    + base64.b64encode(
                        f"{self.config.twilio.account_sid}:{self.config.twilio.auth_token}".encode()
                    ).decode(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    payload = json.loads(response.read().decode())
            except Exception as exc:
                return DeliveryResult(ok=False, status="failed", error=str(exc), chunks_sent=sent)
            sent += 1
            external_ids.append(payload.get("sid", ""))
        return DeliveryResult(ok=True, status="sent", external_id=",".join(filter(None, external_ids)), chunks_sent=sent)


def validate_twilio_signature(url: str, params: Mapping[str, str], provided_signature: str, auth_token: str) -> bool:
    if not provided_signature or not auth_token:
        return False
    message = url + "".join(f"{key}{value}" for key, value in sorted(params.items()))
    expected = base64.b64encode(hmac.new(auth_token.encode(), message.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected, provided_signature)
