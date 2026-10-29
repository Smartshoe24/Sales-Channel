# Stepwhere Sales Desk

Local-first sales, order tracking and invoicing system for Stepwhere.

## Run locally with live marketplace sync

1. Copy `.env.example` to a new file named `.env`.
2. Add the Shopify and/or Amazon credentials described below.
3. From this folder run (Python 3.9 or newer, no extra packages needed):

```bash
python3 server.py
```

Then open `http://localhost:8080`.

Keep the terminal window open. The app syncs automatically every two minutes. The first sync fetches the last `SYNC_LOOKBACK_DAYS` days of orders; after that each sync only fetches orders that changed since the previous one, so restarting the app catches up on anything missed.

Always open the app at the same address (for example always `http://localhost:8080`). The browser keeps a separate database for each address, so opening `127.0.0.1:8080` or the HTML file directly shows a different, empty order list.

## Shopify connection

The Shopify app needs `read_orders` access (Shopify only returns the last 60 days of orders with this scope). Put the store's `*.myshopify.com` domain and Admin API access token into `.env`:

```text
SHOPIFY_STORE=your-store.myshopify.com
SHOPIFY_ACCESS_TOKEN=your-token
```

Custom apps created in the Shopify admin before 1 January 2026 still show their Admin API token there. Since that date new custom apps are created in the Shopify Dev Dashboard, which does not display a token to copy; the token is obtained through the app's install flow.

## Amazon India connection

Register a private SP-API application in Seller Central, request the Orders role, self-authorize it, and place the LWA Client ID, Client Secret and Refresh Token into `.env`. The India marketplace and EU SP-API endpoint are already configured.

Without approved restricted-data access, Amazon does not return buyer name, phone or address, so Amazon orders show "Amazon customer". To stay within Amazon's rate limits, each sync reads item details for at most 25 orders; any remaining orders arrive on the following syncs.

## Included in this MVP

- Unified dashboard and order pipeline
- Order search and channel/status filters
- Detailed order view with status and payment updates
- Manual B2B order creation
- Automatic tax and total calculation
- Invoice preview, printing and PDF saving
- Live Shopify order sync plus CSV fallback
- Live Amazon India SP-API order sync
- CSV export, JSON backup and restore
- Local browser storage for offline operation
- Invoice/business settings
- Blank starting database with a Delete all orders control

## How sync and local edits interact

- Status or payment changes you make in the app are kept. Later syncs update the other order details but never overwrite a field you changed by hand.
- B2B invoice numbers keep counting up even after "Delete all orders", so an invoice number is never issued twice.
- Several tabs can be open; each tab reloads when another one saves.

## Important

The browser stores the data on the current device. Export backups regularly.
Marketplace credentials stay in the local `.env` file and are never returned to the browser. The local server only serves the app page and the `/api` endpoints, only accepts connections from this computer, and rejects sync requests from other websites. Marketplace sync requires an internet connection and the local server must be running.

`.env` and the server's `.sync_state.json` are listed in `.gitignore`; never commit them.
