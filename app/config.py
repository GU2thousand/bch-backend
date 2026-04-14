import json
import os
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()


def _first_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _set_env_if_missing(key: str, value: Any) -> None:
    if value is None:
        return
    if not os.getenv(key):
        os.environ[key] = str(value)


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_aliases() -> None:
    database_url = _first_env("DEPLOYED_DATABASE_URL", "DATABASE_URL")
    if database_url:
        _set_env_if_missing("DEPLOYED_DATABASE_URL", database_url)
        _set_env_if_missing("DATABASE_URL", database_url)

    jwt_secret = _first_env("JWT_SECRET", "SECRET_KEY")
    if jwt_secret:
        _set_env_if_missing("JWT_SECRET", jwt_secret)
        _set_env_if_missing("SECRET_KEY", jwt_secret)

    region = _first_env("AWS_SECRETS_MANAGER_REGION", "AWS_REGION", "AWS_DEFAULT_REGION", default="us-east-2")
    _set_env_if_missing("AWS_REGION", region)
    _set_env_if_missing("AWS_SECRETS_MANAGER_REGION", region)


def _load_secret_payload(secret_id: str, region_name: str) -> dict[str, Any]:
    client = boto3.client("secretsmanager", region_name=region_name)
    response = client.get_secret_value(SecretId=secret_id)
    secret_string = response.get("SecretString")

    if not secret_string:
        raise ValueError(f"Secret '{secret_id}' does not contain a SecretString payload.")

    payload = json.loads(secret_string)
    if not isinstance(payload, dict):
        raise ValueError(f"Secret '{secret_id}' must be a JSON object.")

    return payload


def _load_secrets_manager_env() -> None:
    secret_ids_value = _first_env("AWS_SECRETS_MANAGER_SECRET_IDS", "AWS_SECRETS_MANAGER_SECRET_ID")
    if not secret_ids_value:
        _normalize_aliases()
        return

    region_name = _first_env(
        "AWS_SECRETS_MANAGER_REGION",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        default="us-east-2",
    )

    secret_ids = [secret_id.strip() for secret_id in secret_ids_value.split(",") if secret_id.strip()]
    for secret_id in secret_ids:
        try:
            payload = _load_secret_payload(secret_id, region_name)
        except (BotoCoreError, ClientError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unable to load AWS secret '{secret_id}': {exc}") from exc

        for key, value in payload.items():
            _set_env_if_missing(key, value)

    _normalize_aliases()


_load_secrets_manager_env()


class Settings(BaseModel):
    SECRET_KEY: str = _first_env("SECRET_KEY", "JWT_SECRET", default="changeme") or "changeme"
    JWT_SECRET: str = _first_env("JWT_SECRET", "SECRET_KEY", default="changeme") or "changeme"
    ALGORITHM: str = _first_env("ALGORITHM", default="HS256") or "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(
        _first_env("ACCESS_TOKEN_EXPIRE_MINUTES", default="30") or "30"
    )
    CORS_ORIGINS: list[str] = ["*"]
    DATABASE_URL: str | None = _first_env("DEPLOYED_DATABASE_URL", "DATABASE_URL")
    DEPLOYED_DATABASE_URL: str | None = _first_env("DEPLOYED_DATABASE_URL", "DATABASE_URL")
    MAILERLITE_API: str = "https://connect.mailerlite.com/api/subscribers"
    MAILERLITE_TOKEN: str | None = _first_env("MAILERLITE_TOKEN")
    MONGO_URI: str | None = _first_env("MONGO_URI")
    AWS_REGION: str = _first_env("AWS_REGION", "AWS_DEFAULT_REGION", default="us-east-2") or "us-east-2"
    AWS_SECRETS_MANAGER_REGION: str = (
        _first_env("AWS_SECRETS_MANAGER_REGION", "AWS_REGION", "AWS_DEFAULT_REGION", default="us-east-2")
        or "us-east-2"
    )
    AWS_SECRETS_MANAGER_SECRET_ID: str | None = _first_env(
        "AWS_SECRETS_MANAGER_SECRET_ID", "AWS_SECRETS_MANAGER_SECRET_IDS"
    )
    BITCOIN_AWS_ACCESS_KEY: str | None = _first_env("BITCOIN_AWS_ACCESS_KEY")
    BITCOIN_AWS_SECRET_ACCESS_KEY: str | None = _first_env("BITCOIN_AWS_SECRET_ACCESS_KEY")
    MARKETPLACE_FRONTEND_URL: str = (
        _first_env("MARKETPLACE_FRONTEND_URL", default="http://localhost:8080")
        or "http://localhost:8080"
    )
    MARKETPLACE_VERIFICATION_FROM_EMAIL: str | None = _first_env(
        "MARKETPLACE_VERIFICATION_FROM_EMAIL"
    )
    MARKETPLACE_VERIFICATION_FROM_NAME: str = (
        _first_env("MARKETPLACE_VERIFICATION_FROM_NAME", default="Bitcoin Culture Hub")
        or "Bitcoin Culture Hub"
    )
    MARKETPLACE_VERIFICATION_TTL_MINUTES: int = int(
        _first_env("MARKETPLACE_VERIFICATION_TTL_MINUTES", default="15") or "15"
    )
    MARKETPLACE_VERIFICATION_RESEND_COOLDOWN_SECONDS: int = int(
        _first_env("MARKETPLACE_VERIFICATION_RESEND_COOLDOWN_SECONDS", default="60")
        or "60"
    )
    SKIP_SCHEMA_SYNC: bool = _is_truthy(_first_env("SKIP_SCHEMA_SYNC"))


settings = Settings()
