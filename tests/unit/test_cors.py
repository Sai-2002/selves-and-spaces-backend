"""CORS: a browser SPA may call the API only from origins we list."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import DEV_CORS_ORIGINS, Settings
from app.main import app

BASE = dict(supabase_url="https://x.supabase.co", supabase_secret_key="k")
LIVE = dict(
    razorpay_mode="live",
    razorpay_key_id="rzp_live_a",
    razorpay_key_secret="s",
    razorpay_webhook_secret="w",
    environment="production",
)


def preflight(origin: str, path: str = "/checkout", method: str = "POST"):
    return TestClient(app).options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )


def test_listed_origin_is_allowed_to_send_authorization_header():
    r = preflight(DEV_CORS_ORIGINS[0])
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == DEV_CORS_ORIGINS[0]
    assert "authorization" in r.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in r.headers  # bearer tokens, never cookies


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
def test_all_api_methods_are_allowed(method):
    assert method in preflight(DEV_CORS_ORIGINS[0], method=method).headers["access-control-allow-methods"]


def test_unlisted_origin_gets_no_cors_headers():
    r = preflight("https://evil.example")
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers


def test_simple_request_from_unlisted_origin_gets_no_allow_header():
    # /me answers 401 without touching the database; the point is the missing CORS header.
    r = TestClient(app).get("/me", headers={"Origin": "https://evil.example"})
    assert r.status_code == 401
    assert "access-control-allow-origin" not in r.headers

    ok = TestClient(app).get("/me", headers={"Origin": DEV_CORS_ORIGINS[0]})
    assert ok.headers["access-control-allow-origin"] == DEV_CORS_ORIGINS[0]


def test_origins_parse_from_a_comma_separated_string():
    s = Settings(**BASE, cors_origins=" https://shop.example.com/ , https://admin.example.com ")
    assert s.cors_origin_list == ["https://shop.example.com", "https://admin.example.com"]


def test_empty_setting_means_local_dev_servers_in_development():
    assert Settings(**BASE).cors_origin_list == DEV_CORS_ORIGINS


def test_production_requires_explicit_origins():
    with pytest.raises(ValidationError, match="CORS_ORIGINS must list"):
        Settings(**BASE, **LIVE)


def test_production_rejects_wildcard():
    with pytest.raises(ValidationError, match="must not contain"):
        Settings(**BASE, **LIVE, cors_origins="*")
    with pytest.raises(ValidationError, match="must not contain"):
        Settings(**BASE, **LIVE, cors_origins="https://a.example.com,*")


def test_production_accepts_a_real_origin():
    s = Settings(**BASE, **LIVE, cors_origins="https://shop.example.com")
    assert s.cors_origin_list == ["https://shop.example.com"]
