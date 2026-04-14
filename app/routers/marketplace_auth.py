import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.services.marketplace_auth import (
    create_marketplace_token,
    decode_marketplace_token,
    extract_marketplace_token,
    generate_prefixed_id,
    hash_marketplace_password,
    normalize_email,
    verify_marketplace_password,
)
from app.services.marketplace_verification import (
    issue_marketplace_verification_code,
    verify_marketplace_verification_code,
)

router = APIRouter(tags=["marketplace-auth"])

_SESSION_COOKIE_NAMES = (
    "_medusa_jwt",
    "medusa_jwt",
    "medusa_auth_token",
)


class MarketplaceEmailPasswordPayload(BaseModel):
    email: EmailStr
    password: str


class MarketplaceCustomerCreatePayload(BaseModel):
    email: EmailStr | None = None
    company_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    metadata: dict[str, Any] | None = None


class MarketplaceVerificationCodePayload(BaseModel):
    code: str


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"type": "unauthorized", "message": message},
    )


def _json_dump(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {})


def _set_session_cookie(response: Response, token: str) -> None:
    for cookie_name in _SESSION_COOKIE_NAMES:
        response.set_cookie(
            key=cookie_name,
            value=token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=7 * 24 * 60 * 60,
        )


def _clear_session_cookie(response: Response) -> None:
    for cookie_name in _SESSION_COOKIE_NAMES:
        response.delete_cookie(cookie_name, httponly=True, samesite="lax")


async def _fetch_row(
    session: AsyncSession,
    query: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    result = await session.execute(text(query), params or {})
    row = result.first()
    if not row:
        return None
    return dict(row._mapping)


async def _get_provider_identity_by_email(
    session: AsyncSession,
    email: str,
) -> dict[str, Any] | None:
    return await _fetch_row(
        session,
        """
        select id, provider, entity_id, auth_identity_id, provider_metadata, user_metadata
        from provider_identity
        where provider = 'emailpass'
          and deleted_at is null
          and lower(entity_id) = :email
        limit 1
        """,
        {"email": email},
    )


async def _get_provider_identity_by_auth_identity(
    session: AsyncSession,
    auth_identity_id: str,
) -> dict[str, Any] | None:
    return await _fetch_row(
        session,
        """
        select id, provider, entity_id, auth_identity_id, provider_metadata, user_metadata
        from provider_identity
        where provider = 'emailpass'
          and deleted_at is null
          and auth_identity_id = :auth_identity_id
        limit 1
        """,
        {"auth_identity_id": auth_identity_id},
    )


async def _get_auth_identity(
    session: AsyncSession,
    auth_identity_id: str,
) -> dict[str, Any] | None:
    return await _fetch_row(
        session,
        """
        select id, app_metadata, created_at, updated_at
        from auth_identity
        where id = :auth_identity_id
          and deleted_at is null
        limit 1
        """,
        {"auth_identity_id": auth_identity_id},
    )


async def _get_customer_by_id(
    session: AsyncSession,
    customer_id: str,
) -> dict[str, Any] | None:
    return await _fetch_row(
        session,
        """
        select
            id,
            email,
            company_name,
            first_name,
            last_name,
            phone,
            has_account,
            metadata,
            created_at,
            updated_at,
            created_by
        from customer
        where id = :customer_id
          and deleted_at is null
        limit 1
        """,
        {"customer_id": customer_id},
    )


async def _get_customer_by_email(
    session: AsyncSession,
    email: str,
) -> dict[str, Any] | None:
    return await _fetch_row(
        session,
        """
        select
            id,
            email,
            company_name,
            first_name,
            last_name,
            phone,
            has_account,
            metadata,
            created_at,
            updated_at,
            created_by
        from customer
        where deleted_at is null
          and lower(email) = :email
        order by created_at desc
        limit 1
        """,
        {"email": email},
    )


async def _update_auth_identity_app_metadata(
    session: AsyncSession,
    auth_identity_id: str,
    app_metadata: dict[str, Any],
) -> None:
    await session.execute(
        text(
            """
            update auth_identity
            set app_metadata = cast(:app_metadata as jsonb),
                updated_at = :updated_at
            where id = :auth_identity_id
            """
        ),
        {
            "auth_identity_id": auth_identity_id,
            "app_metadata": _json_dump(app_metadata),
            "updated_at": datetime.now(timezone.utc),
        },
    )


async def _update_customer_metadata(
    session: AsyncSession,
    customer_id: str,
    metadata: dict[str, Any],
) -> None:
    await session.execute(
        text(
            """
            update customer
            set metadata = cast(:metadata as jsonb),
                updated_at = :updated_at
            where id = :customer_id
            """
        ),
        {
            "customer_id": customer_id,
            "metadata": _json_dump(metadata),
            "updated_at": datetime.now(timezone.utc),
        },
    )


async def _get_marketplace_context(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    token = extract_marketplace_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Marketplace authentication is required")

    payload = decode_marketplace_token(token)
    auth_identity_id = payload.get("auth_identity_id") or payload.get("sub")
    auth_identity = await _get_auth_identity(session, auth_identity_id)
    if not auth_identity:
        raise HTTPException(status_code=401, detail="Marketplace auth identity was not found")

    provider_identity = await _get_provider_identity_by_auth_identity(session, auth_identity_id)
    email = (
        (payload.get("user_metadata") or {}).get("email")
        or (provider_identity or {}).get("entity_id")
    )
    app_metadata = auth_identity.get("app_metadata") or {}

    return {
        "token": token,
        "payload": payload,
        "auth_identity": auth_identity,
        "auth_identity_id": auth_identity_id,
        "provider_identity": provider_identity,
        "email": normalize_email(email) if email else None,
        "customer_id": app_metadata.get("customer_id"),
        "app_metadata": app_metadata,
    }


@router.post("/auth/customer/emailpass/register")
async def register_marketplace_customer(
    payload: MarketplaceEmailPasswordPayload,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    email = normalize_email(payload.email)
    existing_identity = await _get_provider_identity_by_email(session, email)
    if existing_identity:
        return _unauthorized("Identity with this email already exists")

    now = datetime.now(timezone.utc)
    auth_identity_id = generate_prefixed_id("authid")
    provider_identity_id = generate_prefixed_id()

    await session.execute(
        text(
            """
            insert into auth_identity (id, app_metadata, created_at, updated_at, deleted_at)
            values (:id, cast(:app_metadata as jsonb), :created_at, :updated_at, null)
            """
        ),
        {
            "id": auth_identity_id,
            "app_metadata": _json_dump({}),
            "created_at": now,
            "updated_at": now,
        },
    )

    await session.execute(
        text(
            """
            insert into provider_identity (
                id,
                entity_id,
                provider,
                auth_identity_id,
                user_metadata,
                provider_metadata,
                created_at,
                updated_at,
                deleted_at
            )
            values (
                :id,
                :entity_id,
                'emailpass',
                :auth_identity_id,
                null,
                cast(:provider_metadata as jsonb),
                :created_at,
                :updated_at,
                null
            )
            """
        ),
        {
            "id": provider_identity_id,
            "entity_id": email,
            "auth_identity_id": auth_identity_id,
            "provider_metadata": _json_dump(
                {"password": hash_marketplace_password(payload.password)}
            ),
            "created_at": now,
            "updated_at": now,
        },
    )
    await session.commit()

    token = create_marketplace_token(
        auth_identity_id=auth_identity_id,
        email=email,
        app_metadata={},
    )
    _set_session_cookie(response, token)
    return {"token": token}


@router.post("/auth/customer/emailpass")
async def login_marketplace_customer(
    payload: MarketplaceEmailPasswordPayload,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    email = normalize_email(payload.email)
    provider_identity = await _get_provider_identity_by_email(session, email)
    if not provider_identity:
        return _unauthorized("Invalid email or password")

    provider_metadata = provider_identity.get("provider_metadata") or {}
    stored_password_hash = provider_metadata.get("password")
    if not verify_marketplace_password(payload.password, stored_password_hash):
        return _unauthorized("Invalid email or password")

    auth_identity = await _get_auth_identity(session, provider_identity["auth_identity_id"])
    if not auth_identity:
        return _unauthorized("Invalid email or password")

    app_metadata = auth_identity.get("app_metadata") or {}
    token = create_marketplace_token(
        auth_identity_id=provider_identity["auth_identity_id"],
        email=email,
        app_metadata=app_metadata,
    )
    _set_session_cookie(response, token)
    return {"token": token}


@router.post("/auth/token/refresh")
async def refresh_marketplace_token(
    response: Response,
    context: dict[str, Any] = Depends(_get_marketplace_context),
):
    token = create_marketplace_token(
        auth_identity_id=context["auth_identity_id"],
        email=context["email"],
        app_metadata=context["app_metadata"],
    )
    _set_session_cookie(response, token)
    return {"token": token}


@router.post("/auth/session")
async def create_marketplace_session(request: Request, response: Response):
    token = extract_marketplace_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Authorization bearer token is required")

    decode_marketplace_token(token)
    _set_session_cookie(response, token)
    return {"success": True}


@router.delete("/auth/session")
async def delete_marketplace_session(response: Response):
    _clear_session_cookie(response)
    return {"success": True}


@router.post("/store/customers")
async def create_marketplace_customer(
    payload: MarketplaceCustomerCreatePayload,
    response: Response,
    context: dict[str, Any] = Depends(_get_marketplace_context),
    session: AsyncSession = Depends(get_session),
):
    auth_identity_id = context["auth_identity_id"]
    email = normalize_email(payload.email or context["email"] or "")
    if not email:
        raise HTTPException(status_code=400, detail="Customer email is required")

    existing_customer = None
    if context["customer_id"]:
        existing_customer = await _get_customer_by_id(session, context["customer_id"])

    if not existing_customer:
        existing_customer = await _get_customer_by_email(session, email)

    now = datetime.now(timezone.utc)
    customer_id = None

    if existing_customer:
        customer_id = existing_customer["id"]
        merged_metadata = existing_customer.get("metadata") or {}
        if payload.metadata:
            merged_metadata = {**merged_metadata, **payload.metadata}
        merged_metadata["email_verified"] = merged_metadata.get("email_verified") is True

        await session.execute(
            text(
                """
                update customer
                set email = :email,
                    company_name = coalesce(:company_name, company_name),
                    first_name = coalesce(:first_name, first_name),
                    last_name = coalesce(:last_name, last_name),
                    phone = coalesce(:phone, phone),
                    metadata = cast(:metadata as jsonb),
                    has_account = true,
                    updated_at = :updated_at
                where id = :customer_id
                """
            ),
            {
                "customer_id": customer_id,
                "email": email,
                "company_name": payload.company_name,
                "first_name": payload.first_name,
                "last_name": payload.last_name,
                "phone": payload.phone,
                "metadata": _json_dump(merged_metadata),
                "updated_at": now,
            },
        )
    else:
        customer_id = generate_prefixed_id("cus")
        initial_metadata = {**(payload.metadata or {})}
        initial_metadata["email_verified"] = False
        await session.execute(
            text(
                """
                insert into customer (
                    id,
                    company_name,
                    first_name,
                    last_name,
                    email,
                    phone,
                    has_account,
                    metadata,
                    created_at,
                    updated_at,
                    deleted_at,
                    created_by
                )
                values (
                    :id,
                    :company_name,
                    :first_name,
                    :last_name,
                    :email,
                    :phone,
                    true,
                    cast(:metadata as jsonb),
                    :created_at,
                    :updated_at,
                    null,
                    null
                )
                """
            ),
            {
                "id": customer_id,
                "company_name": payload.company_name,
                "first_name": payload.first_name,
                "last_name": payload.last_name,
                "email": email,
                "phone": payload.phone,
                "metadata": _json_dump(initial_metadata),
                "created_at": now,
                "updated_at": now,
            },
        )

    updated_app_metadata = {**(context["app_metadata"] or {}), "customer_id": customer_id}
    await _update_auth_identity_app_metadata(session, auth_identity_id, updated_app_metadata)
    await session.commit()

    customer = await _get_customer_by_id(session, customer_id)
    verification = None
    customer_metadata = customer.get("metadata") or {}
    if customer_metadata.get("email_verified") is not True:
        verification = await issue_marketplace_verification_code(
            session,
            customer_id=customer_id,
            auth_identity_id=auth_identity_id,
            email=email,
            issued_for="signup",
            enforce_cooldown=False,
        )
        customer_metadata = {
            **customer_metadata,
            "email_verified": False,
            "verification_required": True,
            "verification_last_sent_at": verification["created_at"],
            "verification_expires_at": verification["expires_at"],
            "verification_delivery_status": verification["delivery_status"],
            "verification_sent_via": verification["sent_via"],
        }
        if verification.get("delivery_error"):
            customer_metadata["verification_delivery_error"] = verification["delivery_error"]
        else:
            customer_metadata.pop("verification_delivery_error", None)
        await _update_customer_metadata(session, customer_id, customer_metadata)
        await session.commit()
        customer = await _get_customer_by_id(session, customer_id)

    token = create_marketplace_token(
        auth_identity_id=auth_identity_id,
        email=email,
        app_metadata=updated_app_metadata,
    )
    _set_session_cookie(response, token)
    return {"customer": customer, "verification": verification}


@router.get("/store/customers/me")
async def get_marketplace_customer(
    context: dict[str, Any] = Depends(_get_marketplace_context),
    session: AsyncSession = Depends(get_session),
):
    customer_id = context["customer_id"]
    if not customer_id:
        raise HTTPException(status_code=404, detail="No customer is linked to this auth identity")

    customer = await _get_customer_by_id(session, customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    return {"customer": customer}


@router.post("/auth/customer/emailpass/resend")
async def resend_marketplace_verification(
    context: dict[str, Any] = Depends(_get_marketplace_context),
    session: AsyncSession = Depends(get_session),
):
    customer_id = context["customer_id"]
    if not customer_id:
        raise HTTPException(status_code=404, detail="Customer not found")

    customer = await _get_customer_by_id(session, customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    metadata = customer.get("metadata") or {}
    if metadata.get("email_verified") is True:
        return {"verified": True, "customer": customer, "verification": None}

    email = normalize_email(customer.get("email") or context["email"] or "")
    if not email:
        raise HTTPException(status_code=400, detail="Customer email is required")

    verification = await issue_marketplace_verification_code(
        session,
        customer_id=customer_id,
        auth_identity_id=context["auth_identity_id"],
        email=email,
        issued_for="resend",
        enforce_cooldown=True,
    )
    metadata = {
        **metadata,
        "email_verified": False,
        "verification_required": True,
        "verification_last_sent_at": verification["created_at"],
        "verification_expires_at": verification["expires_at"],
        "verification_delivery_status": verification["delivery_status"],
        "verification_sent_via": verification["sent_via"],
    }
    if verification.get("delivery_error"):
        metadata["verification_delivery_error"] = verification["delivery_error"]
    else:
        metadata.pop("verification_delivery_error", None)
    await _update_customer_metadata(session, customer_id, metadata)
    await session.commit()

    customer = await _get_customer_by_id(session, customer_id)
    return {"verified": False, "customer": customer, "verification": verification}


@router.post("/auth/customer/emailpass/verify")
async def verify_marketplace_email(
    payload: MarketplaceVerificationCodePayload,
    response: Response,
    context: dict[str, Any] = Depends(_get_marketplace_context),
    session: AsyncSession = Depends(get_session),
):
    code = payload.code.strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(status_code=400, detail="Verification code must be 6 digits.")

    customer_id = context["customer_id"]
    if not customer_id:
        raise HTTPException(status_code=404, detail="Customer not found")

    customer = await _get_customer_by_id(session, customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    metadata = customer.get("metadata") or {}
    if metadata.get("email_verified") is True:
        return {"verified": True, "customer": customer}

    email = normalize_email(customer.get("email") or context["email"] or "")
    if not email:
        raise HTTPException(status_code=400, detail="Customer email is required")

    verification = await verify_marketplace_verification_code(
        session,
        customer_id=customer_id,
        email=email,
        code=code,
    )
    verified_at = verification["verified_at"]
    metadata = {
        **metadata,
        "email_verified": True,
        "email_verified_at": verified_at,
        "verification_required": False,
        "verification_last_verified_at": verified_at,
    }
    metadata.pop("verification_delivery_error", None)
    metadata["verification_delivery_status"] = "verified"
    await _update_customer_metadata(session, customer_id, metadata)

    updated_app_metadata = {
        **(context["app_metadata"] or {}),
        "customer_id": customer_id,
        "email_verified": True,
        "email_verified_at": verified_at,
    }
    await _update_auth_identity_app_metadata(
        session,
        context["auth_identity_id"],
        updated_app_metadata,
    )
    await session.commit()

    customer = await _get_customer_by_id(session, customer_id)
    refreshed_token = create_marketplace_token(
        auth_identity_id=context["auth_identity_id"],
        email=email,
        app_metadata=updated_app_metadata,
    )
    _set_session_cookie(response, refreshed_token)
    return {"verified": True, "customer": customer}
