import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from email.utils import formataddr
from html import escape
from typing import Any
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.services.aws_clients import get_bitcoin_ses_client
from app.services.marketplace_auth import generate_prefixed_id


logger = logging.getLogger(__name__)

_VERIFICATION_TABLE = "marketplace_email_verification"
_CODE_LENGTH = 6


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash_verification_code(code: str) -> str:
    seed = f"{settings.JWT_SECRET}:{code}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()


def _generate_verification_code(length: int = _CODE_LENGTH) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


def _can_send_verification_email() -> bool:
    return bool(settings.MARKETPLACE_VERIFICATION_FROM_EMAIL)


def _verification_link(email: str, code: str) -> str:
    base_url = settings.MARKETPLACE_FRONTEND_URL.rstrip("/")
    return f"{base_url}/verify?email={quote(email)}&code={quote(code)}"


def _build_verification_email(email: str, code: str, expires_at: datetime) -> dict[str, str]:
    expires_minutes = max(settings.MARKETPLACE_VERIFICATION_TTL_MINUTES, 1)
    link = _verification_link(email, code)
    escaped_link = escape(link, quote=True)
    html = f"""
    <html>
      <body style="font-family:Arial,sans-serif;color:#111827;line-height:1.5;">
        <h2 style="margin-bottom:8px;">Verify your Bitcoin Culture Hub email</h2>
        <p style="margin:0 0 16px;">Use this code to finish your marketplace setup:</p>
        <div style="font-size:32px;font-weight:700;letter-spacing:6px;margin:12px 0 20px;">
          {escape(code)}
        </div>
        <p style="margin:0 0 16px;">This code expires in {expires_minutes} minutes.</p>
        <p style="margin:0 0 16px;">
          Or open this link in the same browser session:
          <a href="{escaped_link}">{escaped_link}</a>
        </p>
        <p style="margin-top:24px;color:#6b7280;font-size:12px;">
          If you did not request this, you can safely ignore this email.
        </p>
      </body>
    </html>
    """.strip()
    text_body = (
        "Verify your Bitcoin Culture Hub email\n\n"
        f"Code: {code}\n"
        f"Expires at: {expires_at.isoformat()}\n"
        f"Link: {link}\n"
    )
    return {"html": html, "text": text_body}


def _send_verification_email(email: str, code: str, expires_at: datetime) -> tuple[str, str | None, str]:
    if not _can_send_verification_email():
        return ("skipped", "SES credentials or sender email are not configured.", "preview")

    payload = _build_verification_email(email=email, code=code, expires_at=expires_at)
    source = formataddr(
        (
            settings.MARKETPLACE_VERIFICATION_FROM_NAME,
            settings.MARKETPLACE_VERIFICATION_FROM_EMAIL or "",
        )
    )

    try:
        ses_client = get_bitcoin_ses_client()
        ses_client.send_email(
            Source=source,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": "Verify your Bitcoin Culture Hub email", "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": payload["text"], "Charset": "UTF-8"},
                    "Html": {"Data": payload["html"], "Charset": "UTF-8"},
                },
            },
        )
        return ("sent", None, "ses")
    except (BotoCoreError, ClientError, ValueError) as exc:
        logger.warning("Marketplace verification email failed for %s: %s", email, exc)
        return ("error", str(exc), "ses")


async def ensure_marketplace_verification_table(session: AsyncSession) -> None:
    await session.execute(
        text(
            f"""
            create table if not exists {_VERIFICATION_TABLE} (
                id text primary key,
                customer_id text not null,
                auth_identity_id text not null,
                email text not null,
                code_hash text not null,
                issued_for text not null default 'signup',
                sent_via text not null default 'preview',
                delivery_status text not null default 'pending',
                delivery_error text,
                metadata jsonb not null default '{{}}'::jsonb,
                created_at timestamptz not null,
                expires_at timestamptz not null,
                used_at timestamptz,
                superseded_at timestamptz
            )
            """
        )
    )
    await session.execute(
        text(
            f"""
            create index if not exists idx_{_VERIFICATION_TABLE}_customer_created
            on {_VERIFICATION_TABLE} (customer_id, created_at desc)
            """
        )
    )
    await session.execute(
        text(
            f"""
            create index if not exists idx_{_VERIFICATION_TABLE}_email_created
            on {_VERIFICATION_TABLE} (email, created_at desc)
            """
        )
    )


async def _latest_verification_attempt(
    session: AsyncSession,
    *,
    customer_id: str,
    email: str,
) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            f"""
            select
                id,
                customer_id,
                auth_identity_id,
                email,
                code_hash,
                issued_for,
                sent_via,
                delivery_status,
                delivery_error,
                metadata,
                created_at,
                expires_at,
                used_at,
                superseded_at
            from {_VERIFICATION_TABLE}
            where customer_id = :customer_id
              and lower(email) = lower(:email)
            order by created_at desc
            limit 1
            """
        ),
        {"customer_id": customer_id, "email": email},
    )
    row = result.first()
    if not row:
        return None
    return dict(row._mapping)


async def _latest_active_verification(
    session: AsyncSession,
    *,
    customer_id: str,
    email: str,
) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            f"""
            select
                id,
                customer_id,
                auth_identity_id,
                email,
                code_hash,
                issued_for,
                sent_via,
                delivery_status,
                delivery_error,
                metadata,
                created_at,
                expires_at,
                used_at,
                superseded_at
            from {_VERIFICATION_TABLE}
            where customer_id = :customer_id
              and lower(email) = lower(:email)
              and used_at is null
              and superseded_at is null
            order by created_at desc
            limit 1
            """
        ),
        {"customer_id": customer_id, "email": email},
    )
    row = result.first()
    if not row:
        return None
    return dict(row._mapping)


def _should_include_preview(delivery_status: str) -> bool:
    return delivery_status != "sent"


async def issue_marketplace_verification_code(
    session: AsyncSession,
    *,
    customer_id: str,
    auth_identity_id: str,
    email: str,
    issued_for: str,
    enforce_cooldown: bool = False,
) -> dict[str, Any]:
    await ensure_marketplace_verification_table(session)

    now = _utcnow()
    latest_attempt = await _latest_verification_attempt(
        session,
        customer_id=customer_id,
        email=email,
    )
    cooldown_seconds = max(settings.MARKETPLACE_VERIFICATION_RESEND_COOLDOWN_SECONDS, 0)

    if (
        enforce_cooldown
        and latest_attempt
        and latest_attempt.get("created_at")
        and cooldown_seconds > 0
    ):
        elapsed = (now - latest_attempt["created_at"]).total_seconds()
        if elapsed < cooldown_seconds:
            retry_after = int(cooldown_seconds - elapsed)
            raise HTTPException(
                status_code=429,
                detail=f"Please wait {retry_after} seconds before requesting another code.",
            )

    await session.execute(
        text(
            f"""
            update {_VERIFICATION_TABLE}
            set superseded_at = :superseded_at
            where customer_id = :customer_id
              and lower(email) = lower(:email)
              and used_at is null
              and superseded_at is null
            """
        ),
        {
            "customer_id": customer_id,
            "email": email,
            "superseded_at": now,
        },
    )

    code = _generate_verification_code()
    expires_at = now + timedelta(
        minutes=max(settings.MARKETPLACE_VERIFICATION_TTL_MINUTES, 1)
    )
    delivery_status, delivery_error, sent_via = _send_verification_email(
        email=email,
        code=code,
        expires_at=expires_at,
    )

    verification_id = generate_prefixed_id("verif")
    await session.execute(
        text(
            f"""
            insert into {_VERIFICATION_TABLE} (
                id,
                customer_id,
                auth_identity_id,
                email,
                code_hash,
                issued_for,
                sent_via,
                delivery_status,
                delivery_error,
                metadata,
                created_at,
                expires_at,
                used_at,
                superseded_at
            )
            values (
                :id,
                :customer_id,
                :auth_identity_id,
                :email,
                :code_hash,
                :issued_for,
                :sent_via,
                :delivery_status,
                :delivery_error,
                cast(:metadata as jsonb),
                :created_at,
                :expires_at,
                null,
                null
            )
            """
        ),
        {
            "id": verification_id,
            "customer_id": customer_id,
            "auth_identity_id": auth_identity_id,
            "email": email,
            "code_hash": _hash_verification_code(code),
            "issued_for": issued_for,
            "sent_via": sent_via,
            "delivery_status": delivery_status,
            "delivery_error": delivery_error,
            "metadata": "{}",
            "created_at": now,
            "expires_at": expires_at,
        },
    )

    response = {
        "id": verification_id,
        "email": email,
        "issued_for": issued_for,
        "delivery_status": delivery_status,
        "delivery_error": delivery_error,
        "sent_via": sent_via,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "resend_available_at": (now + timedelta(seconds=cooldown_seconds)).isoformat(),
    }
    if _should_include_preview(delivery_status):
        response["preview_code"] = code
    return response


async def verify_marketplace_verification_code(
    session: AsyncSession,
    *,
    customer_id: str,
    email: str,
    code: str,
) -> dict[str, Any]:
    await ensure_marketplace_verification_table(session)

    active_attempt = await _latest_active_verification(
        session,
        customer_id=customer_id,
        email=email,
    )
    if not active_attempt:
        raise HTTPException(status_code=404, detail="No pending verification code was found.")

    now = _utcnow()
    if active_attempt["expires_at"] <= now:
        raise HTTPException(status_code=400, detail="Verification code has expired.")

    if _hash_verification_code(code) != active_attempt["code_hash"]:
        raise HTTPException(status_code=400, detail="Verification code is invalid.")

    await session.execute(
        text(
            f"""
            update {_VERIFICATION_TABLE}
            set used_at = :used_at
            where id = :verification_id
            """
        ),
        {
            "verification_id": active_attempt["id"],
            "used_at": now,
        },
    )

    return {
        "id": active_attempt["id"],
        "customer_id": customer_id,
        "email": email,
        "verified_at": now.isoformat(),
    }
