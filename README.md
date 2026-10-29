# Stepwhere Sales Desk

Local-first sales, order tracking and invoicing system for Stepwhere.

## Run locally with live marketplace sync

1. Copy `.env.example` to a new file named `.env`.
2. Add the Shopify and/or Amazon credentials described below.
3. From this folder run:

```bash
python3 server.py
```

Then open `http://localhost:8080`.

Keep the terminal window open. The app syncs automatically every two minutes and catches up on missed orders when restarted.

## Shopify connection

The Shopify app needs `read_orders` access. Put the store's `*.myshopify.com` domain and Admin API access token into `.env`:

```text
SHOPIFY_STORE=your-store.myshopify.com
SHOPIFY_ACCESS_TOKEN=your-token
```

## Amazon India connection

Register a private SP-API application in Seller Central, request the Orders role, self-authorize it, and place the LWA Client ID, Client Secret and Refresh Token into `.env`. The India marketplace and EU SP-API endpoint are already configured.

## Included in this MVP

- Unified dashboard and order pipeline
- Order search and channel/status filters
- Detailed order view with status updates
- Manual B2B order creation
- Automatic tax and total calculation
- Invoice preview, printing and PDF saving
- Live Shopify order sync plus CSV fallback
- Live Amazon India SP-API order sync
- CSV export, JSON backup and restore
- Local browser storage for offline operation
- Invoice/business settings
- Blank starting database with a Delete all orders control

## Important

The browser stores the data on the current device. Export backups regularly.
Marketplace credentials stay in the local `.env` file and are never returned to the browser. The app remains local, but marketplace sync requires an internet connection and the local server must be running.
