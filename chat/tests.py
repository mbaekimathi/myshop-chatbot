"""Tests run against SQLite — the unmanaged MY-SHOP tables are created
explicitly with schema_editor so no MySQL server is needed:

    MYSQL_ENABLED=False python manage.py test chat
"""

import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.hashers import make_password
from django.db import connection
from django.test import Client, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from . import services
from .bot import reply_for
from .models import ConversationKind, Message
from .myshop_models import (
    Shop,
    Item,
    ShopStock,
    ShopReceipt,
    ShopReceiptLine,
    ShopDaySession,
    Expense,
    ShopItemPrice,
    CompanyProfile,
    ItemSerial,
    Supplier,
    StockMovement,
    StockMovementLine,
)

UNMANAGED = [
    Shop,
    Item,
    ShopStock,
    ShopReceipt,
    ShopReceiptLine,
    ShopDaySession,
    Expense,
    ShopItemPrice,
    CompanyProfile,
    ItemSerial,
    Supplier,
    StockMovement,
    StockMovementLine,
]


def create_unmanaged_tables():
    with connection.schema_editor() as editor:
        for model in UNMANAGED:
            editor.create_model(model)


def drop_unmanaged_tables():
    with connection.schema_editor() as editor:
        for model in reversed(UNMANAGED):
            try:
                editor.delete_model(model)
            except Exception:
                pass


class ChatTestCase(TransactionTestCase):
    # TransactionTestCase: no wrapping atomic(), so schema_editor can create
    # the unmanaged MY-SHOP tables in setUpClass below.
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        create_unmanaged_tables()

    @classmethod
    def tearDownClass(cls):
        drop_unmanaged_tables()
        super().tearDownClass()

    def setUp(self):
        # TransactionTestCase does not flush unmanaged tables between tests —
        # clear them so each test sees a clean MY-SHOP schema.
        for model in UNMANAGED:
            model.objects.all().delete()
        self.shop_a = Shop.objects.create(
            name="Alpha Store", location="Nairobi CBD", email="a@x.co",
            phone_number="0700000001", login_code="111111",
            password_hash=make_password("pass-a"), is_suspended=False,
            is_hidden=False,
        )
        self.shop_b = Shop.objects.create(
            name="Beta Mart", location="Westlands", email="b@x.co",
            phone_number="0700000002", login_code="222222",
            password_hash=make_password("pass-b"), is_suspended=False,
            is_hidden=False,
        )
        self.client = Client()

    def login(self, code="111111", password="pass-a"):
        return self.client.post(
            reverse("chat:login"),
            {"login_code": code, "password": password},
        )


class AuthTests(ChatTestCase):
    def test_login_success(self):
        resp = self.login()
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/chat/", resp["Location"])

    def test_login_wrong_password(self):
        resp = self.login(password="wrong")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Invalid shop code or password")

    def test_suspended_shop_rejected(self):
        self.shop_a.is_suspended = True
        self.shop_a.save()
        resp = self.login()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Invalid shop code or password")

    def test_app_requires_login(self):
        resp = self.client.get(reverse("chat:app"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])


class DirectChatTests(ChatTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_open_direct_conversation(self):
        resp = self.client.post(
            reverse("chat:api_open"),
            data='{"target": "%s"}' % self.shop_b.pk,
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["conversation"]["kind"], "direct")
        self.assertEqual(data["conversation"]["peer_shop_id"], self.shop_b.pk)

    def test_send_and_poll_direct(self):
        convo = services.get_or_create_direct(self.shop_a, self.shop_b)
        resp = self.client.post(
            reverse("chat:api_send", args=[convo.pk]),
            data='{"body": "Hello Beta", "client_tag": "t1"}',
            content_type="application/json",
        )
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(
            Message.objects.filter(conversation=convo).count(), 1
        )
        # Duplicate client_tag must not repost.
        resp = self.client.post(
            reverse("chat:api_send", args=[convo.pk]),
            data='{"body": "Hello Beta", "client_tag": "t1"}',
            content_type="application/json",
        )
        self.assertEqual(
            Message.objects.filter(conversation=convo).count(), 1
        )
        # Shop B sees the message and an unread badge.
        client_b = Client()
        client_b.post(
            reverse("chat:login"),
            {"login_code": "222222", "password": "pass-b"},
        )
        resp = client_b.get(reverse("chat:api_poll"))
        convos = {c["id"]: c for c in resp.json()["conversations"]}
        self.assertEqual(convos[convo.pk]["unread"], 1)
        resp = client_b.get(
            reverse("chat:api_poll"),
            {"conversation_id": convo.pk},
        )
        active = resp.json()["active"]
        self.assertEqual(len(active["messages"]), 1)
        self.assertEqual(active["messages"][0]["body"], "Hello Beta")
        self.assertFalse(active["messages"][0]["mine"])

    def test_cannot_send_to_foreign_conversation(self):
        shop_c = Shop.objects.create(
            name="Gamma", location="Kisumu", email="c@x.co",
            phone_number="0700000003", login_code="333333",
            password_hash=make_password("pass-c"), is_suspended=False,
            is_hidden=False,
        )
        convo = services.get_or_create_direct(self.shop_b, shop_c)
        resp = self.client.post(
            reverse("chat:api_send", args=[convo.pk]),
            data='{"body": "intruder"}',
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)


class BotTests(ChatTestCase):
    def setUp(self):
        super().setUp()
        self.login()
        item = Item.objects.create(
            category="Grocery", name="Sugar 1kg",
            minimum_selling_price=Decimal("100"),
            shop_price=Decimal("150"), stock=Decimal("40"),
            low_stock_notify=False, low_stock_threshold=0,
            is_suspended=False,
        )
        ShopStock.objects.create(
            shop_id=self.shop_a.pk, item_id=item.pk,
            quantity=Decimal("3"), low_stock_threshold=5,
            average_cost=Decimal("80"),
        )
        self.receipt = ShopReceipt.objects.create(
            shop_id=self.shop_a.pk, receipt_number="R-001",
            kind="sale", payment_method="cash", client_name="",
            total=Decimal("1500"), amount_paid=Decimal("1500"),
            cash_amount=Decimal("1500"), mpesa_amount=Decimal("0"),
            status="confirmed",
        )
        ShopReceiptLine.objects.create(
            receipt_id=self.receipt.pk, item_id=item.pk,
            item_name="Sugar 1kg", quantity=Decimal("10"),
            returned_quantity=Decimal("0"), unit_price=Decimal("150"),
            line_total=Decimal("1500"),
        )

    def test_bot_conversation_created_with_welcome(self):
        convo = services.get_or_create_bot(self.shop_a)
        self.assertEqual(convo.kind, ConversationKind.BOT)
        self.assertEqual(convo.messages.count(), 1)
        self.assertTrue(convo.messages.first().is_bot)

    def test_send_gets_bot_reply(self):
        convo = services.get_or_create_bot(self.shop_a)
        resp = self.client.post(
            reverse("chat:api_send", args=[convo.pk]),
            data='{"body": "sales today"}',
            content_type="application/json",
        )
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertIn("reply", data)
        self.assertIn("KSh 1,500.00", data["reply"]["body"])

    def test_sales_intent(self):
        reply = reply_for(self.shop_a, "how much did we sell today")
        self.assertIn("KSh 1,500.00", reply)

    def test_stock_intent(self):
        reply = reply_for(self.shop_a, "stock sugar")
        self.assertIn("Sugar 1kg", reply)
        self.assertIn("*3*", reply)
        self.assertIn("KSh 150.00", reply)

    def test_low_stock_intent(self):
        reply = reply_for(self.shop_a, "low stock")
        self.assertIn("Sugar 1kg", reply)

    def test_top_sellers_intent(self):
        reply = reply_for(self.shop_a, "top sellers")
        self.assertIn("Sugar 1kg", reply)

    def test_receipt_lookup(self):
        reply = reply_for(self.shop_a, "receipt R-001")
        self.assertIn("R-001", reply)
        self.assertIn("KSh 1,500.00", reply)

    def test_credit_window_filter(self):
        r = ShopReceipt.objects.create(
            shop_id=self.shop_a.pk, receipt_number="C-002",
            kind="credit", payment_method="none", client_name="Joe",
            total=Decimal("1000"), amount_paid=Decimal("400"),
            cash_amount=Decimal("0"), mpesa_amount=Decimal("0"),
            status="confirmed",
        )
        ShopReceipt.objects.filter(pk=r.pk).update(
            created_at=timezone.now() - timedelta(days=400)
        )
        # Bare "credits" stays all-time: the old balance still shows.
        self.assertIn("KSh 600.00", reply_for(self.shop_a, "credits"))
        # A window containing the issue date shows it, labelled.
        reply = reply_for(self.shop_a, "credits 1 Jan 2025 to 31 Dec 2025")
        self.assertIn("KSh 600.00", reply)
        self.assertIn("issued 01 Jan 2025", reply)
        # A window that excludes it reports nothing.
        self.assertIn(
            "No outstanding credit", reply_for(self.shop_a, "credits today")
        )

    def test_help_intent(self):
        reply = reply_for(self.shop_a, "help")
        self.assertIn("sales today", reply.lower())

    def test_greeting(self):
        reply = reply_for(self.shop_a, "hello")
        self.assertIn("Alpha Store", reply)

    def test_fallback(self):
        reply = reply_for(self.shop_a, "xyzzy plover")
        self.assertIn("help", reply.lower())


class PendingConfirmTests(ChatTestCase):
    """Staff acting on a shop they aren't allocated to send requests as a
    pending-confirmation card; a shop member's staff code executes it."""

    EMPLOYEE_DDL = (
        "CREATE TABLE IF NOT EXISTS employees_employeeprofile ("
        "id INTEGER PRIMARY KEY, employee_id VARCHAR(12) NOT NULL, "
        "user_id BIGINT, role VARCHAR(32) NOT NULL, "
        "status VARCHAR(16) NOT NULL)",
        "CREATE TABLE IF NOT EXISTS "
        "employees_employeeprofile_assigned_shops ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "employeeprofile_id BIGINT NOT NULL, shop_id BIGINT NOT NULL)",
    )

    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import User

        with connection.cursor() as cur:
            for ddl in self.EMPLOYEE_DDL:
                cur.execute(ddl)
            cur.execute("DELETE FROM employees_employeeprofile")
            cur.execute(
                "DELETE FROM employees_employeeprofile_assigned_shops"
            )
        manager_user = User.objects.create_user(username="manager")
        member_user = User.objects.create_user(
            username="member1", first_name="Member", last_name="One"
        )
        with connection.cursor() as cur:
            # Acting employee: a manager NOT allocated to shop_a.
            cur.execute(
                "INSERT INTO employees_employeeprofile "
                "(id, employee_id, user_id, role, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                [1, "100001", manager_user.pk, "company_manager", "active"],
            )
            # Shop member: allocated to shop_a, code 654321.
            cur.execute(
                "INSERT INTO employees_employeeprofile "
                "(id, employee_id, user_id, role, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                [2, "654321", member_user.pk, "shop_attendant", "active"],
            )
            cur.execute(
                "INSERT INTO employees_employeeprofile_assigned_shops "
                "(employeeprofile_id, shop_id) VALUES (%s, %s)",
                [2, self.shop_a.pk],
            )
        self.item = Item.objects.create(
            category="Grocery", name="Sugar 1kg",
            minimum_selling_price=Decimal("100"),
            shop_price=Decimal("150"), stock=Decimal("40"),
            low_stock_notify=False, low_stock_threshold=0,
            is_suspended=False,
        )
        self.stock = ShopStock.objects.create(
            shop_id=self.shop_a.pk, item_id=self.item.pk,
            quantity=Decimal("10"), low_stock_threshold=5,
            average_cost=Decimal("80"),
        )
        # Sign in as the non-member manager operating shop_a.
        session = self.client.session
        session["chatbot_employee_id"] = 1
        session["chatbot_shop_id"] = self.shop_a.pk
        session.save()

    def sale_payload(self, **over):
        data = {
            "kind": "sale",
            "payment_method": "cash",
            "lines": [
                {"item_id": self.item.pk, "quantity": "2", "serials": []}
            ],
        }
        data.update(over)
        return data

    def pending_message(self):
        for msg in Message.objects.order_by("-id"):
            pa = (msg.meta or {}).get("pending_action") or {}
            if pa.get("status") == "pending":
                return msg, pa
        return None, {}

    def test_nonmember_submission_posts_pending_card(self):
        resp = self.client.post(
            reverse("chat:api_sale"),
            data=json.dumps(self.sale_payload()),
            content_type="application/json",
        )
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["pending"])
        # Nothing was written — the sale waits for a member's code.
        self.assertEqual(ShopReceipt.objects.count(), 0)
        msg, pa = self.pending_message()
        self.assertIsNotNone(msg)
        self.assertEqual(pa["action"], "sale")
        self.assertIn("payload", pa)  # stored server-side
        # …but stripped from the serialized card the client receives.
        sent = data["message"]["meta"]["pending_action"]
        self.assertNotIn("payload", sent)
        self.assertEqual(sent["requested_by"], "manager")
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal("10"))

    def test_member_code_executes_immediately(self):
        resp = self.client.post(
            reverse("chat:api_sale"),
            data=json.dumps(self.sale_payload(staff_code="654321")),
            content_type="application/json",
        )
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertNotIn("pending", data)
        receipt = ShopReceipt.objects.get()
        self.assertEqual(receipt.created_by_id, 2)

    def test_confirm_executes_with_member_details(self):
        resp = self.client.post(
            reverse("chat:api_sale"),
            data=json.dumps(self.sale_payload()),
            content_type="application/json",
        )
        msg, _ = self.pending_message()
        resp = self.client.post(
            reverse("chat:api_pending_confirm", args=[msg.pk]),
            data=json.dumps({"staff_code": "654321"}),
            content_type="application/json",
        )
        data = resp.json()
        self.assertTrue(data["ok"])
        receipt = ShopReceipt.objects.get()
        self.assertEqual(receipt.created_by_id, 2)
        self.assertEqual(receipt.status, "confirmed")
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal("8"))
        # The card becomes the real sale card, stamped with the member.
        meta = data["message"]["meta"]
        self.assertIn("sale", meta)
        self.assertNotIn("pending_action", meta)
        self.assertEqual(meta["confirmed"]["by"], "Member One")

    def test_confirm_rejects_nonmember_code(self):
        self.client.post(
            reverse("chat:api_sale"),
            data=json.dumps(self.sale_payload()),
            content_type="application/json",
        )
        msg, _ = self.pending_message()
        # The manager's own code is active but not a shop member.
        resp = self.client.post(
            reverse("chat:api_pending_confirm", args=[msg.pk]),
            data=json.dumps({"staff_code": "100001"}),
            content_type="application/json",
        )
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(ShopReceipt.objects.count(), 0)
        msg.refresh_from_db()
        self.assertEqual(
            msg.meta["pending_action"]["status"], "pending"
        )

    def test_second_confirm_fails(self):
        self.client.post(
            reverse("chat:api_sale"),
            data=json.dumps(self.sale_payload()),
            content_type="application/json",
        )
        msg, _ = self.pending_message()
        url = reverse("chat:api_pending_confirm", args=[msg.pk])
        self.client.post(
            url,
            data=json.dumps({"staff_code": "654321"}),
            content_type="application/json",
        )
        resp = self.client.post(
            url,
            data=json.dumps({"staff_code": "654321"}),
            content_type="application/json",
        )
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(ShopReceipt.objects.count(), 1)

    def test_pending_transfer_confirms_into_direct_convo(self):
        resp = self.client.post(
            reverse("chat:api_transfers"),
            data=json.dumps(
                {
                    "destination_shop_id": self.shop_b.pk,
                    "lines": [
                        {
                            "item_id": self.item.pk,
                            "quantity": "3",
                            "serials": [],
                        }
                    ],
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["pending"])
        self.assertEqual(StockMovement.objects.count(), 0)
        msg, pa = self.pending_message()
        self.assertEqual(pa["action"], "transfer")

        resp = self.client.post(
            reverse("chat:api_pending_confirm", args=[msg.pk]),
            data=json.dumps({"staff_code": "654321"}),
            content_type="application/json",
        )
        data = resp.json()
        self.assertTrue(data["ok"])
        movement = StockMovement.objects.get()
        self.assertEqual(movement.request_status, "pending")
        self.assertEqual(movement.created_by_id, 2)
        self.assertEqual(movement.requested_from_shop_id, self.shop_b.pk)
        # The real transfer card was posted into the direct conversation.
        card_msg = Message.objects.get(client_tag=f"transfer:{movement.pk}")
        self.assertEqual(card_msg.meta["transfer"]["status"], "pending")
        self.assertEqual(data["card_conversation_id"], card_msg.conversation_id)


class ViewSmokeTests(ChatTestCase):
    def test_app_renders_after_login(self):
        self.login()
        resp = self.client.get(reverse("chat:app"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "wa-app")

    def test_poll_returns_assistant(self):
        self.login()
        resp = self.client.get(reverse("chat:api_poll"))
        convos = resp.json()["conversations"]
        kinds = {c["kind"] for c in convos}
        self.assertIn("bot", kinds)
