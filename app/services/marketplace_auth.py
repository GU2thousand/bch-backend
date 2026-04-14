import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request
from jose import JWTError, jwt

from app.config import settings


_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_SCrypt_PREFIX = b"scrypt"
_SCrypt_VERSION = 0
_DEFAULT_LOG_N = 15
_DEFAULT_R = 8
_DEFAULT_P = 1
_TOKEN_TTL_DAYS = 7


def normalize_email(email: str) -> str:
    return email.strip().lower()


def generate_prefixed_id(prefix: str | None = None) -> str:
    body = "01" + "".join(secrets.choice(_CROCKFORD_ALPHABET) for _ in range(24))
    if prefix:
        return f"{prefix}_{body}"
    return body


def hash_marketplace_password(
    password: str,
    *,
    log_n: int = _DEFAULT_LOG_N,
    r: int = _DEFAULT_R,
    p: int = _DEFAULT_P,
) -> str:
    salt = secrets.token_bytes(32)

    header = bytearray()
    header.extend(_SCrypt_PREFIX)
    header.append(_SCrypt_VERSION)
    header.append(log_n)
    header.extend(r.to_bytes(4, "big"))
    header.extend(p.to_bytes(4, "big"))
    header.extend(salt)

    checksum = hashlib.sha256(bytes(header)).digest()[:16]
    payload = bytes(header) + checksum
    derived_key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=1 << log_n,
        r=r,
        p=p,
        dklen=64,
        maxmem=128 * 1024 * 1024,
    )
    mac = hmac.new(derived_key, payload, hashlib.sha256).digest()
    return base64.b64encode(payload + mac).decode("ascii")


def verify_marketplace_password(password: str, encoded_hash: str | None) -> bool:
    if not encoded_hash:
        return False

    try:
        raw = base64.b64decode(encoded_hash)
    except Exception:
        return False

    if len(raw) != 96 or raw[:6] != _SCrypt_PREFIX or raw[6] != _SCrypt_VERSION:
        return False

    header = raw[:48]
    checksum = raw[48:64]
    mac = raw[64:96]

    expected_checksum = hashlib.sha256(header).digest()[:16]
    if not hmac.compare_digest(checksum, expected_checksum):
        return False

    log_n = raw[7]
    r = int.from_bytes(raw[8:12], "big")
    p = int.from_bytes(raw[12:16], "big")
    salt = raw[16:48]

    try:
        derived_key = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=1 << log_n,
            r=r,
            p=p,
            dklen=64,
            maxmem=128 * 1024 * 1024,
        )
    except ValueError:
        return False

    expected_mac = hmac.new(derived_key, raw[:64], hashlib.sha256).digest()
    return hmac.compare_digest(mac, expected_mac)


def create_marketplace_token(
    *,
    auth_identity_id: str,
    email: str | None,
    app_metadata: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    app_metadata = app_metadata or {}
    payload = {
        "sub": auth_identity_id,
        "auth_identity_id": auth_identity_id,
        "actor_id": app_metadata.get("customer_id", "") or "",
        "actor_type": "customer",
        "app_metadata": app_metadata,
        "user_metadata": {"email": email} if email else {},
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=_TOKEN_TTL_DAYS)).timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.ALGORITHM)


def decode_marketplace_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.ALGORITHM])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid marketplace token") from exc

    auth_identity_id = payload.get("auth_identity_id") or payload.get("sub")
    if not auth_identity_id:
        raise HTTPException(status_code=401, detail="Marketplace token is missing auth identity")

    return payload


def extract_marketplace_token(request: Request) -> str | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if token:
            return token

    for cookie_name in (
        "_medusa_jwt",
        "medusa_jwt",
        "medusa_auth_token",
        "auth_token",
        "jwt",
    ):
        token = request.cookies.get(cookie_name)
        if token:
            return token

    return None
