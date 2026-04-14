from decimal import Decimal
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import bindparam, text
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session


router = APIRouter(tags=["marketplace-store"])


def _fetchone_dict(result: Any) -> dict[str, Any] | None:
    row = result.first()
    if not row:
        return None
    return dict(row._mapping)


async def _fetch_row(
    session: AsyncSession,
    query: str,
    params: dict[str, Any] | None = None,
    *,
    expanding: dict[str, list[Any]] | None = None,
) -> dict[str, Any] | None:
    statement = text(query)
    for key in (expanding or {}):
        statement = statement.bindparams(bindparam(key, expanding=True))
    result = await session.execute(statement, {**(params or {}), **(expanding or {})})
    return _fetchone_dict(result)


async def _fetch_all(
    session: AsyncSession,
    query: str,
    params: dict[str, Any] | None = None,
    *,
    expanding: dict[str, list[Any]] | None = None,
) -> list[dict[str, Any]]:
    statement = text(query)
    for key in (expanding or {}):
        statement = statement.bindparams(bindparam(key, expanding=True))
    result = await session.execute(statement, {**(params or {}), **(expanding or {})})
    return [dict(row._mapping) for row in result.fetchall()]


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "collection"


def _series_title(metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(metadata, dict):
        return None
    return metadata.get("series_name") or metadata.get("series")


def _synthetic_collection_id(title: str) -> str:
    return f"pcol_series::{title}"


def _is_synthetic_collection_id(collection_id: str) -> bool:
    return collection_id.startswith("pcol_series::")


def _collection_title_from_id(collection_id: str) -> str:
    return collection_id.split("::", 1)[1]


def _synthetic_collection(title: str) -> dict[str, Any]:
    return {
        "id": _synthetic_collection_id(title),
        "title": title,
        "handle": _slugify(title),
        "metadata": {"synthetic": True},
    }


def _serialize_decimal(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


def _variant_is_sold(metadata: dict[str, Any]) -> bool:
    state = str(metadata.get("state") or "").lower()
    return (
        metadata.get("is_sold") is True
        or metadata.get("sold") is True
        or state == "sold"
    )


def _inventory_quantity_for_variant(metadata: dict[str, Any]) -> int:
    raw_quantity = metadata.get("inventory_quantity")
    if isinstance(raw_quantity, bool):
        return int(raw_quantity)
    if isinstance(raw_quantity, (int, float)):
        return int(raw_quantity)
    if isinstance(raw_quantity, str) and raw_quantity.isdigit():
        return int(raw_quantity)
    return 0 if _variant_is_sold(metadata) else 1


def _build_collection(product_row: dict[str, Any]) -> dict[str, Any] | None:
    if product_row.get("collection_ref_id"):
        return {
            "id": product_row["collection_ref_id"],
            "title": product_row.get("collection_title"),
            "handle": product_row.get("collection_handle"),
            "metadata": product_row.get("collection_metadata") or {},
        }

    title = _series_title(product_row.get("metadata") or {})
    if title:
        return _synthetic_collection(title)
    return None


def _build_price_rows(
    variant: dict[str, Any],
    explicit_prices: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prices = [
        {
            "id": row["id"],
            "currency_code": row["currency_code"],
            "amount": _serialize_decimal(row["amount"]),
            "min_quantity": _serialize_decimal(row.get("min_quantity")),
            "max_quantity": _serialize_decimal(row.get("max_quantity")),
        }
        for row in explicit_prices
    ]
    metadata = variant.get("metadata") or {}
    has_usd = any(str(price.get("currency_code")).lower() == "usd" for price in prices)
    raw_usd = metadata.get("price_usd")
    if not has_usd and raw_usd not in (None, ""):
        try:
            usd_cents = int(round(float(raw_usd) * 100))
        except (TypeError, ValueError):
            usd_cents = None
        if usd_cents is not None:
            prices.append(
                {
                    "id": f"price_preview_{variant['id']}_usd",
                    "currency_code": "usd",
                    "amount": usd_cents,
                    "min_quantity": None,
                    "max_quantity": None,
                }
            )
    return prices


def _build_variant(
    variant: dict[str, Any],
    prices: list[dict[str, Any]],
) -> dict[str, Any]:
    metadata = variant.get("metadata") or {}
    return {
        "id": variant["id"],
        "title": variant.get("title"),
        "sku": variant.get("sku"),
        "metadata": metadata,
        "created_at": variant.get("created_at").isoformat() if variant.get("created_at") else None,
        "updated_at": variant.get("updated_at").isoformat() if variant.get("updated_at") else None,
        "thumbnail": variant.get("thumbnail"),
        "inventory_quantity": _inventory_quantity_for_variant(metadata),
        "prices": _build_price_rows(variant, prices),
    }


def _build_product(
    product: dict[str, Any],
    images: list[dict[str, Any]],
    variants: list[dict[str, Any]],
    variant_prices: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "id": product["id"],
        "title": product.get("title"),
        "handle": product.get("handle"),
        "subtitle": product.get("subtitle"),
        "description": product.get("description"),
        "status": product.get("status"),
        "thumbnail": product.get("thumbnail"),
        "metadata": product.get("metadata") or {},
        "created_at": product.get("created_at").isoformat() if product.get("created_at") else None,
        "updated_at": product.get("updated_at").isoformat() if product.get("updated_at") else None,
        "collection": _build_collection(product),
        "images": [
            {
                "id": image["id"],
                "url": image.get("url"),
                "rank": image.get("rank"),
                "metadata": image.get("metadata") or {},
            }
            for image in images
        ],
        "variants": [
            _build_variant(variant, variant_prices.get(variant["id"], []))
            for variant in variants
        ],
    }


def _product_where_clauses(
    collection_ids: list[str] | None,
) -> tuple[str, dict[str, list[str]]]:
    where_parts = ["p.deleted_at is null", "p.status = 'published'"]
    expanding: dict[str, list[str]] = {}
    actual_collection_ids: list[str] = []
    synthetic_titles: list[str] = []

    for collection_id in collection_ids or []:
        if _is_synthetic_collection_id(collection_id):
            synthetic_titles.append(_collection_title_from_id(collection_id))
        else:
            actual_collection_ids.append(collection_id)

    filter_parts: list[str] = []
    if actual_collection_ids:
        filter_parts.append("p.collection_id in :collection_ids")
        expanding["collection_ids"] = actual_collection_ids
    if synthetic_titles:
        filter_parts.append(
            "coalesce(nullif(p.metadata->>'series_name', ''), nullif(p.metadata->>'series', '')) in :series_titles"
        )
        expanding["series_titles"] = synthetic_titles
    if filter_parts:
        where_parts.append("(" + " or ".join(filter_parts) + ")")

    return " and ".join(where_parts), expanding


async def _load_products(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    collection_ids: list[str] | None,
) -> tuple[list[dict[str, Any]], int]:
    where_clause, expanding = _product_where_clauses(collection_ids)
    count_row = await _fetch_row(
        session,
        f"""
        select count(*) as count
        from product p
        where {where_clause}
        """,
        expanding=expanding or None,
    )
    count = int(count_row["count"]) if count_row else 0

    products = await _fetch_all(
        session,
        f"""
        select
            p.id,
            p.title,
            p.handle,
            p.subtitle,
            p.description,
            p.status,
            p.thumbnail,
            p.metadata,
            p.collection_id,
            p.created_at,
            p.updated_at,
            pc.id as collection_ref_id,
            pc.title as collection_title,
            pc.handle as collection_handle,
            pc.metadata as collection_metadata
        from product p
        left join product_collection pc
          on pc.id = p.collection_id
         and pc.deleted_at is null
        where {where_clause}
        order by p.created_at desc
        limit :limit
        offset :offset
        """,
        params={"limit": limit, "offset": offset},
        expanding=expanding or None,
    )
    return products, count


async def _enrich_products(
    session: AsyncSession,
    products: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not products:
        return []

    product_ids = [product["id"] for product in products]
    images = await _fetch_all(
        session,
        """
        select id, product_id, url, metadata, rank
        from image
        where deleted_at is null
          and product_id in :product_ids
        order by product_id, rank nulls last, created_at asc
        """,
        expanding={"product_ids": product_ids},
    )
    variants = await _fetch_all(
        session,
        """
        select
            id,
            product_id,
            title,
            sku,
            metadata,
            created_at,
            updated_at,
            thumbnail
        from product_variant
        where deleted_at is null
          and product_id in :product_ids
        order by product_id, created_at desc
        """,
        expanding={"product_ids": product_ids},
    )

    variant_ids = [variant["id"] for variant in variants]
    price_rows: list[dict[str, Any]] = []
    if variant_ids:
        price_rows = await _fetch_all(
            session,
            """
            select
                pvps.variant_id,
                p.id,
                p.currency_code,
                p.amount,
                p.min_quantity,
                p.max_quantity
            from product_variant_price_set pvps
            join price p
              on p.price_set_id = pvps.price_set_id
            where pvps.deleted_at is null
              and p.deleted_at is null
              and pvps.variant_id in :variant_ids
            order by pvps.variant_id, p.created_at asc
            """,
            expanding={"variant_ids": variant_ids},
        )

    images_by_product: dict[str, list[dict[str, Any]]] = {}
    for image in images:
        images_by_product.setdefault(image["product_id"], []).append(image)

    variants_by_product: dict[str, list[dict[str, Any]]] = {}
    for variant in variants:
        variants_by_product.setdefault(variant["product_id"], []).append(variant)

    prices_by_variant: dict[str, list[dict[str, Any]]] = {}
    for price in price_rows:
        prices_by_variant.setdefault(price["variant_id"], []).append(price)

    return [
        _build_product(
            product,
            images_by_product.get(product["id"], []),
            variants_by_product.get(product["id"], []),
            prices_by_variant,
        )
        for product in products
    ]


@router.get("/store/products")
async def list_store_products(
    limit: int = Query(default=50, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
    collection_ids: list[str] | None = Query(default=None, alias="collection_id[]"),
    session: AsyncSession = Depends(get_session),
):
    raw_products, count = await _load_products(
        session,
        limit=limit,
        offset=offset,
        collection_ids=collection_ids,
    )
    products = await _enrich_products(session, raw_products)
    return {
        "products": products,
        "count": count,
        "offset": offset,
        "limit": limit,
    }


@router.get("/store/products/{product_id}")
async def retrieve_store_product(
    product_id: str,
    session: AsyncSession = Depends(get_session),
):
    products = await _fetch_all(
        session,
        """
        select
            p.id,
            p.title,
            p.handle,
            p.subtitle,
            p.description,
            p.status,
            p.thumbnail,
            p.metadata,
            p.collection_id,
            p.created_at,
            p.updated_at,
            pc.id as collection_ref_id,
            pc.title as collection_title,
            pc.handle as collection_handle,
            pc.metadata as collection_metadata
        from product p
        left join product_collection pc
          on pc.id = p.collection_id
         and pc.deleted_at is null
        where p.deleted_at is null
          and p.id = :product_id
        limit 1
        """,
        {"product_id": product_id},
    )
    if not products:
        raise HTTPException(status_code=404, detail="Product not found")

    enriched = await _enrich_products(session, products)
    return {"product": enriched[0]}


@router.get("/store/collections")
async def list_store_collections(
    limit: int = Query(default=100, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    actual = await _fetch_all(
        session,
        """
        select id, title, handle, metadata, created_at
        from product_collection
        where deleted_at is null
        order by created_at desc
        """
    )
    synthetic_rows = await _fetch_all(
        session,
        """
        select distinct
            coalesce(nullif(metadata->>'series_name', ''), nullif(metadata->>'series', '')) as title
        from product
        where deleted_at is null
          and status = 'published'
          and coalesce(nullif(metadata->>'series_name', ''), nullif(metadata->>'series', '')) is not null
        order by title asc
        """
    )

    items: list[dict[str, Any]] = []
    seen_titles = {row["title"] for row in actual if row.get("title")}
    for row in actual:
        items.append(
            {
                "id": row["id"],
                "title": row.get("title"),
                "handle": row.get("handle"),
                "metadata": row.get("metadata") or {},
            }
        )
    for row in synthetic_rows:
        title = row.get("title")
        if not title or title in seen_titles:
            continue
        items.append(_synthetic_collection(title))

    paginated = items[offset : offset + limit]
    return {
        "collections": paginated,
        "count": len(items),
        "offset": offset,
        "limit": limit,
    }


@router.get("/store/collections/{collection_id}")
async def retrieve_store_collection(
    collection_id: str,
    session: AsyncSession = Depends(get_session),
):
    if _is_synthetic_collection_id(collection_id):
        title = _collection_title_from_id(collection_id)
        return {"collection": _synthetic_collection(title)}

    collection = await _fetch_row(
        session,
        """
        select id, title, handle, metadata
        from product_collection
        where deleted_at is null
          and id = :collection_id
        limit 1
        """,
        {"collection_id": collection_id},
    )
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    return {
        "collection": {
            "id": collection["id"],
            "title": collection.get("title"),
            "handle": collection.get("handle"),
            "metadata": collection.get("metadata") or {},
        }
    }
