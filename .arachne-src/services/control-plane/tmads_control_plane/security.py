from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import phonenumbers
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

log = logging.getLogger("tmads.security")


def normalize_phone(raw: str, *, default_region: str = "US") -> str:
    candidate = raw.strip()
    if not candidate:
        raise ValueError("Enter a valid North American phone number.")
    try:
        if candidate.startswith("+"):
            parsed = phonenumbers.parse(candidate, None)
        else:
            parsed = phonenumbers.parse(candidate, default_region)
    except phonenumbers.NumberParseException as exc:
        raise ValueError("Enter a valid North American phone number.") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("Enter a valid North American phone number.")
    if parsed.country_code != 1:
        raise ValueError("Enter a valid North American phone number.")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_supply_admin_phones(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    phones: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            phones.append(normalize_phone(part))
    return ",".join(phones) if phones else None


def is_supply_admin_phone(phone_e164: str, configured: str | None) -> bool:
    if not configured:
        return False
    for admin_phone in configured.split(","):
        candidate = admin_phone.strip()
        if candidate and secrets.compare_digest(phone_e164, candidate):
            return True
    return False


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_otp(challenge_id: str, code: str) -> str:
    return digest(f"{challenge_id}:{code}")


def verify_otp(challenge_id: str, code: str, expected: str) -> bool:
    return hmac.compare_digest(hash_otp(challenge_id, code), expected)


class SmsProvider(Protocol):
    def start(self, phone_e164: str, code: str) -> None: ...

    def verify(self, phone_e164: str, code: str, expected_hash: str, challenge_id: str) -> bool: ...

    def send_invitation(self, phone_e164: str, invitee_name: str, url: str) -> str: ...

    def send_message(self, phone_e164: str, body: str) -> str: ...


@dataclass
class DemoSmsProvider:
    expose_code: bool

    def start(self, phone_e164: str, code: str) -> None:
        if not self.expose_code:
            log.info("demo_sms_suppressed", extra={"phone_suffix": phone_e164[-4:]})
            return
        log.warning(
            "demo_otp_code",
            extra={"phone_suffix": phone_e164[-4:], "demo_otp_code": code},
        )

    def verify(self, phone_e164: str, code: str, expected_hash: str, challenge_id: str) -> bool:
        return verify_otp(challenge_id, code, expected_hash)

    def send_invitation(self, phone_e164: str, invitee_name: str, url: str) -> str:
        if not self.expose_code:
            raise RuntimeError("Demo invitation delivery is disabled")
        log.warning(
            "demo_invitation",
            extra={"phone_suffix": phone_e164[-4:], "invitee_name": invitee_name, "url": url},
        )
        return "demo"

    def send_message(self, phone_e164: str, body: str) -> str:
        if not self.expose_code:
            raise RuntimeError("Demo SMS delivery is disabled")
        log.warning(
            "demo_sms_message",
            extra={"phone_suffix": phone_e164[-4:], "body": body},
        )
        return "demo"


class DisabledSmsProvider:
    def start(self, phone_e164: str, code: str) -> None:
        raise RuntimeError("No SMS provider configured")

    def verify(self, phone_e164: str, code: str, expected_hash: str, challenge_id: str) -> bool:
        raise RuntimeError("No SMS provider configured")

    def send_invitation(self, phone_e164: str, invitee_name: str, url: str) -> str:
        raise RuntimeError("Invitation SMS delivery is not configured")

    def send_message(self, phone_e164: str, body: str) -> str:
        raise RuntimeError("SMS notification delivery is not configured")


@dataclass
class TwilioVerifySmsProvider:
    account_sid: str
    auth_token: str
    service_sid: str

    def __post_init__(self) -> None:
        self._client = Client(self.account_sid, self.auth_token)

    def start(self, phone_e164: str, code: str) -> None:
        del code
        try:
            self._client.verify.v2.services(self.service_sid).verifications.create(
                to=phone_e164,
                channel="sms",
            )
        except TwilioRestException as exc:
            log.warning(
                "twilio_verify_start_failed",
                extra={"phone_suffix": phone_e164[-4:], "status": exc.status},
            )
            raise RuntimeError("Unable to send verification code") from exc

    def verify(self, phone_e164: str, code: str, expected_hash: str, challenge_id: str) -> bool:
        del expected_hash, challenge_id
        try:
            check = self._client.verify.v2.services(
                self.service_sid
            ).verification_checks.create(to=phone_e164, code=code)
        except TwilioRestException as exc:
            log.warning(
                "twilio_verify_check_failed",
                extra={"phone_suffix": phone_e164[-4:], "status": exc.status},
            )
            return False
        return check.status == "approved"

    def send_invitation(self, phone_e164: str, invitee_name: str, url: str) -> str:
        del phone_e164, invitee_name, url
        raise RuntimeError(
            "Twilio Verify cannot send invitations; configure TMADS_TWILIO_PHONE_NUMBER"
        )

    def send_message(self, phone_e164: str, body: str) -> str:
        del phone_e164, body
        raise RuntimeError(
            "Twilio Verify cannot send notifications; configure TMADS_TWILIO_PHONE_NUMBER"
        )


@dataclass
class TwilioMessagingSmsProvider:
    account_sid: str
    auth_token: str
    from_number: str

    def __post_init__(self) -> None:
        self._client = Client(self.account_sid, self.auth_token)

    def start(self, phone_e164: str, code: str) -> None:
        try:
            self._client.messages.create(
                body=f"Your TMAds verification code is {code}. It expires in 5 minutes.",
                from_=self.from_number,
                to=phone_e164,
            )
        except TwilioRestException as exc:
            log.warning(
                "twilio_message_start_failed",
                extra={"phone_suffix": phone_e164[-4:], "status": exc.status},
            )
            raise RuntimeError("Unable to send verification code") from exc

    def verify(self, phone_e164: str, code: str, expected_hash: str, challenge_id: str) -> bool:
        del phone_e164
        return verify_otp(challenge_id, code, expected_hash)

    def send_invitation(self, phone_e164: str, invitee_name: str, url: str) -> str:
        return self.send_message(
            phone_e164,
            f"{invitee_name}, you were invited to TMAds. Sign in and accept: {url}",
        )

    def send_message(self, phone_e164: str, body: str) -> str:
        try:
            message = self._client.messages.create(
                body=body,
                from_=self.from_number,
                to=phone_e164,
            )
        except TwilioRestException as exc:
            log.warning("twilio_sms_failed", extra={"phone_suffix": phone_e164[-4:], "status": exc.status})
            raise RuntimeError("Unable to deliver SMS") from exc
        return str(message.sid)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def load_or_create_signing_key(secrets_dir: Path) -> tuple[Ed25519PrivateKey, str]:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    key_path = secrets_dir / "contract-signing-ed25519.pem"
    if key_path.exists():
        private = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        if not isinstance(private, Ed25519PrivateKey):
            raise RuntimeError("Configured contract signing key is not Ed25519")
    else:
        private = Ed25519PrivateKey.generate()
        raw = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private, f"ed25519:{hashlib.sha256(public).hexdigest()[:24]}"


def rotate_signing_key(
    secrets_dir: Path,
) -> tuple[str, str, Ed25519PrivateKey, str, str]:
    """Rotate the software-held Ed25519 key and retain the predecessor for audit."""
    current, old_key_id = load_or_create_signing_key(secrets_dir)
    old_public = current.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    old_public_b64 = "ed25519:" + base64.urlsafe_b64encode(old_public).rstrip(b"=").decode()
    key_path = secrets_dir / "contract-signing-ed25519.pem"
    archive = secrets_dir / f"contract-signing-{old_key_id.removeprefix('ed25519:')}.pem"
    if not archive.exists():
        os.replace(key_path, archive)
    else:
        key_path.unlink()
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(pem)
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    new_key_id = f"ed25519:{hashlib.sha256(public).hexdigest()[:24]}"
    public_b64 = base64.urlsafe_b64encode(public).rstrip(b"=").decode()
    return old_key_id, old_public_b64, private, new_key_id, f"ed25519:{public_b64}"
