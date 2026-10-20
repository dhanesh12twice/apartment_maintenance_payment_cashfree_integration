# Cashfree Checkout API for apartment maintenance

FastAPI sandbox starter for creating Cashfree hosted-checkout orders and recording payment status from signed webhooks.

## Files

- `app/main.py`: create-order API and Cashfree webhook endpoint.
- `checkout-example.html`: small browser example that opens Cashfree Checkout.
- SQLite stores created order metadata and processed webhook IDs in `data/payments.db`.

## Setup

From this folder:

```sh
cp .env.example .env
# Add the Cashfree sandbox App ID and Secret Key from the Cashfree dashboard to .env
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Open `http://localhost:8000/checkout-example.html`. The example amount is a sandbox placeholder. Cashfree API credentials remain on the backend and must never be included in browser code.

Create a payment order with `POST /api/payments`:

```json
{
  "member_id": "unit-101",
  "billing_period": "2026-10",
  "amount": 1000.00,
  "customer_name": "Apartment Member",
  "customer_email": "member@example.com",
  "customer_phone": "9999999999"
}
```

The API returns `order_id`, `payment_session_id`, and `environment`. Pass `payment_session_id` to Cashfree's JavaScript SDK; the included HTML demonstrates this. Checkout return/redirect is not proof of payment: the app marks an order paid only after a valid Cashfree success webhook.

## Webhook setup

Expose `POST /api/webhooks/cashfree` over HTTPS and register that URL in **Cashfree Dashboard > Payment Gateway > Developers > Webhooks**. Enable payment success, failed, and user-dropped events. Keep `CASHFREE_SECRET_KEY` equal to the key used for that environment. The handler verifies the signature over the exact raw body, deduplicates webhook deliveries, and updates local order status.

For local testing, expose the server with a secure development tunnel and register the tunnel URL in the Cashfree sandbox dashboard.

## Important before production

- **Do not trust an amount submitted by the browser.** This starter accepts an amount to make sandbox checkout demonstrable. In your real apartment app, authenticate the member and load the unit's outstanding maintenance dues from your trusted database on the server; derive the amount there and prevent overpayment/duplicate collection for a billing period.
- Add your app's authentication and authorization to order creation, and only allow members to pay their own dues. Configure `CORS_ORIGINS` with your exact deployed frontend origin(s).
- Replace SQLite as appropriate for your deployment and persist a unique maintenance invoice/payment allocation in the same transaction as order creation.
- Set `CASHFREE_ENV=production` only after Cashfree enables the live account, you set production credentials, whitelist your live domain, and test the complete live configuration. Don't expose the secret in logs, frontend code, or source control.
- Reconcile payments against Cashfree's order/payment status API and settlements. Treat webhooks as the primary timely notification; do not treat the browser return page as confirmation.

API reference: [Cashfree Create Order](https://www.cashfree.com/docs/api-reference/payments/latest/orders/create-order), [hosted web checkout](https://www.cashfree.com/docs/payments/online/web/redirect), and [webhook signature verification](https://www.cashfree.com/docs/payments/online/webhooks/signature-verification).
