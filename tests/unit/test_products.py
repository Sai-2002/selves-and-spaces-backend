"""Unit tests for the product routes. Supabase (auth, tables, storage, rpc) is faked."""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.products import service

ADMIN_ID = str(uuid4())
CUSTOMER_ID = str(uuid4())
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
NOW = datetime.now(timezone.utc).isoformat()


class Result:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self


class FakeQuery:
    def __init__(self, db, table):
        self.db, self.table, self.filters, self.op, self.row = db, table, {}, "select", None

    def select(self, *_):
        self.op = "select"
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def order(self, *_, **__):
        return self

    def range(self, *_):
        return self

    def limit(self, *_):
        return self

    def insert(self, row):
        self.op, self.row = "insert", row
        return self

    def delete(self):
        self.op = "delete"
        return self

    def _matching(self):
        rows = self.db.tables[self.table]
        return [r for r in rows if all(r.get(k) == v for k, v in self.filters.items())]

    def execute(self):
        if self.op == "insert":
            if self.db.fail_insert:
                raise RuntimeError("db down")
            row = {"id": str(uuid4()), "created_at": NOW, **self.row}
            self.db.tables[self.table].append(row)
            return Result([row])
        rows = self._matching()
        if self.op == "delete":
            for r in rows:
                self.db.tables[self.table].remove(r)
            return Result(rows)
        if self.table == "products":
            rows = [self._nest(r) for r in rows]
        return Result(rows)

    def _nest(self, product):
        return {
            **product,
            "images": [i for i in self.db.tables["product_images"] if i["product_id"] == product["id"]],
            "details": [d for d in self.db.tables["product_details"] if d["product_id"] == product["id"]],
        }


class FakeBucket:
    def __init__(self, db):
        self.db = db

    def upload(self, path, data, opts):
        self.db.files[path] = (data, opts)

    def remove(self, paths):
        for p in paths:
            self.db.files.pop(p, None)


class FakeDB:
    def __init__(self):
        self.tables = {"products": [], "product_images": [], "product_details": []}
        self.files = {}
        self.rpc_calls = []
        self.fail_insert = False
        self.storage = SimpleNamespace(from_=lambda bucket: FakeBucket(self))
        self.auth = self  # get_claims lives here
        self.admin = self  # get_user_by_id lives here
        self.caller = {"sub": ADMIN_ID, "app_metadata": {"roles": ["super_admin"]}}
        self.users = {
            ADMIN_ID: SimpleNamespace(app_metadata={"roles": ["super_admin"]}, banned_until=None),
            CUSTOMER_ID: SimpleNamespace(app_metadata={}, banned_until=None),
        }

    # auth
    def get_claims(self, token):
        return {"claims": self.caller}

    def get_user_by_id(self, uid):
        return SimpleNamespace(user=self.users[uid])

    # data
    def table(self, name):
        return FakeQuery(self, name)

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        if name == "create_product":
            pid = str(uuid4())
            self.tables["products"].append(
                {"id": pid, "description": "", "created_at": NOW, "updated_at": NOW,
                 **{k: params["p"][k] for k in ("title", "amount")},
                 **({"description": params["p"]["description"]} if "description" in params["p"] else {})}
            )
            for i, d in enumerate(params["p"].get("details", [])):
                self.tables["product_details"].append(
                    {"id": str(uuid4()), "product_id": pid, "subtitle": None, **d, "position": i}
                )
            return Result(pid)
        pid = params["pid"]
        found = any(p["id"] == pid for p in self.tables["products"])
        if found:
            next(p for p in self.tables["products"] if p["id"] == pid).update(
                {k: v for k, v in params["p"].items() if k != "details"}
            )
        return Result(found)


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    for target in ("app.auth.deps.get_supabase", "app.products.router.get_supabase"):
        monkeypatch.setattr(target, lambda: fake)
    monkeypatch.setattr("app.products.router.get_settings", lambda: SimpleNamespace(supabase_url="https://x.supabase.co"))
    return fake


@pytest.fixture
def as_admin(db):
    return TestClient(app, headers={"Authorization": "Bearer t.t.t"})


@pytest.fixture
def as_customer(db):
    db.caller = {"sub": CUSTOMER_ID}
    return TestClient(app, headers={"Authorization": "Bearer t.t.t"})


@pytest.fixture
def anon(db):
    return TestClient(app)


BODY = {
    "title": "Oak desk",
    "amount": "199.99",
    "description": "Solid oak",
    "details": [
        {"title": "Dimensions", "subtitle": "Metric", "paragraph": "120 x 60 cm"},
        {"title": "Care", "paragraph": "Wipe with a damp cloth"},
    ],
}


# --- who can write --------------------------------------------------------


@pytest.mark.parametrize("method,path", [("post", "/products"), ("patch", f"/products/{uuid4()}")])
def test_anonymous_cannot_write(anon, method, path):
    assert getattr(anon, method)(path, json=BODY).status_code in (401, 403)


def test_customer_cannot_create_update_or_manage_images(as_customer):
    pid = uuid4()
    assert as_customer.post("/products", json=BODY).status_code == 403
    assert as_customer.patch(f"/products/{pid}", json={"title": "x"}).status_code == 403
    assert as_customer.post(f"/products/{pid}/images", files={"file": ("a.jpg", JPEG)}).status_code == 403
    assert as_customer.delete(f"/products/{pid}/images/{uuid4()}").status_code == 403


def test_deactivated_super_admin_cannot_write(as_admin, db):
    db.users[ADMIN_ID].banned_until = "2999-01-01T00:00:00Z"
    assert as_admin.post("/products", json=BODY).status_code == 403


# --- create / read --------------------------------------------------------


def test_super_admin_creates_product_with_ordered_details(as_admin):
    r = as_admin.post("/products", json=BODY)
    assert r.status_code == 201
    body = r.json()
    assert body["title"] == "Oak desk" and body["amount"] == "199.99"
    assert [d["title"] for d in body["details"]] == ["Dimensions", "Care"]
    assert body["details"][1]["subtitle"] is None


def test_anyone_can_list_and_view_products_without_a_token(as_admin, anon):
    pid = as_admin.post("/products", json=BODY).json()["id"]
    assert [p["id"] for p in anon.get("/products").json()] == [pid]
    assert anon.get(f"/products/{pid}").json()["title"] == "Oak desk"


def test_unknown_product_is_404(anon):
    assert anon.get(f"/products/{uuid4()}").status_code == 404


def test_malformed_product_id_is_422(anon):
    assert anon.get("/products/not-a-uuid").status_code == 422


@pytest.mark.parametrize(
    "patch",
    [
        {"title": "   "},
        {"title": ""},
        {"amount": "-1"},
        {"amount": "1.999"},
        {"amount": "abc"},
        {"details": [{"title": "t", "paragraph": ""}]},
        {"details": [{"title": "", "paragraph": "p"}]},
        {"details": [{"title": "t", "paragraph": "p"}] * 51},
    ],
)
def test_invalid_input_is_rejected(as_admin, patch):
    assert as_admin.post("/products", json={**BODY, **patch}).status_code == 422


def test_missing_required_fields_rejected(as_admin):
    assert as_admin.post("/products", json={"title": "x"}).status_code == 422


# --- update ---------------------------------------------------------------


def test_partial_update_does_not_touch_other_fields_or_details(as_admin, db):
    pid = as_admin.post("/products", json=BODY).json()["id"]
    r = as_admin.patch(f"/products/{pid}", json={"title": "New"})
    assert r.status_code == 200 and r.json()["title"] == "New"
    assert db.rpc_calls[-1][1]["p"] == {"title": "New"}  # no "details" key => SQL leaves them alone
    assert len(r.json()["details"]) == 2


def test_update_can_replace_details_with_empty_list(as_admin, db):
    pid = as_admin.post("/products", json=BODY).json()["id"]
    as_admin.patch(f"/products/{pid}", json={"details": []})
    assert db.rpc_calls[-1][1]["p"] == {"details": []}


def test_explicit_null_title_is_not_sent_to_sql(as_admin, db):
    pid = as_admin.post("/products", json=BODY).json()["id"]
    as_admin.patch(f"/products/{pid}", json={"title": None, "amount": "5"})
    assert db.rpc_calls[-1][1]["p"] == {"amount": "5"}


def test_update_unknown_product_is_404(as_admin):
    assert as_admin.patch(f"/products/{uuid4()}", json={"title": "x"}).status_code == 404


# --- images ---------------------------------------------------------------


def make_product(as_admin):
    return as_admin.post("/products", json=BODY).json()["id"]


def test_upload_image_stores_file_and_returns_public_url(as_admin, db):
    pid = make_product(as_admin)
    r = as_admin.post(f"/products/{pid}/images", files={"file": ("evil.exe", PNG, "text/plain")})
    assert r.status_code == 201
    assert r.json()["url"].startswith(f"https://x.supabase.co/storage/v1/object/public/product-images/{pid}/")
    assert r.json()["url"].endswith(".png")  # type comes from the bytes, not the filename/header
    assert len(db.files) == 1
    assert as_admin.get(f"/products/{pid}").json()["images"][0]["position"] == 0


def test_multiple_images_get_increasing_positions(as_admin):
    pid = make_product(as_admin)
    for _ in range(3):
        as_admin.post(f"/products/{pid}/images", files={"file": ("a.jpg", JPEG)})
    assert [i["position"] for i in as_admin.get(f"/products/{pid}").json()["images"]] == [0, 1, 2]


def test_non_image_content_is_rejected_even_with_image_header(as_admin, db):
    pid = make_product(as_admin)
    r = as_admin.post(f"/products/{pid}/images", files={"file": ("a.jpg", b"<script>alert(1)</script>", "image/jpeg")})
    assert r.status_code == 415 and not db.files


def test_oversize_image_is_rejected(as_admin, db):
    pid = make_product(as_admin)
    big = JPEG + b"0" * service.MAX_IMAGE_BYTES
    assert as_admin.post(f"/products/{pid}/images", files={"file": ("a.jpg", big)}).status_code == 413
    assert not db.files


def test_image_limit_per_product(as_admin, monkeypatch):
    monkeypatch.setattr(service, "MAX_IMAGES_PER_PRODUCT", 2)
    pid = make_product(as_admin)
    codes = [as_admin.post(f"/products/{pid}/images", files={"file": ("a.jpg", JPEG)}).status_code for _ in range(3)]
    assert codes == [201, 201, 409]


def test_upload_to_unknown_product_is_404_and_stores_nothing(as_admin, db):
    assert as_admin.post(f"/products/{uuid4()}/images", files={"file": ("a.jpg", JPEG)}).status_code == 404
    assert not db.files


def test_failed_db_insert_removes_uploaded_file(as_admin, db):
    pid = make_product(as_admin)
    db.fail_insert = True
    with pytest.raises(RuntimeError):
        as_admin.post(f"/products/{pid}/images", files={"file": ("a.jpg", JPEG)})
    assert not db.files


def test_delete_image_removes_row_and_file(as_admin, db):
    pid = make_product(as_admin)
    image_id = as_admin.post(f"/products/{pid}/images", files={"file": ("a.jpg", JPEG)}).json()["id"]
    assert as_admin.delete(f"/products/{pid}/images/{image_id}").status_code == 204
    assert not db.files and not as_admin.get(f"/products/{pid}").json()["images"]


def test_cannot_delete_image_through_another_product(as_admin, db):
    a, b = make_product(as_admin), make_product(as_admin)
    image_id = as_admin.post(f"/products/{a}/images", files={"file": ("a.jpg", JPEG)}).json()["id"]
    assert as_admin.delete(f"/products/{b}/images/{image_id}").status_code == 404
    assert len(db.files) == 1


def test_detect_image_type():
    assert service.detect_image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ("image/webp", "webp")
    assert service.detect_image_type(b"GIF89a") is None
