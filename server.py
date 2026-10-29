#!/usr/bin/env python3
"""Local Stepwhere Sales Desk server with Shopify and Amazon order sync."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "index.html"
STATE_FILE = ROOT / ".sync_state.json"
IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_LOOKBACK_DAYS = 30
MAX_LOOKBACK_DAYS = 365
# Re-read a small window before the saved cursor so orders updated at the
# same moment as the previous sync are not missed.
CURSOR_OVERLAP = timedelta(minutes=5)
# Amazon's getOrderItems allows a burst of about 30 calls, refilling at 0.5/s.
# Staying below the burst keeps each sync inside the rate limit; remaining
# orders are picked up on the next sync.
AMAZON_ITEM_CALLS_PER_SYNC = 25
SYNC_LOCK = threading.Lock()


def load_env():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def read_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_state(data):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def lookback_days():
    try:
        days = int(os.getenv("SYNC_LOOKBACK_DAYS") or DEFAULT_LOOKBACK_DAYS)
    except ValueError:
        days = DEFAULT_LOOKBACK_DAYS
    return max(1, min(MAX_LOOKBACK_DAYS, days))


def iso_z(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def sync_start(state, channel, now):
    """Return the 'updated after' time for a channel: saved cursor or first-sync lookback."""
    cursor = parse_iso((state.get("cursors") or {}).get(channel))
    if cursor:
        return cursor - CURSOR_OVERLAP
    return now - timedelta(days=lookback_days())


def ist_date(value):
    """Convert a marketplace UTC timestamp to the Indian calendar date (YYYY-MM-DD)."""
    dt = parse_iso(value)
    if not dt:
        return datetime.now(IST).date().isoformat()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).date().isoformat()


def request_json(url, *, method="GET", headers=None, data=None, timeout=30):
    body = None
    req_headers = {"Accept": "application/json", "User-Agent": "StepwhereSalesDesk/1.0 Python"}
    if headers:
        req_headers.update(headers)
    if data is not None:
        if isinstance(data, dict):
            body = json.dumps(data).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        else:
            body = data
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"API returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Connection failed: {exc.reason}") from exc


def amount(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def reconcile(subtotal, total):
    if total >= subtotal:
        return round(total - subtotal, 2), 0.0
    return 0.0, round(subtotal - total, 2)


SHOPIFY_PAGE_SIZE = 100


def sync_shopify(updated_after, now):
    """Return (orders, next_cursor). Orders are fetched oldest-update first so the
    cursor can advance page by page across syncs."""
    store = os.getenv("SHOPIFY_STORE", "").replace("https://", "").rstrip("/")
    token = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
    version = os.getenv("SHOPIFY_API_VERSION", "2026-07")
    if not store or not token:
        raise RuntimeError("Shopify setup required")
    query = """
    query StepwhereOrders($query: String!, $first: Int!) {
      orders(first: $first, sortKey: UPDATED_AT, query: $query) {
        nodes {
          id name createdAt updatedAt displayFinancialStatus displayFulfillmentStatus email phone
          currentTotalPriceSet { shopMoney { amount currencyCode } }
          currentSubtotalPriceSet { shopMoney { amount currencyCode } }
          customer { displayName email phone }
          shippingAddress { name phone address1 address2 city province zip country }
          lineItems(first: 100) {
            nodes { title variantTitle quantity discountedUnitPriceAfterAllDiscountsSet { shopMoney { amount } } }
          }
        }
      }
    }
    """
    payload = request_json(
        f"https://{store}/admin/api/{version}/graphql.json",
        method="POST",
        headers={"X-Shopify-Access-Token": token},
        data={"query": query, "variables": {"query": f"updated_at:>='{iso_z(updated_after)}'", "first": SHOPIFY_PAGE_SIZE}},
    )
    if payload.get("errors"):
        raise RuntimeError("Shopify: " + payload["errors"][0].get("message", "GraphQL error"))
    nodes = (payload.get("data") or {}).get("orders", {}).get("nodes", [])
    result = []
    for order in nodes:
        address = order.get("shippingAddress") or {}
        customer = order.get("customer") or {}
        items = []
        for item in order.get("lineItems", {}).get("nodes", []):
            variant = item.get("variantTitle")
            title = item.get("title") or "Shopify item"
            if variant and variant != "Default Title":
                title += f" — {variant}"
            price = amount(((item.get("discountedUnitPriceAfterAllDiscountsSet") or {}).get("shopMoney") or {}).get("amount"))
            items.append({"name": title, "qty": int(item.get("quantity") or 1), "price": price})
        subtotal = amount(((order.get("currentSubtotalPriceSet") or {}).get("shopMoney") or {}).get("amount"))
        total = amount(((order.get("currentTotalPriceSet") or {}).get("shopMoney") or {}).get("amount"))
        shipping, discount = reconcile(subtotal, total)
        fulfillment = (order.get("displayFulfillmentStatus") or "").upper()
        financial = (order.get("displayFinancialStatus") or "").upper()
        status = "delivered" if fulfillment == "FULFILLED" else "processing" if fulfillment in {"PARTIALLY_FULFILLED", "IN_PROGRESS"} else "confirmed"
        if financial in {"VOIDED", "REFUNDED"}:
            status = "cancelled"
        contact = address.get("name") or customer.get("displayName") or "Shopify customer"
        number = str(order.get("name") or order.get("id", "")).lstrip("#")
        result.append({
            "id": "SHOP-" + number,
            "invoice": "SW-" + number,
            "customer": contact,
            "contact": contact,
            "phone": address.get("phone") or order.get("phone") or customer.get("phone") or "",
            "email": order.get("email") or customer.get("email") or "",
            "address": ", ".join(str(address.get(k)) for k in ("address1", "address2", "city", "province", "zip", "country") if address.get(k)),
            "gstin": "", "channel": "Shopify", "date": ist_date(order.get("createdAt")),
            "status": status, "payment": "paid" if financial in {"PAID", "PARTIALLY_REFUNDED"} else "pending",
            "items": items or [{"name": "Shopify order", "qty": 1, "price": subtotal}],
            "shipping": shipping, "discount": discount, "taxRate": 0,
            "notes": "Synced automatically from Shopify"
        })
    if len(nodes) >= SHOPIFY_PAGE_SIZE:
        # More orders are waiting; continue from the last one on the next sync.
        next_cursor = parse_iso(nodes[-1].get("updatedAt")) or now
    else:
        next_cursor = now
    return result, next_cursor


def amazon_access_token():
    required = ["AMAZON_LWA_CLIENT_ID", "AMAZON_LWA_CLIENT_SECRET", "AMAZON_REFRESH_TOKEN"]
    if any(not os.getenv(k) for k in required):
        raise RuntimeError("Amazon setup required")
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": os.environ["AMAZON_REFRESH_TOKEN"],
        "client_id": os.environ["AMAZON_LWA_CLIENT_ID"],
        "client_secret": os.environ["AMAZON_LWA_CLIENT_SECRET"],
    }).encode("utf-8")
    result = request_json("https://api.amazon.com/auth/o2/token", method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"}, data=body)
    return result["access_token"]


def amazon_get(endpoint, token, params=None):
    base = os.getenv("AMAZON_SP_API_ENDPOINT", "https://sellingpartnerapi-eu.amazon.com").rstrip("/")
    url = base + endpoint
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    return request_json(url, headers={"x-amz-access-token": token})


AMAZON_STATUS_MAP = {"Pending": "confirmed", "PendingAvailability": "confirmed", "Unshipped": "confirmed", "PartiallyShipped": "processing", "Shipped": "shipped", "Canceled": "cancelled", "Unfulfillable": "cancelled", "InvoiceUnconfirmed": "confirmed"}


def sync_amazon(updated_after, now):
    """Return (orders, next_cursor). Uses LastUpdatedAfter so each sync only reads
    changed orders, follows NextToken pages, and limits order-item calls per sync."""
    token = amazon_access_token()
    marketplace = os.getenv("AMAZON_MARKETPLACE_ID", "A21TJRUUN4KGV")
    raw_orders = []
    params = {"MarketplaceIds": marketplace, "LastUpdatedAfter": iso_z(updated_after), "MaxResultsPerPage": 100}
    for _ in range(10):
        data = amazon_get("/orders/v0/orders", token, params)
        payload = data.get("payload") or {}
        raw_orders.extend(payload.get("Orders") or [])
        next_token = payload.get("NextToken")
        if not next_token:
            break
        params = {"MarketplaceIds": marketplace, "NextToken": next_token}
    raw_orders.sort(key=lambda o: o.get("LastUpdateDate") or "")

    result = []
    next_cursor = now
    item_calls = 0
    for order in raw_orders:
        order_id = order.get("AmazonOrderId", "")
        if item_calls >= AMAZON_ITEM_CALLS_PER_SYNC:
            # Stop here; the next sync resumes from this order's update time.
            next_cursor = parse_iso(order.get("LastUpdateDate")) or updated_after
            break
        item_calls += 1
        items_pending = False
        try:
            item_data = amazon_get(f"/orders/v0/orders/{urllib.parse.quote(order_id)}/orderItems", token)
            raw_items = (item_data.get("payload") or {}).get("OrderItems", [])
        except RuntimeError:
            # Usually throttling. Resume from this order next sync so its items are fetched again.
            raw_items = []
            items_pending = True
            next_cursor = parse_iso(order.get("LastUpdateDate")) or updated_after
        items = []
        for item in raw_items:
            qty = int(item.get("QuantityOrdered") or 1)
            total_price = amount((item.get("ItemPrice") or {}).get("Amount"))
            items.append({"name": item.get("Title") or item.get("SellerSKU") or "Amazon item", "qty": qty, "price": round(total_price / qty, 2) if qty else total_price})
        order_total = amount((order.get("OrderTotal") or {}).get("Amount"))
        subtotal = sum(i["qty"] * i["price"] for i in items)
        shipping, discount = reconcile(subtotal, order_total)
        entry = {
            "id": "AMZ-" + order_id, "invoice": "SW-AMZ-" + order_id[-8:],
            "customer": "Amazon customer", "contact": "Amazon customer", "phone": "", "email": "", "address": "Available in Amazon Seller Central",
            "gstin": "", "channel": "Amazon", "date": ist_date(order.get("PurchaseDate")),
            "status": AMAZON_STATUS_MAP.get(order.get("OrderStatus"), "processing"),
            "payment": "pending" if order.get("OrderStatus") in {"Pending", "PendingAvailability"} else "paid",
            "items": items or [{"name": "Amazon order", "qty": 1, "price": order_total}],
            "shipping": shipping, "discount": discount, "taxRate": 0,
            "notes": "Synced automatically from Amazon India. Buyer details require approved restricted access."
        }
        if items_pending:
            # Tells the browser to keep any item lines it already has for this order.
            entry["itemsPending"] = True
        result.append(entry)
        if items_pending:
            break
    return result, next_cursor


def credentials_present(channel):
    if channel == "shopify":
        return bool(os.getenv("SHOPIFY_STORE") and os.getenv("SHOPIFY_ACCESS_TOKEN"))
    return bool(os.getenv("AMAZON_LWA_CLIENT_ID") and os.getenv("AMAZON_LWA_CLIENT_SECRET") and os.getenv("AMAZON_REFRESH_TOKEN"))


def run_sync(channel):
    with SYNC_LOCK:
        state = read_state()
        cursors = dict(state.get("cursors") or {})
        orders, errors = [], []
        for name, label, fn in (("shopify", "Shopify", sync_shopify), ("amazon", "Amazon", sync_amazon)):
            if channel not in {"all", name}:
                continue
            if not credentials_present(name):
                if channel == name:
                    errors.append(f"{label} setup required")
                continue
            now = datetime.now(timezone.utc)
            try:
                fetched, next_cursor = fn(sync_start(state, name, now), now)
                orders.extend(fetched)
                # Only advance the cursor after a successful fetch, so failed syncs retry.
                cursors[name] = iso_z(next_cursor)
            except (RuntimeError, KeyError, ValueError) as exc:
                errors.append(f"{label}: {exc}")
        finished = datetime.now(timezone.utc).isoformat()
        write_state({"last_sync": finished, "cursors": cursors})
        return {"orders": orders, "errors": errors, "last_sync": finished}


class Handler(BaseHTTPRequestHandler):
    def allowed_host(self):
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin in {f"http://{self.headers.get('Host', '')}"}

    def send_body(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload, status=200):
        self.send_body(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status)

    def do_GET(self):
        if not self.allowed_host():
            self.send_json({"error": "Forbidden"}, 403)
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/status":
            state = read_state()
            self.send_json({
                "shopify": credentials_present("shopify"),
                "amazon": credentials_present("amazon"),
                "last_sync": state.get("last_sync")
            })
            return
        if path in {"/", "/index.html"}:
            # Only the app page is served, so .env and other local files are never exposed.
            self.send_body(INDEX_FILE.read_bytes(), "text/html; charset=utf-8")
            return
        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        if not self.allowed_host() or not self.same_origin():
            self.send_json({"error": "Forbidden"}, 403)
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/sync":
            self.send_json({"error": "Not found"}, 404)
            return
        channel = urllib.parse.parse_qs(parsed.query).get("channel", ["all"])[0]
        if channel not in {"all", "shopify", "amazon"}:
            self.send_json({"error": "Unknown channel"}, 400)
            return
        self.send_json(run_sync(channel))


def main():
    load_env()
    port = int(os.getenv("PORT") or "8080")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Stepwhere Sales Desk is running at http://127.0.0.1:{port}")
    print("Keep this window open while using live marketplace sync.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
