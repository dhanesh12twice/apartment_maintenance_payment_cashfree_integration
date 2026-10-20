import base64
import hashlib
import hmac
import json
import sqlite3

from fastapi.testclient import TestClient

from app import main


def sign(raw_body: bytes, timestamp: str, secret: str) -> str:
    signed = timestamp.encode() + b"." + raw_body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def test_webhook_rejects_invalid_signature(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "payments.db")
    monkeypatch.setenv("CASHFREE_APP_ID", "test-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "test-secret")
    raw_body = b'{"type":"PAYMENT_SUCCESS_WEBHOOK"}'

    with TestClient(main.app) as client:
        response = client.post(
            "/api/webhooks/cashfree",
            content=raw_body,
            headers={
                "x-webhook-timestamp": "123456",
                "x-webhook-signature": "invalid",
            },
        )

    assert response.status_code == 401


def test_valid_success_webhook_marks_payment_paid_once(monkeypatch, tmp_path):
    db_path = tmp_path / "payments.db"
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setenv("CASHFREE_APP_ID", "test-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "test-secret")
    main.initialize_database()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO payments (order_id, member_id, billing_period, amount, status)
            VALUES ('maint_test123', 'unit-101', '2026-10', '1000.00', 'PENDING')
            """
        )

    raw_body = json.dumps(
        {
            "type": "PAYMENT_SUCCESS_WEBHOOK",
            "data": {
                "order": {"order_id": "maint_test123"},
                "payment": {"payment_status": "SUCCESS"},
            },
        },
        separators=(",", ":"),
    ).encode()
    timestamp = "123456"
    headers = {
        "x-webhook-timestamp": timestamp,
        "x-webhook-signature": sign(raw_body, timestamp, "test-secret"),
        "x-idempotency-key": "event-1",
        "content-type": "application/json",
    }

    with TestClient(main.app) as client:
        first = client.post("/api/webhooks/cashfree", content=raw_body, headers=headers)
        second = client.post("/api/webhooks/cashfree", content=raw_body, headers=headers)

    assert first.status_code == 200
    assert first.json() == {"status": "processed"}
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}
    with sqlite3.connect(db_path) as connection:
        status = connection.execute(
            "SELECT status FROM payments WHERE order_id = 'maint_test123'"
        ).fetchone()[0]
    assert status == "PAID"


def test_create_payment_creates_cashfree_order(monkeypatch, tmp_path):
    db_path = tmp_path / "payments.db"
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setenv("CASHFREE_APP_ID", "test-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "test-secret")
    monkeypatch.setenv("CASHFREE_ENV", "sandbox")
    main.initialize_database()
    sent: dict = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"payment_session_id": "session_test", "cf_order_id": 12345}

    class FakeClient:
        def __init__(self, timeout):
            assert timeout == 20.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers, json):
            sent.update(url=url, headers=headers, payload=json)
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)
    request = {
        "member_id": "unit-101",
        "billing_period": "2026-10",
        "amount": 1000.0,
        "customer_name": "Test Member",
        "customer_email": "member@example.com",
        "customer_phone": "9999999999",
    }

    with TestClient(main.app) as client:
        response = client.post("/api/payments", json=request)
        example_page = client.get("/checkout-example.html")

    assert response.status_code == 200
    result = response.json()
    assert result["payment_session_id"] == "session_test"
    assert result["environment"] == "sandbox"
    assert sent["url"] == "https://sandbox.cashfree.com/pg/orders"
    assert sent["headers"]["x-api-version"] == "2026-01-01"
    assert sent["payload"]["order_amount"] == 1000.0
    assert sent["payload"]["order_meta"]["return_url"].endswith(
        f"order_id={result['order_id']}"
    )
    assert example_page.status_code == 200
    with sqlite3.connect(db_path) as connection:
        saved = connection.execute(
            "SELECT amount, status FROM payments WHERE order_id = ?",
            (result["order_id"],),
        ).fetchone()
    assert saved == ("1000.00", "PENDING")
