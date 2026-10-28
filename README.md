# pos_system

A Flask point-of-sale for Kenyan shops: products and stock, barcode checkout, discounts, receipts with QR codes, refunds, reports, users, and a pluggable integration layer for KRA eTIMS, M-Pesa and any other API.

## Running locally

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # SQLite works out of the box for local use
python seed_dev_data.py       # optional sample data
python app.py                 # http://localhost:5000  (admin / ADMIN_PASSWORD)
```

Run the tests with `pip install pytest && pytest`.

## Integrations

Open **Integrations** in the admin menu. Every external API is an *integration* that reacts to POS events:

| Event | When |
|---|---|
| `sale.completed` | A sale is paid (immediately for cash, after confirmation for M-Pesa) |
| `sale.refunded` | An admin refunds a sale |
| `payment.requested` | The cashier picks a payment integration at checkout |
| `payment.completed` / `payment.failed` | The provider confirms or rejects a payment |

Every call is recorded in the activity log with the request and response (secrets redacted), and failed calls have a **Retry** button. A failing integration never blocks a sale.

Each integration has a **mode**. In `mock` mode everything works with no credentials (M-Pesa shows "Simulate success/failure" buttons, eTIMS signs invoices locally and marks them as test invoices), so you can try the full flow before you have API keys. Secrets are never stored in the database or shown in the UI; they're read from environment variables (see `.env.example`).

### M-Pesa (Daraja)
* **STK push**: choose "M-Pesa (STK push)" at checkout and enter the customer's number. The POS waits on a status page and moves to the receipt as soon as Safaricom calls back (or a status query confirms the payment). From there the cashier can resend the prompt, take cash instead, or cancel the sale (which restocks it).
* **C2B**: customers can also pay the Till/Paybill manually with account number `SALE<id>`. Press "Register C2B URLs" once on the M-Pesa page.
* Set `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET`, `MPESA_PASSKEY` and `PUBLIC_BASE_URL` (callbacks need public HTTPS), then switch the mode to `sandbox`, and to `production` when you go live.

### KRA eTIMS (OSCU)
* Each paid sale is sent to `saveTrnsSalesOsdc`. The signed invoice number, signature and verification QR code replace the receipt's KRA section. Refunds are sent as credit notes.
* Prices are treated as VAT-inclusive with a default tax type (B = 16%). Add the customer's KRA PIN at checkout to issue a tax invoice in their name.
* To go live: get OSCU access on the KRA portal, set the PIN, branch and device serial, press "Initialise eTIMS device" to get the CMC key, put it in `ETIMS_CMC_KEY`, and register your items on eTIMS using the item codes shown in the activity log (`<prefix><product id>`).

### Any other API (no code)
Add a **Custom API / webhook** integration. You can add as many as you like. Set:
* the URL and method (Jinja placeholders like `{{ sale.id }}` work),
* the events to send,
* headers, using `${ENV_VAR}` for keys (e.g. `{"Authorization": "Bearer ${MY_API_KEY}"}`),
* an optional Jinja body template. If you leave it blank, `{"event": ..., "data": {"sale": ..., "payment": ...}}` is sent,
* an optional HMAC secret env var, which adds an `X-POS-Signature: sha256=...` header over `timestamp.body`,
* optional response paths, so a third-party invoicing API's invoice number/QR code shows on the receipt.

### Writing your own adapter (code)
Drop a `.py` file in `integrations/plugins/` with a subclass of `integrations.Integration`, and it appears on the Integrations page automatically. `integrations/plugins/example_loyalty.py` is a complete template. Payment providers also set `category = 'payments'` and implement `parse_callback()` (and optionally `query_payment()`). They then appear as a checkout option, with callbacks arriving at `/integrations/<key>/callback`.

### Database
The integrations use new tables (`integration_configs`, `integration_logs`, `payments`, `sale_meta`) that the app creates on boot, so existing databases need no migration.
