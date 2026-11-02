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
  `shops_companypossettings`, `shops_shoppossettings`). Never add real FKs
  to them.
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

## Realtime
No websockets — `GET /api/poll/?conversation_id=&after_id=` returns the
conversation list + new messages; the JS polls ~2.5s (8s hidden, exponential
backoff on errors). Presence = `Participant.last_seen_at` (25s window),
typing = `typing_until` (+4s), read ticks = `last_read_id`.

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
