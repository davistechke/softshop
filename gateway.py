"""
Makamesco Nexus Pay (https://makamescopay.com) - M-Pesa STK Push adapter.
All gateway-specific code lives here; every credential comes from .env.

Request format (same as the working NexaBinary / ProxyGet integrations):
    POST MAKAMESCO_STKPUSH_URL          (https://makamescopay.com/api/payments/stkpush)
    Header  X-API-Key: <MAKAMESCO_API_KEY>
    Body    {"phoneNumber", "amount", "accountReference", "transactionDesc"}

`accountReference` is our order token; Makamesco echoes it back in the webhook,
which is how the order is found. Webhook/status payloads aren't publicly
documented, so they are parsed defensively. Every gateway response and webhook
is logged to data/gateway.log - check it after the first real payment.
"""
import os, json, hmac, hashlib, requests
from datetime import datetime


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


BASE = _env("MAKAMESCO_BASE_URL", "https://makamescopay.com").rstrip("/")
STK_URL = _env("MAKAMESCO_STKPUSH_URL") or BASE + "/api/payments/stkpush"
STATUS_URL = _env("MAKAMESCO_STATUS_URL")            # optional, e.g. .../api/payments/status/{reference}
API_KEY = _env("MAKAMESCO_API_KEY")
CALLBACK_TOKEN = _env("MAKAMESCO_CALLBACK_TOKEN")
WEBHOOK_SECRET = _env("MAKAMESCO_WEBHOOK_SECRET")
CALLBACK_URL = _env("MAKAMESCO_CALLBACK_URL")        # optional: sent as callbackUrl if set
SIMULATE = _env("MAKAMESCO_SIMULATE", "0") == "1"
LOG = os.path.join(os.environ.get("DATA_DIR", "data"), "gateway.log")

PAID = {"SUCCESS", "SUCCESSFUL", "COMPLETE", "COMPLETED", "PAID", "CONFIRMED", "OK"}
CANCELLED = {"CANCELLED", "CANCELED", "USER_CANCELLED", "REQUEST_CANCELLED_BY_USER"}
FAILED = {"FAILED", "FAILURE", "ERROR", "DECLINED", "REJECTED", "TIMEOUT",
          "TIMED_OUT", "EXPIRED", "INSUFFICIENT_FUNDS"} | CANCELLED
SIGNATURE_HEADERS = ("X-Signature", "X-Webhook-Signature", "X-Nexus-Signature",
                     "X-Makamesco-Signature", "X-Hub-Signature-256")


class GatewayError(Exception):
    pass


def is_configured():
    return SIMULATE or bool(STK_URL and API_KEY)


def log(kind, obj):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{datetime.utcnow().isoformat()} {kind} {json.dumps(obj, default=str)[:1500]}\n")
    except Exception:
        pass


def _headers():
    return {"X-API-Key": API_KEY, "Content-Type": "application/json", "Accept": "application/json"}


def _first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_status(value):
    v = str(value if value is not None else "").strip().upper().replace(" ", "_")
    if v in PAID:
        return "paid"
    if v in FAILED:
        return "failed"
    return "pending"


def initiate(*, token, phone, amount, description):
    """Start an STK push. Returns the gateway's payment id (falls back to our own token)."""
    if SIMULATE:
        return f"SIM-{token}"
    if not (STK_URL and API_KEY):
        raise GatewayError("Payment gateway is not configured.")
    payload = {
        "phoneNumber": phone,
        "amount": int(amount),
        "accountReference": token,
        "transactionDesc": description,
    }
    if CALLBACK_URL:  # normally the webhook is registered in the Makamesco dashboard instead
        payload["callbackUrl"] = CALLBACK_URL
    try:
        r = requests.post(STK_URL, json=payload, headers=_headers(), timeout=30)
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:500]}
    except requests.exceptions.Timeout:
        log("initiate_error", "timeout")
        raise GatewayError("Payment gateway timed out. Please try again.")
    except requests.RequestException as e:
        log("initiate_error", str(e))
        raise GatewayError("Could not reach the payment gateway. Please try again.")
    log("initiate_response", {"status": r.status_code, "body": data})
    if not isinstance(data, dict):
        data = {}
    d = data.get("data", data) if isinstance(data.get("data", data), dict) else data
    explicit_fail = (data.get("success") is False or data.get("ok") is False
                     or d.get("success") is False
                     or str(d.get("status", "")).upper() in FAILED)
    if r.status_code not in (200, 201, 202) or explicit_fail:
        raise GatewayError(str(d.get("message") or d.get("error") or d.get("detail")
                               or data.get("message") or data.get("error")
                               or "Payment request was rejected."))
    return str(_first(d, "checkoutRequestId", "CheckoutRequestID", "checkout_request_id",
                      "transactionId", "transaction_id", "reference", "id") or token)


def parse_payload(data):
    """Normalise a webhook / status body -> (our_token, gateway_ref, status, receipt, amount).
    status is paid | failed | pending. Handles flat JSON, {"data": {...}} and the
    Safaricom-style {"Body": {"stkCallback": {...}}}."""
    if not isinstance(data, dict):
        return None, None, "pending", None, None
    res = data
    for wrapper in ("data", "response", "payment", "transaction"):
        if isinstance(res.get(wrapper), dict):
            res = res[wrapper]
            break

    stk = (data.get("Body") or {}).get("stkCallback") if isinstance(data.get("Body"), dict) else None
    if isinstance(stk, dict):
        items = {i.get("Name"): i.get("Value") for i in (stk.get("CallbackMetadata") or {}).get("Item", [])
                 if isinstance(i, dict)}
        status = "paid" if str(stk.get("ResultCode")) == "0" else "failed"
        return (None, stk.get("CheckoutRequestID"), status,
                items.get("MpesaReceiptNumber"), _to_float(items.get("Amount")))

    code = _first(res, "resultCode", "ResultCode", "result_code")
    raw_status = _first(res, "status", "Status", "paymentStatus", "state")
    status = normalize_status(raw_status)
    if raw_status is None and code is not None:
        status = "paid" if str(code).strip() == "0" else "failed"
    elif status == "pending" and code is not None and str(code).strip() != "":
        status = "paid" if str(code).strip() == "0" else "failed"

    token = _first(res, "accountReference", "AccountReference", "account_reference",
                   "externalReference", "external_reference", "ExternalReference",
                   "reference", "merchantReference", "order_id") \
        or _first(data, "accountReference", "external_reference", "reference")
    ref = _first(res, "checkoutRequestId", "CheckoutRequestID", "checkout_request_id",
                 "transactionId", "transaction_id", "id")
    receipt = _first(res, "receipt", "mpesaReceipt", "mpesa_receipt", "MpesaReceiptNumber",
                     "mpesa_receipt_number", "receiptNumber")
    amount = _to_float(_first(res, "amount", "Amount"))
    return (str(token) if token else None, str(ref) if ref else None, status, receipt, amount)


def parse_webhook(data):
    log("webhook", data)
    return parse_payload(data)


def check(reference):
    """Optional status poll. Returns (status, receipt, amount). Only used if MAKAMESCO_STATUS_URL is set."""
    if SIMULATE or not STATUS_URL:
        return "pending", None, None
    url = STATUS_URL.replace("{reference}", reference)
    try:
        r = requests.get(url, params=None if "{reference}" in STATUS_URL else {"reference": reference},
                         headers={"X-API-Key": API_KEY, "Accept": "application/json"}, timeout=20)
        if r.status_code != 200:
            log("status_error", {"status": r.status_code, "body": r.text[:300]})
            return "pending", None, None
        data = r.json()
    except Exception:
        return "pending", None, None
    log("status_response", data)
    _tok, _ref, status, receipt, amount = parse_payload(data)
    return status, receipt, amount


def webhook_ok(request):
    """Authenticate an incoming webhook. Fails CLOSED. Passes if ANY check passes:
      1. ?token= / ?secret= (or X-Callback-Token / X-Webhook-Secret header) equals
         MAKAMESCO_CALLBACK_TOKEN or MAKAMESCO_WEBHOOK_SECRET
      2. HMAC-SHA256(raw body, MAKAMESCO_WEBHOOK_SECRET) in a signature header
    """
    valid = [v for v in (CALLBACK_TOKEN, WEBHOOK_SECRET) if v]
    given = [request.args.get("token"), request.args.get("secret"),
             request.headers.get("X-Callback-Token"), request.headers.get("X-Webhook-Secret")]
    if any(g and hmac.compare_digest(g, v) for g in given for v in valid):
        return True
    if WEBHOOK_SECRET:
        expected = hmac.new(WEBHOOK_SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
        for h in SIGNATURE_HEADERS:
            sig = (request.headers.get(h) or "").strip()
            if sig.lower().startswith("sha256="):
                sig = sig[7:]
            if sig and hmac.compare_digest(sig.lower(), expected):
                return True
    return False
