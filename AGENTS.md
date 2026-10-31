# MY-CHATBOT — project notes

## Stack
Django 5 app (`chat`). Templates in `templates/`, JS in `static/js/chatbot.js`,
one consolidated stylesheet `static/css/chatbot.css` (Manrope + Lucide, same
`--wa-*` design tokens as MY-SHOP `comms.css`/`whatsapp-inbox.css`).

## Database
Shares the `myshop` MySQL DB with the MY-SHOP project (`..\MY-SHOP`).
- `chat/myshop_models.py` — `managed = False` read models over MY-SHOP tables
  (`shops_shop`, `items_*`, `shops_shopreceipt*`, `shops_expense`,
  `shops_shopdaysession`, `shops_companyprofile`). Never add real FKs to them.
- `chat/models.py` — owned tables `chat_conversation`, `chat_participant`,
  `chat_message`. `shop_id` is a plain bigint pointing at `shops_shop.id`.
- `MYSQL_ENABLED=False` falls back to `db.sqlite3` (tests).

## Auth
Shop session key `chatbot_shop_id`. Shops sign in with `shops_shop.login_code`
+ `password_hash` (Django `check_password`, Argon2). Suspended/hidden shops
are rejected — mirrors `shops.services.authenticate_shop_login`.

## Realtime
No websockets — `GET /api/poll/?conversation_id=&after_id=` returns the
conversation list + new messages; the JS polls ~2.5s (8s hidden, exponential
backoff on errors). Presence = `Participant.last_seen_at` (25s window),
typing = `typing_until` (+4s), read ticks = `last_read_id`.

## Tests
```bash
MYSQL_ENABLED=False python manage.py test chat
```
Unmanaged MY-SHOP tables are created in the test DB via `schema_editor`.
