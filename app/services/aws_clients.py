import boto3

from app.config import settings


def _build_client(
    service_name: str,
    *,
    access_key: str | None = None,
    secret_key: str | None = None,
    region_name: str | None = None,
):
    client_kwargs: dict[str, str] = {"region_name": region_name or settings.AWS_REGION}

    if access_key and secret_key:
        client_kwargs["aws_access_key_id"] = access_key
        client_kwargs["aws_secret_access_key"] = secret_key

    return boto3.client(service_name, **client_kwargs)

def get_bitcoin_ses_client():
    return _build_client(
        "ses",
        access_key=settings.BITCOIN_AWS_ACCESS_KEY,
        secret_key=settings.BITCOIN_AWS_SECRET_ACCESS_KEY,
    )
