import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile, status

from app.auth.deps import CurrentUser, require_roles
from app.auth.roles import Role
from app.config import get_settings
from app.products import service
from app.products.schemas import ImageOut, ProductCreate, ProductOut, ProductUpdate
from app.supabase_client import get_supabase

logger = logging.getLogger("audit")

router = APIRouter(prefix="/products", tags=["products"])

# Writes are super-admin only; reads are public (no auth dependency at all).
SuperAdmin = Annotated[CurrentUser, Depends(require_roles(Role.SUPER_ADMIN))]


def _out(row: dict) -> ProductOut:
    return service.to_product_out(row, get_settings().supabase_url)


def _not_found(_: service.ProductNotFoundError) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


@router.get("")
def list_products(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ProductOut]:
    return [_out(r) for r in service.list_products(get_supabase(), limit, offset)]


@router.get("/{product_id}")
def get_product(product_id: UUID) -> ProductOut:
    try:
        return _out(service.get_product(get_supabase(), str(product_id)))
    except service.ProductNotFoundError as e:
        raise _not_found(e) from e


@router.post("", status_code=status.HTTP_201_CREATED)
def create_product(data: ProductCreate, current: SuperAdmin) -> ProductOut:
    db = get_supabase()
    product_id = service.create_product(db, data)
    logger.info("product.create actor=%s product=%s", current.id, product_id)
    return _out(service.get_product(db, product_id))


@router.patch("/{product_id}")
def update_product(product_id: UUID, data: ProductUpdate, current: SuperAdmin) -> ProductOut:
    db = get_supabase()
    try:
        service.update_product(db, str(product_id), data)
        row = service.get_product(db, str(product_id))
    except service.ProductNotFoundError as e:
        raise _not_found(e) from e
    logger.info("product.update actor=%s product=%s", current.id, product_id)
    return _out(row)


@router.post("/{product_id}/images", status_code=status.HTTP_201_CREATED)
def upload_image(product_id: UUID, file: UploadFile, current: SuperAdmin) -> ImageOut:
    # Read one byte past the cap so oversize files are rejected without buffering them whole.
    data = file.file.read(service.MAX_IMAGE_BYTES + 1)
    if len(data) > service.MAX_IMAGE_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Image must be 5 MB or smaller")
    try:
        row = service.add_image(get_supabase(), str(product_id), data)
    except service.ProductNotFoundError as e:
        raise _not_found(e) from e
    except service.InvalidImageError as e:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(e)) from e
    except service.TooManyImagesError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    logger.info("product.image.add actor=%s product=%s image=%s", current.id, product_id, row["id"])
    return ImageOut(
        id=row["id"],
        url=service.public_url(get_settings().supabase_url, row["storage_path"]),
        position=row["position"],
    )


@router.delete("/{product_id}/images/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_image(product_id: UUID, image_id: UUID, current: SuperAdmin) -> Response:
    try:
        service.delete_image(get_supabase(), str(product_id), str(image_id))
    except service.ProductNotFoundError as e:
        raise _not_found(e) from e
    logger.info("product.image.delete actor=%s product=%s image=%s", current.id, product_id, image_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
