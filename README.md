# MY-CHATBOT

WhatsApp-style shop chat for the MY-SHOP system: shop-to-shop messaging plus
a data assistant that answers questions from live business data.

## What it does

- **Shop chat** — every registered shop signs in with the same 6-digit code +
  password it uses on the MY-SHOP portal, then chats with other shops:
  unread badges, delivery/read ticks, typing indicator, presence, photo
  messages, emoji picker, light/dark theme.
- **MY-SHOP Assistant** — a pinned bot thread per shop. Ask things like
  `sales today`, `stock sugar`, `low stock`, `top sellers`, `credit`,
  `expenses this month`, `receipt 00123`, `day status`, `shops`, or `help`.

## Stack

Same as MY-SHOP: Django 5, MySQL/MariaDB via PyMySQL, plain HTML/CSS/JS,
Manrope, Lucide icons, WhiteNoise.

## Database

Shares the `myshop` database with MY-SHOP:

- Reads `shops_shop`, `items_item`, `items_shopstock`, `items_shopitemprice`,
  `shops_shopreceipt`, `shops_shopreceiptline`, `shops_shopdaysession`,
  `shops_expense`, `shops_companyprofile` through **unmanaged** models.
- Owns only `chat_conversation`, `chat_participant`, `chat_message`
  (created by `migrate`).

## Quick start (local, XAMPP)

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000/ and sign in with a shop's code + password.

## Tests

```bash
MYSQL_ENABLED=False python manage.py test chat
```

(The suite creates the MY-SHOP tables it needs on SQLite, so no MySQL
server is required for tests.)
