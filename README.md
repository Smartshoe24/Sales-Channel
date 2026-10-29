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

Since 1 January 2026 new Shopify apps are made in the Shopify **Dev Dashboard**. These apps don't show an access token to copy. Instead, the server exchanges the app's Client ID and Client secret for an access token that lasts 24 hours, and renews it automatically.

1. In Shopify admin open **Settings → Apps and sales channels → Develop apps** and click **Build apps in Dev Dashboard**. Create an app, for example "Stepwhere Sales Desk".
2. Create an app version with the **`read_orders`** access scope, release it, and install the app on your store.
3. Optional but recommended: in the app's API access settings request **Protected customer data** access for name, email, phone and address. Without it, orders still sync but customer names, phones and addresses stay hidden.
4. Copy the **Client ID** and **Client secret** from the app's Settings page into `.env`:

```text
SHOPIFY_STORE=your-store.myshopify.com
SHOPIFY_CLIENT_ID=your-client-id
SHOPIFY_CLIENT_SECRET=your-client-secret
```

5. Restart `server.py`. The Terminal window prints `Shopify: connected to <store name>` or the reason it couldn't connect.

If you already have an older app created in Shopify admin before 2026 with an Admin API access token (`shpat_…`), put it in `SHOPIFY_ACCESS_TOKEN` instead of the Client ID and secret.

Notes:
- The Client ID/secret method only works for a store that belongs to the same Shopify organization as the app. Creating the app from the store's own admin (step 1) makes sure of that.
- With `read_orders`, Shopify only returns orders from the last 60 days.

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
