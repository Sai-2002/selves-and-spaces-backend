import uuid

from app.money import money
from app.products.schemas import ProductCreate, ProductOut, ProductUpdate

BUCKET = "product-images"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES_PER_PRODUCT = 10

SELECT = "*, images:product_images(*), details:product_details(*)"


class ProductNotFoundError(Exception):
    pass


class InvalidImageError(Exception):
    pass


class TooManyImagesError(Exception):
    pass


def detect_image_type(data: bytes) -> tuple[str, str] | None:
    """(content type, extension) from the file's magic bytes. The client's header/filename can lie."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def public_url(supabase_url: str, storage_path: str) -> str:
    return f"{supabase_url.rstrip('/')}/storage/v1/object/public/{BUCKET}/{storage_path}"


def to_product_out(row: dict, supabase_url: str) -> ProductOut:
    images = sorted(row.get("images") or [], key=lambda i: (i["position"], i["created_at"]))
    details = sorted(row.get("details") or [], key=lambda d: d["position"])
    return ProductOut(
        id=row["id"],
        title=row["title"],
        amount=money(row["amount"]),
        description=row["description"],
        images=[
            {"id": i["id"], "url": public_url(supabase_url, i["storage_path"]), "position": i["position"]}
            for i in images
        ],
        details=details,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _payload(model: ProductCreate | ProductUpdate) -> dict:
    # mode="json" turns Decimal into a string, which Postgres casts to numeric exactly.
    return model.model_dump(mode="json", exclude_unset=True)


def list_products(db, limit: int, offset: int) -> list[dict]:
    return (
        db.table("products")
        .select(SELECT)
        .order("created_at", desc=True)
        .order("id")
        .range(offset, offset + limit - 1)
        .execute()
        .data
    )


def get_product(db, product_id: str) -> dict:
    rows = db.table("products").select(SELECT).eq("id", product_id).limit(1).execute().data
    if not rows:
        raise ProductNotFoundError(product_id)
    return rows[0]


def create_product(db, data: ProductCreate) -> str:
    return db.rpc("create_product", {"p": _payload(data)}).execute().data


def update_product(db, product_id: str, data: ProductUpdate) -> None:
    payload = _payload(data)
    # An explicit null for a required column would otherwise reach SQL as NULL.
    payload = {k: v for k, v in payload.items() if v is not None}
    found = db.rpc("update_product", {"pid": product_id, "p": payload}).execute().data
    if not found:
        raise ProductNotFoundError(product_id)


def add_image(db, product_id: str, data: bytes) -> dict:
    detected = detect_image_type(data)
    if detected is None:
        raise InvalidImageError("Only JPEG, PNG or WebP images are allowed")
    content_type, ext = detected

    get_product(db, product_id)  # 404 before touching storage
    existing = db.table("product_images").select("position").eq("product_id", product_id).execute().data
    if len(existing) >= MAX_IMAGES_PER_PRODUCT:
        raise TooManyImagesError(f"A product can have at most {MAX_IMAGES_PER_PRODUCT} images")

    path = f"{product_id}/{uuid.uuid4()}.{ext}"
    db.storage.from_(BUCKET).upload(path, data, {"content-type": content_type})
    position = max((r["position"] for r in existing), default=-1) + 1
    try:
        row = (
            db.table("product_images")
            .insert({"product_id": product_id, "storage_path": path, "position": position})
            .execute()
            .data[0]
        )
    except Exception:
        db.storage.from_(BUCKET).remove([path])  # don't leave an orphaned file behind
        raise
    return row


def delete_image(db, product_id: str, image_id: str) -> None:
    rows = (
        db.table("product_images")
        .delete()
        .eq("id", image_id)
        .eq("product_id", product_id)
        .execute()
        .data
    )
    if not rows:
        raise ProductNotFoundError(image_id)
    db.storage.from_(BUCKET).remove([rows[0]["storage_path"]])
