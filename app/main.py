import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DATABASE_PATH", "data/payments.db"))
API_BASE_URLS = {
    "sandbox": "https://sandbox.cashfree.com/pg",
    "production": "https://api.cashfree.com/pg",
}
ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,45}$")
CustomerId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
PhoneNumber = Annotated[str, StringConstraints(pattern=r"^[0-9]{10,15}$")]


def initialize_database() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                order_id TEXT PRIMARY KEY,
                member_id TEXT NOT NULL,
                billing_period TEXT NOT NULL,
                amount TEXT NOT NULL,
                status TEXT NOT NULL,
                cf_order_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_webhooks (
                event_key TEXT PRIMARY KEY,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title="Apartment Maintenance Payments API", lifespan=lifespan)
cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class CreatePaymentRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    member_id: CustomerId
    billing_period: Annotated[str, StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
    amount: Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]
    customer_name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    customer_email: Annotated[str, StringConstraints(min_length=3, max_length=254)]
    customer_phone: PhoneNumber


class CreatePaymentResponse(BaseModel):
    order_id: str
    payment_session_id: str
    environment: str


def get_cashfree_config() -> tuple[str, str, str, str]:
    app_id = os.getenv("CASHFREE_APP_ID", "").strip()
    secret_key = os.getenv("CASHFREE_SECRET_KEY", "").strip()
    environment = os.getenv("CASHFREE_ENV", "sandbox").strip().lower()
    api_version = os.getenv("CASHFREE_API_VERSION", "2026-01-01").strip()
    if not app_id or not secret_key:
        raise HTTPException(status_code=503, detail="Cashfree API credentials are not configured")
    if environment not in API_BASE_URLS:
        raise HTTPException(status_code=503, detail="CASHFREE_ENV must be sandbox or production")
    return app_id, secret_key, environment, api_version


def make_return_url(base_url: str, order_id: str) -> str:
    parts = urlsplit(base_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("order_id", order_id))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def verify_webhook_signature(raw_body: bytes, timestamp: str, signature: str, secret_key: str) -> bool:
    signed_payload = timestamp.encode("utf-8") + b"." + raw_body
    expected = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), signed_payload, hashlib.sha256).digest()
    ).decode("ascii")
    return hmac.compare_digest(expected, signature)


def apply_webhook(raw_body: bytes, idempotency_key: str | None) -> bool:
    event_key = idempotency_key or hashlib.sha256(raw_body).hexdigest()
    payload = json.loads(raw_body)
    if not isinstance(payload, dict):
        raise ValueError("Webhook payload must be a JSON object")
    event_type = payload.get("type")
    data = payload.get("data") or {}
    order = data.get("order") or {}
    payment = data.get("payment") or {}
    order_id = order.get("order_id")
    payment_status = payment.get("payment_status")

    with sqlite3.connect(DB_PATH) as connection:
        try:
            connection.execute(
                "INSERT INTO processed_webhooks (event_key) VALUES (?)", (event_key,)
            )
        except sqlite3.IntegrityError:
            return False

        if isinstance(order_id, str) and ORDER_ID_PATTERN.fullmatch(order_id):
            status = None
            if event_type == "PAYMENT_SUCCESS_WEBHOOK" and payment_status == "SUCCESS":
                status = "PAID"
            elif event_type == "PAYMENT_FAILED_WEBHOOK":
                status = "FAILED"
            elif event_type == "PAYMENT_USER_DROPPED_WEBHOOK":
                status = "USER_DROPPED"

            if status:
                connection.execute(
                    """
                    UPDATE payments
                    SET status = CASE WHEN status = 'PAID' THEN status ELSE ? END
                    WHERE order_id = ?
                    """,
                    (status, order_id),
                )
    return True


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/checkout-example.html", include_in_schema=False)
async def checkout_example() -> FileResponse:
    return FileResponse(Path(__file__).resolve().parents[1] / "checkout-example.html")


@app.post("/api/payments", response_model=CreatePaymentResponse)
async def create_payment(body: CreatePaymentRequest) -> CreatePaymentResponse:
    app_id, secret_key, environment, api_version = get_cashfree_config()
    amount = body.amount.quantize(Decimal("0.01"))
    order_id = f"maint_{uuid.uuid4().hex}"
    return_url = make_return_url(
        os.getenv("FRONTEND_RETURN_URL", "http://localhost:8000/checkout-example.html"),
        order_id,
    )
    order_payload = {
        "order_id": order_id,
        "order_amount": float(amount),
        "order_currency": "INR",
        "customer_details": {
            "customer_id": body.member_id,
            "customer_name": body.customer_name,
            "customer_email": body.customer_email,
            "customer_phone": body.customer_phone,
        },
        "order_meta": {"return_url": return_url},
        "order_note": f"Apartment maintenance {body.billing_period}",
    }
    headers = {
        "x-client-id": app_id,
        "x-client-secret": secret_key,
        "x-api-version": api_version,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{API_BASE_URLS[environment]}/orders",
                headers=headers,
                json=order_payload,
            )
            response.raise_for_status()
            cashfree_order = response.json()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Cashfree order creation failed with HTTP %s",
            exc.response.status_code,
        )
        raise HTTPException(
            status_code=502, detail="Cashfree could not create the payment order"
        ) from exc
    except (httpx.RequestError, ValueError) as exc:
        logger.exception("Unable to create Cashfree payment order")
        raise HTTPException(
            status_code=502, detail="Could not connect to Cashfree or parse its response"
        ) from exc

    payment_session_id = cashfree_order.get("payment_session_id")
    if not isinstance(payment_session_id, str) or not payment_session_id:
        logger.error("Cashfree order response did not include payment_session_id")
        raise HTTPException(status_code=502, detail="Cashfree returned an invalid order response")

    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            INSERT INTO payments (order_id, member_id, billing_period, amount, status, cf_order_id)
            VALUES (?, ?, ?, ?, 'PENDING', ?)
            """,
            (
                order_id,
                body.member_id,
                body.billing_period,
                f"{amount:.2f}",
                str(cashfree_order.get("cf_order_id", "")),
            ),
        )

    return CreatePaymentResponse(
        order_id=order_id,
        payment_session_id=payment_session_id,
        environment=environment,
    )


@app.post("/api/webhooks/cashfree")
async def cashfree_webhook(
    request: Request,
    signature: Annotated[str | None, Header(alias="x-webhook-signature")] = None,
    timestamp: Annotated[str | None, Header(alias="x-webhook-timestamp")] = None,
    idempotency_key: Annotated[
        str | None, Header(alias="x-idempotency-key")
    ] = None,
) -> dict[str, str]:
    _, secret_key, _, _ = get_cashfree_config()
    raw_body = await request.body()
    if not signature or not timestamp or not verify_webhook_signature(
        raw_body, timestamp, signature, secret_key
    ):
        raise HTTPException(status_code=401, detail="Invalid Cashfree webhook signature")

    try:
        processed = apply_webhook(raw_body, idempotency_key)
    except (json.JSONDecodeError, ValueError, sqlite3.Error) as exc:
        logger.exception("Could not process Cashfree webhook")
        raise HTTPException(status_code=400, detail="Invalid or unprocessable webhook") from exc

    return {"status": "processed" if processed else "duplicate"}
