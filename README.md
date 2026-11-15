# Software Store (Flask)

Public store (no login/registration) + admin panel at `/admin`. Buyer pays via Makamesco Pay (M-Pesa STK push),
download unlocks once payment is confirmed server-side.

## Run
    python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
    pip install -r requirements.txt
    cp .env.example .env   # fill in values, then export them (or use python-dotenv / your host's env settings)
    MAKAMESCO_SIMULATE=1 ADMIN_PASSWORD=secret python app.py    # test mode, no real payments
Open http://localhost:5000 and http://localhost:5000/admin

## Production
    gunicorn app:app --workers 2 --timeout 300
Put it behind HTTPS. On Render/similar, attach a persistent disk and point DATA_DIR at it
(uploads and the SQLite DB live there). Set every value from `.env` in the host's Environment settings (the `.env` file itself is not committed).

## Makamesco Pay
Gateway code is in `gateway.py`; every credential comes from `.env` (see `.env.example`).
STK push: `POST MAKAMESCO_STKPUSH_URL` with header `X-API-Key` and body
`{phoneNumber, amount, accountReference, transactionDesc}`.
Register this webhook in the Makamesco dashboard:
`https://softshop.gainpesa.site/callback?token=<MAKAMESCO_CALLBACK_TOKEN>`
(also served at `/webhook/makamesco`). Every gateway response/webhook is logged to `data/gateway.log`.
