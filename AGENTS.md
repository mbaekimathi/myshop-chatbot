# MY-CHATBOT — project notes

## Stack
Django 5 app (`chat`). Templates in `templates/`, JS in `static/js/chatbot.js`,
one consolidated stylesheet `static/css/chatbot.css` (Manrope + Lucide, same
`--wa-*` design tokens as MY-SHOP `comms.css`/`whatsapp-inbox.css`).

## Database
Shares the `myshop` MySQL DB with the MY-SHOP project (`..\MY-SHOP`).
- `chat/myshop_models.py` — `managed = False` read models over MY-SHOP tables
  (`shops_shop`, `items_*`, `shops_shopreceipt*`, `shops_expense`,
  `shops_shopdaysession`, `shops_companyprofile`,
  `shops_companyanalyticssettings`, `shops_companypossettings`,
  `shops_shoppossettings`). Never add real FKs to them.
  Sales / item analytics follow `receipt_data_source` (pending|confirmed);
  the user can override in chat (`sales today confirmed`).
- `chat/models.py` — owned tables `chat_conversation`, `chat_participant`,
  `chat_message`. `shop_id` is a plain bigint pointing at `shops_shop.id`.
- `MYSQL_ENABLED=False` falls back to `db.sqlite3` (tests).

## Auth
Shop session key `chatbot_shop_id`. Shops sign in with `shops_shop.login_code`
+ `password_hash` (Django `check_password`, Argon2). Suspended/hidden shops
are rejected — mirrors `shops.services.authenticate_shop_login`.

## Member confirmation
Staff signed in via the employee portal (`chatbot_employee_id`) who act on a
shop they aren't allocated to (`_needs_member_confirmation`) are gated only
on **stock-in, transfers and trades** — those send a *pending-confirmation card* in
the shop's Assistant thread instead of being blocked: `stock.build_pending`
dry-runs the real `create_*` in a rolled-back transaction and stores the
payload in `Message.meta.pending_action` (`payload` is stripped on
serialize). A shop member's staff code on
`POST /api/pending/<message_id>/confirm/` executes it via
`stock.execute_pending` with `created_by_id` = the confirming member —
`member_employee_id` + `verify_employee_code(require_member=True)` gate it.
If a valid member code is typed directly in the stock panel it executes
immediately (no pending card).

Accepting or declining an incoming transfer
(`POST /api/transfers/<id>/respond/` → `stock.respond_to_transfer`) also
needs the 6-digit code of a member of the *receiving* shop
(`verify_employee_code(require_member=True)`) — the employee id lands on
`items_stockmovement.responded_by_id` and the card meta `responded_by`
names who confirmed/declined.

Sales, credit sales and quotations are exempt (`api_sale`): a staff session
completes them under its own employee profile when no code is typed, and a
typed code only needs to be an *active* employee (`require_member=False`),
not a shop member. Shop sessions still require a staff code as before.

## Trade
`api_sale` also accepts `kind=trade_out|trade_in`, routed to `_api_trade` →
`stock.create_trade` (mirrors MY-SHOP `complete_shop_checkout` trade paths:
trade-out releases stock like a sale, trade-in stocks in at the "amount
owed" price; both need client name+phone and write `items_stockmovement`
rows so they show in stock reports). The Trade FAB (`data-fab="trade"`,
Lucide `repeat`) only renders when `stock.trade_flags(me)` — the effective
POS settings (`shops_companypossettings` defaults merged with
`shops_shoppossettings` when `override_pos` is on) — enables at least one
kind; the panel's Trade out / Trade in toggle shows only enabled kinds.

## Expenses
The expense FAB (`data-fab="expense"`, Lucide `wallet`) is always shown —
MY-SHOP has no enable flag for it. `POST /api/expenses/` →
`stock.create_expense` mirrors `register_shop_expense`: category (fixed
`EXPENSE_CATEGORIES` list matching MY-SHOP's), name, amount, payment status
(paid/partial/unpaid), required supplier name+phone upserted into
`shops_expensesupplier`. Member-confirmation gating applies like
stock-in/transfers.

## Activity feeds
Shared all-shops feed groups auto-accumulate completed actions so anyone can
track the day's activity: **Incoming stock** (`stockin`), **Transfers**
(`transfer`), **Trades** (`trade`), **Expenses** (`expense`).
- `services.activity_conversation(kind)` get-or-creates the group — identified
  by `pair_key="activity:<kind>"` (never collides with user groups, which have
  no pair_key) — and syncs every active shop in as a participant.
- `views._activity_post(...)` mirrors the same card into the feed with
  `meta.by` = the staff member's name (`stock_ops.employee_label` on the
  movement/receipt/expense `created_by_id`, or the pending-card confirmer);
  chatbot.js renders it as a "by …" byline under the card.
- Transfers post a **readonly** card on send (tagged
  `transfer-feed:<movement_id>`; the receiving shop still sees it blind until
  fulfilled) — `api_transfer_respond` updates that card's status and adds a
  note on accept/decline.

## Branding
App name comes from `services.company_name()` (`shops_companyprofile.name`,
fallback "MY-SHOP") — `services.app_name()` = "<Company> Chat",
`bot_name()` = "<Company> Assistant", `bot_welcome(shop)` the greeting.
The PWA manifest is NOT a static file: `GET /manifest.json` → `views.manifest`
renders it dynamically so the install name follows the company name.
App icons are also dynamic: `GET /icon-192.png` / `/icon-512.png` →
`views.app_icon` draws the company name (fallback **MY SHOP**) via
`chat/icons.py`; `static/img/icon-*.png` are offline fallbacks only.

## Realtime
No websockets — `GET /api/poll/?conversation_id=&after_id=` returns the
conversation list + new messages; the JS polls ~2.5s (8s hidden, exponential
backoff on errors). Presence = `Participant.last_seen_at` (25s window),
typing = `typing_until` (+4s), read ticks = `last_read_id`.

## PWA auto-update
`static/js/pwa-update.js` (every page) registers `GET /sw.js` and rechecks on
focus / every 5 min; a new worker reloads the app. Manifest stays at a stable
`/manifest.json` with `id: "/"`; icon URLs include `?v=` so Chrome can refresh
the home-screen icon after the installed app is opened (Android). Desktop /
iOS icon refresh is browser-limited.

## Deploy (cPanel/Passenger at /home3/richcomc/CHAT)
```bash
./deploy.sh   # pull → pip install → migrate → collectstatic --clear → restart
```
- WhiteNoise serves `staticfiles/` in production — without collectstatic,
  pulled JS/CSS changes stay invisible (symptom: works on localhost only).
- Passenger restart = `touch tmp/restart.txt` (app root `tmp/`).
- Bump `?v=` in templates when editing JS/CSS so browsers drop cache.

## Tests
```bash
MYSQL_ENABLED=False python manage.py test chat
```
Unmanaged MY-SHOP tables are created in the test DB via `schema_editor`.
