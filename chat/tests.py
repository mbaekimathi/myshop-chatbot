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
from .models import Conversation, ConversationKind, Message
from .myshop_models import (
    Shop,
    Item,
    ShopStock,
    ShopReceipt,
    ShopReceiptLine,
    ShopDaySession,
    Expense,
    ExpenseSupplier,
    ShopItemPrice,
    CompanyProfile,
    CompanyPosSettings,
    ShopPosSettings,
    ItemSerial,
    Supplier,
    StockMovement,
    StockMovementLine,
    Client as ClientModel,
)

UNMANAGED = [
    Shop,
    Item,
    ShopStock,
    ShopReceipt,
    ShopReceiptLine,
    ShopDaySession,
    Expense,
    ExpenseSupplier,
    ShopItemPrice,
    CompanyProfile,
    CompanyPosSettings,
    ShopPosSettings,
    ItemSerial,
    Supplier,
    StockMovement,
    StockMovementLine,
    ClientModel,
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


class IconTests(ChatTestCase):
    def test_icon_falls_back_to_my_shop(self):
        from . import icons

        self.assertEqual(icons.icon_label(), "MY SHOP")
        png = icons.render_app_icon(192, rounded=True)
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_icon_uses_company_name(self):
        from . import icons

        CompanyProfile.objects.create(
            name="Acme Retail",
            phone_number="0700",
            email="a@x.co",
            location="Nairobi",
        )
        self.assertEqual(icons.icon_label(), "Acme Retail")

    def test_app_icon_endpoint_and_manifest(self):
        resp = self.client.get(reverse("chat:app_icon", kwargs={"size": 192}))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertTrue(resp.content.startswith(b"\x89PNG"))

        manifest = self.client.get(reverse("chat:manifest")).json()
        self.assertEqual(manifest["id"], "/")
        self.assertTrue(manifest["icons"])
        self.assertTrue(manifest["icons"][0]["src"].startswith("/icon-"))
        self.assertIn("v=", manifest["icons"][0]["src"])

        sw = self.client.get(reverse("chat:service_worker"))
        self.assertEqual(sw.status_code, 200)
        self.assertIn("javascript", sw["Content-Type"])
        self.assertIn("no-cache", sw["Cache-Control"])


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
    """Staff acting on a shop they aren't allocated to: stock-in and
    transfers send a pending-confirmation card which a shop member's staff
    code executes; sales, credit sales and quotations complete directly
    under the acting employee's account."""

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

    def stockin_payload(self, **over):
        data = {
            "supplier_name": "ACME Traders",
            "supplier_phone": "0712 345 678",
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

    def test_nonmember_sale_executes_directly(self):
        """Sales are exempt from member confirmation — the signed-in staff
        account signs the receipt when no code is typed."""
        resp = self.client.post(
            reverse("chat:api_sale"),
            data=json.dumps(self.sale_payload()),
            content_type="application/json",
        )
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertNotIn("pending", data)
        receipt = ShopReceipt.objects.get()
        self.assertEqual(receipt.created_by_id, 1)
        self.assertEqual(receipt.status, "confirmed")
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal("8"))

    def test_nonmember_credit_executes_directly(self):
        resp = self.client.post(
            reverse("chat:api_sale"),
            data=json.dumps(
                self.sale_payload(
                    kind="credit",
                    client_name="Joe",
                    credit_due_date="2099-01-01",
                )
            ),
            content_type="application/json",
        )
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertNotIn("pending", data)
        receipt = ShopReceipt.objects.get()
        self.assertEqual(receipt.kind, "credit")
        self.assertEqual(receipt.created_by_id, 1)

    def test_nonmember_quotation_executes_directly(self):
        resp = self.client.post(
            reverse("chat:api_sale"),
            data=json.dumps(
                self.sale_payload(kind="quotation", client_name="Joe")
            ),
            content_type="application/json",
        )
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertNotIn("pending", data)
        receipt = ShopReceipt.objects.get()
        self.assertEqual(receipt.kind, "quotation")
        # Quotations never touch stock.
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal("10"))

    def test_nonmember_own_code_executes(self):
        """A typed code only needs to be an active employee — it no longer
        has to belong to a shop member for sales."""
        resp = self.client.post(
            reverse("chat:api_sale"),
            data=json.dumps(self.sale_payload(staff_code="100001")),
            content_type="application/json",
        )
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertNotIn("pending", data)
        self.assertEqual(ShopReceipt.objects.get().created_by_id, 1)

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

    def test_nonmember_stockin_posts_pending_card(self):
        resp = self.client.post(
            reverse("chat:api_stock_in"),
            data=json.dumps(self.stockin_payload()),
            content_type="application/json",
        )
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["pending"])
        # Nothing was written — the stock-in waits for a member's code.
        self.assertEqual(StockMovement.objects.count(), 0)
        msg, pa = self.pending_message()
        self.assertIsNotNone(msg)
        self.assertEqual(pa["action"], "stockin")
        self.assertIn("payload", pa)  # stored server-side
        # …but stripped from the serialized card the client receives.
        sent = data["message"]["meta"]["pending_action"]
        self.assertNotIn("payload", sent)
        self.assertEqual(sent["requested_by"], "manager")
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal("10"))

    def test_confirm_executes_with_member_details(self):
        self.client.post(
            reverse("chat:api_stock_in"),
            data=json.dumps(self.stockin_payload()),
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
        movement = StockMovement.objects.get()
        self.assertEqual(movement.created_by_id, 2)
        self.assertEqual(movement.movement_type, "in")
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal("12"))
        # The card becomes the real stock-in card, stamped with the member.
        meta = data["message"]["meta"]
        self.assertIn("stock_in", meta)
        self.assertNotIn("pending_action", meta)
        self.assertEqual(meta["confirmed"]["by"], "Member One")

    def test_confirm_rejects_nonmember_code(self):
        self.client.post(
            reverse("chat:api_stock_in"),
            data=json.dumps(self.stockin_payload()),
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
        self.assertEqual(StockMovement.objects.count(), 0)
        msg.refresh_from_db()
        self.assertEqual(
            msg.meta["pending_action"]["status"], "pending"
        )

    def test_second_confirm_fails(self):
        self.client.post(
            reverse("chat:api_stock_in"),
            data=json.dumps(self.stockin_payload()),
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
        self.assertEqual(StockMovement.objects.count(), 1)

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


class TransferBlindTests(ChatTestCase):
    """The receiving shop counts what arrived blind — the sent quantities
    (and serial values) stay hidden until they accept and stock moves."""

    def setUp(self):
        super().setUp()
        self.item = Item.objects.create(
            category="Grocery", name="Sugar 1kg",
            minimum_selling_price=Decimal("100"),
            shop_price=Decimal("150"), stock=Decimal("40"),
            low_stock_notify=False, low_stock_threshold=0,
            is_suspended=False,
        )
        ShopStock.objects.create(
            shop_id=self.shop_a.pk, item_id=self.item.pk,
            quantity=Decimal("10"), low_stock_threshold=5,
            average_cost=Decimal("80"),
        )
        ShopStock.objects.create(
            shop_id=self.shop_b.pk, item_id=self.item.pk,
            quantity=Decimal("0"), low_stock_threshold=5,
            average_cost=Decimal("80"),
        )

    def _posted_card(self):
        from . import stock as stock_ops

        movement, _ = stock_ops.create_transfer(
            self.shop_a,
            self.shop_b,
            [{"item_id": self.item.pk, "quantity": "3", "serials": []}],
            employee_id=0,
        )
        lines = list(
            stock_ops.StockMovementLine.objects.filter(
                movement_id=movement.pk
            ).order_by("pk")
        )
        convo = services.get_or_create_direct(self.shop_a, self.shop_b)
        meta = stock_ops.transfer_card(
            movement, self.shop_a, self.shop_b, lines
        )
        msg = services.post_card(
            convo,
            self.shop_a,
            "Stock transfer → Beta Mart: 3× Sugar 1kg",
            meta,
        )
        return msg, movement, convo

    def test_receiver_serialized_card_is_blind(self):
        msg, movement, _ = self._posted_card()
        data = services.serialize_message(msg, self.shop_b, None)
        t = data["meta"]["transfer"]
        self.assertTrue(t["blind"])
        self.assertEqual(t["items"][0]["qty"], "")
        self.assertEqual(t["items"][0]["serial_count"], 0)
        # …and the real figures are not sitting in the message body either.
        self.assertNotIn("3×", data["body"])

    def test_sender_view_keeps_quantities(self):
        msg, movement, _ = self._posted_card()
        data = services.serialize_message(msg, self.shop_a, None)
        t = data["meta"]["transfer"]
        self.assertNotIn("blind", t)
        self.assertEqual(t["items"][0]["qty"], "3.000")
        self.assertIn("3×", data["body"])

    def test_fulfilled_transfer_reveals_quantities(self):
        from . import stock as stock_ops

        msg, movement, _ = self._posted_card()
        line = StockMovementLine.objects.get(movement_id=movement.pk)
        stock_ops.respond_to_transfer(
            self.shop_b, movement.pk, "accept", {str(line.pk): "3"},
            employee_id=0,
        )
        # Mirror the view: the card meta gets the new status.
        meta = dict(msg.meta)
        transfer = dict(meta["transfer"])
        transfer["status"] = "fulfilled"
        meta["transfer"] = transfer
        msg.meta = meta
        msg.save(update_fields=["meta"])
        data = services.serialize_message(msg, self.shop_b, None)
        t = data["meta"]["transfer"]
        self.assertNotIn("blind", t)
        self.assertEqual(t["items"][0]["qty"], "3.000")

    def test_chat_preview_hides_quantities(self):
        msg, movement, convo = self._posted_card()
        data = services.serialize_conversation(
            convo, self.shop_b, {self.shop_a.pk: self.shop_a}
        )
        preview = data["last_message"]["preview"]
        self.assertNotIn("3×", preview)
        self.assertIn("Incoming stock transfer", preview)

    def test_last_message_card_flags(self):
        # Notification sounds key off these: cards vs plain chat.
        msg, movement, convo = self._posted_card()
        data = services.serialize_conversation(
            convo, self.shop_b, {self.shop_a.pk: self.shop_a}
        )
        self.assertEqual(data["last_message"]["kind"], "text")
        self.assertEqual(data["last_message"]["card"], "transfer")

        services.post_text(convo, self.shop_a, "plain hello", "")
        data = services.serialize_conversation(
            convo, self.shop_b, {self.shop_a.pk: self.shop_a}
        )
        self.assertEqual(data["last_message"]["card"], "")


class TransferRespondCodeTests(ChatTestCase):
    """The receiving shop accepts or declines with a member's 6-digit
    staff code — the movement stores responded_by_id and the card/note
    name the employee who handled it."""

    EMPLOYEE_DDL = PendingConfirmTests.EMPLOYEE_DDL

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
        member_user = User.objects.create_user(
            username="memberb", first_name="Beta", last_name="Member"
        )
        outsider_user = User.objects.create_user(username="outsider")
        with connection.cursor() as cur:
            # Member of the receiving shop (shop_b).
            cur.execute(
                "INSERT INTO employees_employeeprofile "
                "(id, employee_id, user_id, role, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                [2, "654321", member_user.pk, "shop_attendant", "active"],
            )
            cur.execute(
                "INSERT INTO employees_employeeprofile_assigned_shops "
                "(employeeprofile_id, shop_id) VALUES (%s, %s)",
                [2, self.shop_b.pk],
            )
            # Active employee not allocated to shop_b.
            cur.execute(
                "INSERT INTO employees_employeeprofile "
                "(id, employee_id, user_id, role, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                [3, "100001", outsider_user.pk, "company_manager", "active"],
            )
        self.item = Item.objects.create(
            category="Grocery", name="Sugar 1kg",
            minimum_selling_price=Decimal("100"),
            shop_price=Decimal("150"), stock=Decimal("40"),
            low_stock_notify=False, low_stock_threshold=0,
            is_suspended=False,
        )
        ShopStock.objects.create(
            shop_id=self.shop_a.pk, item_id=self.item.pk,
            quantity=Decimal("10"), low_stock_threshold=5,
            average_cost=Decimal("80"),
        )
        ShopStock.objects.create(
            shop_id=self.shop_b.pk, item_id=self.item.pk,
            quantity=Decimal("0"), low_stock_threshold=5,
            average_cost=Decimal("80"),
        )
        self.login(code="222222", password="pass-b")

    def _make_transfer(self):
        from . import stock as stock_ops

        movement, lines = stock_ops.create_transfer(
            self.shop_a,
            self.shop_b,
            [{"item_id": self.item.pk, "quantity": "3", "serials": []}],
            employee_id=0,
        )
        movement_lines = list(
            StockMovementLine.objects.filter(
                movement_id=movement.pk
            ).order_by("pk")
        )
        convo = services.get_or_create_direct(self.shop_a, self.shop_b)
        msg = services.post_card(
            convo,
            self.shop_a,
            "Stock transfer → Beta Mart: 3× Sugar 1kg",
            stock_ops.transfer_card(
                movement, self.shop_a, self.shop_b, movement_lines
            ),
        )
        msg.client_tag = f"transfer:{movement.pk}"
        msg.save(update_fields=["client_tag"])
        return movement, movement_lines[0], convo

    def _respond(self, movement, payload):
        return self.client.post(
            reverse("chat:api_transfer_respond", args=[movement.pk]),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_accept_without_code_is_rejected(self):
        movement, line, _ = self._make_transfer()
        resp = self._respond(
            movement,
            {"decision": "accept", "received": {str(line.pk): "3"}},
        )
        self.assertFalse(resp.json()["ok"])
        self.assertIn("member of this shop", resp.json()["error"])
        movement.refresh_from_db()
        self.assertEqual(movement.request_status, "pending")
        self.assertIsNone(movement.responded_by_id)

    def test_non_member_code_is_rejected(self):
        movement, line, _ = self._make_transfer()
        resp = self._respond(
            movement,
            {
                "decision": "accept",
                "received": {str(line.pk): "3"},
                "staff_code": "100001",
            },
        )
        self.assertFalse(resp.json()["ok"])
        self.assertIn("member of this shop", resp.json()["error"])
        movement.refresh_from_db()
        self.assertEqual(movement.request_status, "pending")

    def test_accept_records_responder(self):
        movement, line, convo = self._make_transfer()
        resp = self._respond(
            movement,
            {
                "decision": "accept",
                "received": {str(line.pk): "3"},
                "staff_code": "654321",
            },
        )
        data = resp.json()
        self.assertTrue(data["ok"])
        movement.refresh_from_db()
        self.assertEqual(movement.request_status, "fulfilled")
        self.assertEqual(movement.responded_by_id, 2)
        self.assertEqual(
            data["card_meta"]["transfer"]["responded_by"], "Beta Member"
        )
        note = Message.objects.filter(conversation=convo).order_by("-id")[0]
        self.assertIn("by Beta Member", note.body)
        # The received stock actually landed at shop_b.
        self.assertEqual(
            ShopStock.objects.get(
                shop_id=self.shop_b.pk, item_id=self.item.pk
            ).quantity,
            Decimal("3.000"),
        )

    def test_decline_records_responder(self):
        movement, line, convo = self._make_transfer()
        resp = self._respond(
            movement,
            {
                "decision": "decline",
                "reason": "wrong items sent",
                "staff_code": "654321",
            },
        )
        data = resp.json()
        self.assertTrue(data["ok"])
        movement.refresh_from_db()
        self.assertEqual(movement.request_status, "declined")
        self.assertEqual(movement.responded_by_id, 2)
        self.assertEqual(movement.response_note, "wrong items sent")
        note = Message.objects.filter(conversation=convo).order_by("-id")[0]
        self.assertIn("declined by Beta Member", note.body)


class GroupTests(ChatTestCase):
    def setUp(self):
        super().setUp()
        self.shop_c = Shop.objects.create(
            name="Gamma Foods", location="Kisumu", email="c@x.co",
            phone_number="0700000003", login_code="333333",
            password_hash=make_password("pass-c"), is_suspended=False,
            is_hidden=False,
        )
        self.login()

    def create_group(self, name="Branch group", members=None):
        members = members if members is not None else [
            self.shop_b.pk, self.shop_c.pk
        ]
        return self.client.post(
            reverse("chat:api_group_create"),
            data=json.dumps({"name": name, "members": members}),
            content_type="application/json",
        )

    def test_create_group(self):
        resp = self.create_group()
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        convo = data["conversation"]
        self.assertEqual(convo["kind"], "group")
        self.assertEqual(convo["title"], "Branch group")
        self.assertIsNone(convo["peer_shop_id"])
        self.assertIn("Beta Mart", convo["subtitle"])
        self.assertIn("Gamma Foods", convo["subtitle"])
        # The creator sees the system message immediately.
        msgs = data["active"]["messages"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["kind"], "system")
        self.assertIn("created group", msgs[0]["body"])

    def test_create_group_requires_name(self):
        resp = self.create_group(name="  ")
        self.assertEqual(resp.status_code, 400)

    def test_create_group_requires_members(self):
        resp = self.create_group(members=[])
        self.assertEqual(resp.status_code, 400)

    def test_create_group_skips_self_and_duplicates(self):
        resp = self.create_group(
            members=[self.shop_a.pk, self.shop_b.pk, self.shop_b.pk]
        )
        self.assertTrue(resp.json()["ok"])
        convo_id = resp.json()["conversation"]["id"]
        convo = services.get_member_conversation(convo_id, self.shop_a)
        self.assertEqual(convo.participants.count(), 2)

    def test_group_send_and_poll(self):
        convo_id = self.create_group().json()["conversation"]["id"]
        resp = self.client.post(
            reverse("chat:api_send", args=[convo_id]),
            data='{"body": "Hello all"}',
            content_type="application/json",
        )
        self.assertTrue(resp.json()["ok"])

        # Both other members see the group, the sender label and unread.
        client_b = Client()
        client_b.post(
            reverse("chat:login"),
            {"login_code": "222222", "password": "pass-b"},
        )
        resp = client_b.get(
            reverse("chat:api_poll"), {"conversation_id": convo_id}
        )
        data = resp.json()
        convos = {c["id"]: c for c in data["conversations"]}
        self.assertEqual(convos[convo_id]["title"], "Branch group")
        # System notice + the message = 2 unread for a fresh member.
        self.assertEqual(convos[convo_id]["unread"], 2)
        msgs = data["active"]["messages"]
        self.assertEqual(msgs[-1]["body"], "Hello all")
        self.assertEqual(msgs[-1]["sender_name"], "Alpha Store")
        self.assertFalse(msgs[-1]["mine"])

        client_c = Client()
        client_c.post(
            reverse("chat:login"),
            {"login_code": "333333", "password": "pass-c"},
        )
        resp = client_c.get(
            reverse("chat:api_poll"), {"conversation_id": convo_id}
        )
        self.assertEqual(
            resp.json()["active"]["messages"][-1]["sender_name"], "Alpha Store"
        )

    def test_group_ticks_read_when_all_read(self):
        convo_id = self.create_group().json()["conversation"]["id"]
        self.client.post(
            reverse("chat:api_send", args=[convo_id]),
            data='{"body": "hi"}',
            content_type="application/json",
        )
        last_id = Message.objects.order_by("-id").first().pk
        for code, password in (
            ("222222", "pass-b"), ("333333", "pass-c")
        ):
            c = Client()
            c.post(
                reverse("chat:login"),
                {"login_code": code, "password": password},
            )
            c.post(
                reverse("chat:api_read", args=[convo_id]),
                data=json.dumps({"up_to_id": last_id}),
                content_type="application/json",
            )
        resp = self.client.get(
            reverse("chat:api_poll"), {"conversation_id": convo_id}
        )
        msgs = resp.json()["active"]["messages"]
        self.assertEqual(msgs[-1]["status"], "read")

    def test_group_send_rejected_for_non_member(self):
        convo_id = self.create_group(
            members=[self.shop_b.pk]
        ).json()["conversation"]["id"]
        client_c = Client()
        client_c.post(
            reverse("chat:login"),
            {"login_code": "333333", "password": "pass-c"},
        )
        resp = client_c.post(
            reverse("chat:api_send", args=[convo_id]),
            data='{"body": "intruder"}',
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_rename_group(self):
        convo_id = self.create_group().json()["conversation"]["id"]
        resp = self.client.post(
            reverse("chat:api_group_update", args=[convo_id]),
            data=json.dumps({"name": "Renamed branches"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["conversation"]["title"], "Renamed branches")
        self.assertTrue(data["conversation"]["can_manage"])
        note = Message.objects.filter(conversation_id=convo_id).order_by("-id")[0]
        self.assertEqual(note.kind, "system")
        self.assertIn("renamed the group", note.body)
        self.assertIn("Renamed branches", note.body)

    def test_rename_group_requires_name(self):
        convo_id = self.create_group().json()["conversation"]["id"]
        resp = self.client.post(
            reverse("chat:api_group_update", args=[convo_id]),
            data=json.dumps({"name": "  "}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_delete_group(self):
        convo_id = self.create_group().json()["conversation"]["id"]
        resp = self.client.post(
            reverse("chat:api_group_delete", args=[convo_id]),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(resp.json()["deleted_id"], convo_id)
        self.assertIsNone(
            services.get_member_conversation(convo_id, self.shop_a)
        )
        # Other members also lose the thread.
        self.assertIsNone(
            services.get_member_conversation(convo_id, self.shop_b)
        )

    def test_group_settings_rejected_for_non_member(self):
        convo_id = self.create_group(
            members=[self.shop_b.pk]
        ).json()["conversation"]["id"]
        client_c = Client()
        client_c.post(
            reverse("chat:login"),
            {"login_code": "333333", "password": "pass-c"},
        )
        resp = client_c.post(
            reverse("chat:api_group_update", args=[convo_id]),
            data=json.dumps({"name": "Hijacked"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)
        resp = client_c.post(
            reverse("chat:api_group_delete", args=[convo_id]),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)


class QuickStockinTests(ChatTestCase):
    """The `#` quick stock-in posts into the open chat: a pending card
    for a member of the posting shop to confirm with their staff code,
    which executes the stock-in and drops a "received" note in the chat."""

    EMPLOYEE_DDL = PendingConfirmTests.EMPLOYEE_DDL

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
        member_user = User.objects.create_user(
            username="member1", first_name="Member", last_name="One"
        )
        with connection.cursor() as cur:
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
        self.shop_c = Shop.objects.create(
            name="Gamma Foods", location="Kisumu", email="c@x.co",
            phone_number="0700000003", login_code="333333",
            password_hash=make_password("pass-c"), is_suspended=False,
            is_hidden=False,
        )
        self.login()
        self.convo = services.create_group(
            self.shop_a, "Branch group", [self.shop_b, self.shop_c]
        )

    def quickin(self, **over):
        data = {
            "lines": [
                {
                    "item_id": self.item.pk,
                    "quantity": "5",
                    "serials": [],
                    "buying_price": "90",
                }
            ],
        }
        data.update(over)
        return self.client.post(
            reverse("chat:api_quick_stockin", args=[self.convo.pk]),
            data=json.dumps(data),
            content_type="application/json",
        )

    def test_quick_stockin_posts_pending_card_to_chat(self):
        data = self.quickin().json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["pending"])
        self.assertEqual(data["conversation_id"], self.convo.pk)
        msg = Message.objects.get(pk=data["message"]["id"])
        self.assertEqual(msg.conversation_id, self.convo.pk)
        self.assertEqual(msg.sender_shop_id, self.shop_a.pk)
        pa = msg.meta["pending_action"]
        self.assertEqual(pa["action"], "stockin")
        self.assertEqual(pa["shop_id"], self.shop_a.pk)
        self.assertEqual(pa["shop_name"], "Alpha Store")
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_member_code_executes_and_posts_received(self):
        msg_id = self.quickin().json()["message"]["id"]
        resp = self.client.post(
            reverse("chat:api_pending_confirm", args=[msg_id]),
            data='{"staff_code": "654321"}',
            content_type="application/json",
        )
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertIn("note", data)

        # The card became the real stock-in card in place.
        msg = Message.objects.get(pk=msg_id)
        self.assertNotIn("pending_action", msg.meta)
        self.assertIn("stock_in", msg.meta)
        self.assertEqual(msg.meta["confirmed"]["by"], "Member One")

        # Stock moved under the confirming member's id.
        movement = StockMovement.objects.get()
        self.assertEqual(movement.shop_id, self.shop_a.pk)
        self.assertEqual(movement.created_by_id, 2)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal("15"))
        self.item.refresh_from_db()
        self.assertEqual(self.item.stock, Decimal("45"))

        # A "received" note landed in the chat from the action shop.
        note = Message.objects.get(pk=data["note"]["id"])
        self.assertEqual(note.conversation_id, self.convo.pk)
        self.assertEqual(note.sender_shop_id, self.shop_a.pk)
        self.assertIn("Stock received", note.body)
        self.assertIn("Member One", note.body)

    def test_other_shop_member_code_rejected(self):
        msg_id = self.quickin().json()["message"]["id"]
        client_b = Client()
        client_b.post(
            reverse("chat:login"),
            {"login_code": "222222", "password": "pass-b"},
        )
        resp = client_b.post(
            reverse("chat:api_pending_confirm", args=[msg_id]),
            data='{"staff_code": "999999"}',
            content_type="application/json",
        )
        self.assertFalse(resp.json()["ok"])
        self.assertIn("Alpha Store", resp.json()["error"])
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_typed_code_executes_immediately(self):
        data = self.quickin(staff_code="654321").json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["pending"])
        self.assertIn("stock_in", data["message"]["meta"])
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal("15"))

    def test_quickin_requires_lines(self):
        resp = self.quickin(lines=[])
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(Message.objects.count(), 1)  # just the system msg

    def test_quickin_rejected_for_non_member(self):
        outsider = Client()
        outsider.post(
            reverse("chat:login"),
            {"login_code": "333333", "password": "pass-c"},
        )
        # Shop C is in the group; a direct convo it isn't in must 404.
        solo = services.get_or_create_direct(self.shop_a, self.shop_b)
        resp = outsider.post(
            reverse("chat:api_quick_stockin", args=[solo.pk]),
            data=json.dumps({"lines": []}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)


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


class TradeTests(ChatTestCase):
    """Trade out / trade in mirror MY-SHOP's checkout and only run when the
    shop's effective POS settings enable them (company defaults merged with
    the shop's override row)."""

    EMPLOYEE_DDL = PendingConfirmTests.EMPLOYEE_DDL

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            for ddl in self.EMPLOYEE_DDL:
                cur.execute(ddl)
            cur.execute("DELETE FROM employees_employeeprofile")
            cur.execute(
                "DELETE FROM employees_employeeprofile_assigned_shops"
            )
            # Non-member manager (staff session) and a shop member whose
            # code completes stock actions.
            cur.execute(
                "INSERT INTO employees_employeeprofile "
                "(id, employee_id, user_id, role, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                [1, "100001", None, "company_manager", "active"],
            )
            cur.execute(
                "INSERT INTO employees_employeeprofile "
                "(id, employee_id, user_id, role, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                [2, "654321", None, "shop_attendant", "active"],
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
        self.login()

    def trade_payload(self, kind="trade_out", **over):
        data = {
            "kind": kind,
            "client_name": "Joe",
            "client_phone": "0712 345 678",
            "staff_code": "654321",
            "lines": [
                {"item_id": self.item.pk, "quantity": "2", "serials": []}
            ],
        }
        data.update(over)
        return data

    def post_trade(self, payload):
        return self.client.post(
            reverse("chat:api_sale"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_trade_out_moves_stock_and_writes_receipt(self):
        data = self.post_trade(self.trade_payload()).json()
        self.assertTrue(data["ok"])
        receipt = ShopReceipt.objects.get()
        self.assertEqual(receipt.kind, "trade_out")
        # Open trade — MY-SHOP settles it later via payment or exchange.
        self.assertEqual(receipt.status, "active")
        self.assertTrue(receipt.receipt_number.startswith("T"))
        self.assertEqual(receipt.client_name, "JOE")
        self.assertEqual(receipt.amount_paid, Decimal("0"))
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal("8"))
        movement = StockMovement.objects.get()
        self.assertEqual(movement.movement_type, "out")
        self.assertEqual(movement.entry_source, "trade_out")
        self.assertEqual(movement.created_by_id, 2)

    def test_trade_in_stocks_in_with_owed_price(self):
        data = self.post_trade(
            self.trade_payload(
                kind="trade_in",
                lines=[
                    {
                        "item_id": self.item.pk,
                        "quantity": "4",
                        "serials": [],
                        "buying_price": "70",
                    }
                ],
            )
        ).json()
        self.assertTrue(data["ok"])
        receipt = ShopReceipt.objects.get()
        self.assertEqual(receipt.kind, "trade_in")
        self.assertTrue(receipt.receipt_number.startswith("TI"))
        self.assertEqual(receipt.total, Decimal("280"))
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal("14"))
        movement = StockMovement.objects.get()
        self.assertEqual(movement.movement_type, "in")
        self.assertEqual(movement.entry_source, "trade_in")

    def test_trade_in_requires_owed_price(self):
        resp = self.post_trade(self.trade_payload(kind="trade_in"))
        self.assertFalse(resp.json()["ok"])
        self.assertIn("amount owed", resp.json()["error"])

    def test_trade_requires_client(self):
        resp = self.post_trade(self.trade_payload(client_name=""))
        self.assertFalse(resp.json()["ok"])
        resp = self.post_trade(self.trade_payload(client_phone=""))
        self.assertFalse(resp.json()["ok"])

    def test_trade_disabled_by_company_settings(self):
        CompanyPosSettings.objects.create(
            id=1, enable_trade_out=False, enable_trade_in=False
        )
        for kind in ("trade_out", "trade_in"):
            resp = self.post_trade(self.trade_payload(kind=kind))
            self.assertFalse(resp.json()["ok"])
            self.assertIn("disabled", resp.json()["error"])
        self.assertEqual(ShopReceipt.objects.count(), 0)

    def test_shop_override_disables_one_kind(self):
        CompanyPosSettings.objects.create(id=1)
        ShopPosSettings.objects.create(
            shop_id=self.shop_a.pk, override_pos=True,
            enable_trade_out=False, enable_trade_in=True,
        )
        resp = self.post_trade(self.trade_payload(kind="trade_out"))
        self.assertFalse(resp.json()["ok"])
        resp = self.post_trade(
            self.trade_payload(
                kind="trade_in",
                lines=[
                    {
                        "item_id": self.item.pk,
                        "quantity": "1",
                        "serials": [],
                        "buying_price": "70",
                    }
                ],
            )
        )
        self.assertTrue(resp.json()["ok"])

    def test_trade_fab_hidden_when_disabled(self):
        resp = self.client.get(reverse("chat:app"))
        self.assertContains(resp, 'data-fab="trade"')
        CompanyPosSettings.objects.create(
            id=1, enable_trade_out=False, enable_trade_in=False
        )
        resp = self.client.get(reverse("chat:app"))
        self.assertNotContains(resp, 'data-fab="trade"')

    def test_nonmember_trade_posts_pending_card(self):
        """Staff not allocated to the shop send trades for confirmation —
        same gate as stock-in and transfers."""
        session = self.client.session
        session["chatbot_employee_id"] = 1
        session.save()
        data = self.post_trade(self.trade_payload(staff_code="")).json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["pending"])
        self.assertEqual(ShopReceipt.objects.count(), 0)
        msg = Message.objects.order_by("-id").first()
        data = self.client.post(
            reverse("chat:api_pending_confirm", args=[msg.pk]),
            data=json.dumps({"staff_code": "654321"}),
            content_type="application/json",
        ).json()
        self.assertTrue(data["ok"])
        self.assertEqual(ShopReceipt.objects.get().kind, "trade_out")
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal("8"))


class ExpenseTests(ChatTestCase):
    """Expense registration mirrors MY-SHOP's register_shop_expense and is
    gated like stock-in/transfers for non-member staff."""

    EMPLOYEE_DDL = PendingConfirmTests.EMPLOYEE_DDL

    def setUp(self):
        super().setUp()
        with connection.cursor() as cur:
            for ddl in self.EMPLOYEE_DDL:
                cur.execute(ddl)
            cur.execute("DELETE FROM employees_employeeprofile")
            cur.execute(
                "DELETE FROM employees_employeeprofile_assigned_shops"
            )
            cur.execute(
                "INSERT INTO employees_employeeprofile "
                "(id, employee_id, user_id, role, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                [1, "100001", None, "company_manager", "active"],
            )
            cur.execute(
                "INSERT INTO employees_employeeprofile "
                "(id, employee_id, user_id, role, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                [2, "654321", None, "shop_attendant", "active"],
            )
            cur.execute(
                "INSERT INTO employees_employeeprofile_assigned_shops "
                "(employeeprofile_id, shop_id) VALUES (%s, %s)",
                [2, self.shop_a.pk],
            )
        self.login()

    def expense_payload(self, **over):
        data = {
            "category": "transport",
            "name": "Fuel",
            "amount": "500",
            "payment_status": "paid",
            "supplier_name": "Shell Station",
            "supplier_phone": "0712 345 678",
            "staff_code": "654321",
        }
        data.update(over)
        return data

    def post_expense(self, payload):
        return self.client.post(
            reverse("chat:api_expense"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_paid_expense_writes_row_and_supplier(self):
        data = self.post_expense(self.expense_payload()).json()
        self.assertTrue(data["ok"])
        expense = Expense.objects.get()
        self.assertEqual(expense.name, "FUEL")
        self.assertEqual(expense.category, "transport")
        self.assertEqual(expense.amount, Decimal("500.00"))
        self.assertEqual(expense.amount_paid, Decimal("500.00"))
        self.assertEqual(expense.cash_amount, Decimal("500.00"))
        self.assertEqual(expense.payment_status, "paid")
        self.assertEqual(expense.created_by_id, 2)
        supplier = ExpenseSupplier.objects.get()
        self.assertEqual(supplier.name, "SHELL STATION")
        self.assertEqual(expense.supplier_id, supplier.pk)

    def test_unpaid_expense_leaves_amount_paid_zero(self):
        data = self.post_expense(
            self.expense_payload(payment_status="unpaid")
        ).json()
        self.assertTrue(data["ok"])
        expense = Expense.objects.get()
        self.assertEqual(expense.amount_paid, Decimal("0"))
        self.assertEqual(expense.payment_status, "unpaid")

    def test_expense_requires_supplier_and_valid_fields(self):
        resp = self.post_expense(self.expense_payload(supplier_name=""))
        self.assertFalse(resp.json()["ok"])
        resp = self.post_expense(self.expense_payload(supplier_phone=""))
        self.assertFalse(resp.json()["ok"])
        resp = self.post_expense(self.expense_payload(category=""))
        self.assertFalse(resp.json()["ok"])
        resp = self.post_expense(self.expense_payload(amount="0"))
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(Expense.objects.count(), 0)

    def test_expense_requires_staff_code(self):
        resp = self.post_expense(self.expense_payload(staff_code=""))
        self.assertFalse(resp.json()["ok"])

    def test_nonmember_expense_posts_pending_card(self):
        session = self.client.session
        session["chatbot_employee_id"] = 1
        session.save()
        data = self.post_expense(self.expense_payload(staff_code="")).json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["pending"])
        self.assertEqual(Expense.objects.count(), 0)
        msg = Message.objects.order_by("-id").first()
        data = self.client.post(
            reverse("chat:api_pending_confirm", args=[msg.pk]),
            data=json.dumps({"staff_code": "654321"}),
            content_type="application/json",
        ).json()
        self.assertTrue(data["ok"])
        expense = Expense.objects.get()
        self.assertEqual(expense.created_by_id, 2)
        self.assertIn("expense", data["message"]["meta"])


class ActivityFeedTests(ChatTestCase):
    """Completed stock-in/transfers/trades/expenses also post a card into
    shared all-shops feed groups (Incoming stock / Transfers / Trades /
    Expenses) so everyone can track the day's activity — each feed card
    names the staff member who ran the action (`meta.by`)."""

    EMPLOYEE_DDL = PendingConfirmTests.EMPLOYEE_DDL

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
        member_a = User.objects.create_user(
            username="membera", first_name="Member", last_name="One"
        )
        member_b = User.objects.create_user(
            username="memberb", first_name="Beta", last_name="Member"
        )
        with connection.cursor() as cur:
            # Non-member manager (staff session) — only pending requests.
            cur.execute(
                "INSERT INTO employees_employeeprofile "
                "(id, employee_id, user_id, role, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                [1, "100001", None, "company_manager", "active"],
            )
            # Members of shop_a / shop_b whose codes complete actions.
            cur.execute(
                "INSERT INTO employees_employeeprofile "
                "(id, employee_id, user_id, role, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                [2, "654321", member_a.pk, "shop_attendant", "active"],
            )
            cur.execute(
                "INSERT INTO employees_employeeprofile_assigned_shops "
                "(employeeprofile_id, shop_id) VALUES (%s, %s)",
                [2, self.shop_a.pk],
            )
            cur.execute(
                "INSERT INTO employees_employeeprofile "
                "(id, employee_id, user_id, role, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                [3, "654322", member_b.pk, "shop_attendant", "active"],
            )
            cur.execute(
                "INSERT INTO employees_employeeprofile_assigned_shops "
                "(employeeprofile_id, shop_id) VALUES (%s, %s)",
                [3, self.shop_b.pk],
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
        self.login()

    def feed(self, activity):
        return Conversation.objects.get(
            kind=ConversationKind.GROUP,
            pair_key="activity:" + activity,
        )

    def post_stockin(self, **over):
        data = {
            "supplier_name": "ACME Traders",
            "supplier_phone": "0712 345 678",
            "staff_code": "654321",
            "lines": [
                {"item_id": self.item.pk, "quantity": "2", "serials": []}
            ],
        }
        data.update(over)
        return self.client.post(
            reverse("chat:api_stock_in"),
            data=json.dumps(data),
            content_type="application/json",
        )

    def test_stockin_posts_to_incoming_feed(self):
        resp = self.post_stockin()
        self.assertTrue(resp.json()["ok"])
        convo = self.feed("stockin")
        self.assertEqual(convo.name, "Incoming stock")
        # Every active shop rides along — the feed is the shared log.
        self.assertEqual(
            {p.shop_id for p in convo.participants.all()},
            {self.shop_a.pk, self.shop_b.pk},
        )
        msg = convo.messages.get()
        self.assertEqual(msg.sender_shop_id, self.shop_a.pk)
        self.assertIn("stock_in", msg.meta)
        self.assertEqual(msg.meta["by"], "Member One")
        self.assertIn("Stock received", msg.body)
        # The shop's own Assistant thread still gets its private card.
        bot = services.get_or_create_bot(self.shop_a)
        self.assertEqual(bot.messages.count(), 2)  # welcome + card

    def test_transfer_feed_card_and_resolution(self):
        resp = self.client.post(
            reverse("chat:api_transfers"),
            data=json.dumps(
                {
                    "destination_shop_id": self.shop_b.pk,
                    "staff_code": "654321",
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
        self.assertTrue(resp.json()["ok"])
        convo = self.feed("transfer")
        msg = convo.messages.get()
        self.assertEqual(msg.sender_shop_id, self.shop_a.pk)
        transfer = msg.meta["transfer"]
        self.assertTrue(transfer["readonly"])
        self.assertEqual(transfer["status"], "pending")
        self.assertEqual(msg.meta["by"], "Member One")
        movement_id = transfer["movement_id"]
        self.assertEqual(msg.client_tag, f"transfer-feed:{movement_id}")

        # The receiving shop accepts — the feed card resolves in place
        # and a note lands in the same group.
        client_b = Client()
        client_b.post(
            reverse("chat:login"),
            {"login_code": "222222", "password": "pass-b"},
        )
        line = StockMovementLine.objects.get(movement_id=movement_id)
        resp = client_b.post(
            reverse("chat:api_transfer_respond", args=[movement_id]),
            data=json.dumps(
                {
                    "decision": "accept",
                    "received": {str(line.pk): "3"},
                    "staff_code": "654322",
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["ok"])
        msg.refresh_from_db()
        transfer = msg.meta["transfer"]
        self.assertEqual(transfer["status"], "fulfilled")
        self.assertEqual(transfer["responded_by"], "Beta Member")
        note = convo.messages.order_by("-id").first()
        self.assertNotEqual(note.pk, msg.pk)
        self.assertEqual(note.sender_shop_id, self.shop_b.pk)
        self.assertIn("confirmed by Beta Member", note.body)

    def test_trade_posts_to_trade_feed(self):
        resp = self.client.post(
            reverse("chat:api_sale"),
            data=json.dumps(
                {
                    "kind": "trade_out",
                    "client_name": "Joe",
                    "client_phone": "0712 345 678",
                    "staff_code": "654321",
                    "lines": [
                        {
                            "item_id": self.item.pk,
                            "quantity": "2",
                            "serials": [],
                        }
                    ],
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["ok"])
        msg = self.feed("trade").messages.get()
        self.assertEqual(msg.sender_shop_id, self.shop_a.pk)
        self.assertEqual(msg.meta["sale"]["kind"], "trade_out")
        self.assertEqual(msg.meta["by"], "Member One")

    def test_expense_posts_to_expense_feed(self):
        resp = self.client.post(
            reverse("chat:api_expense"),
            data=json.dumps(
                {
                    "category": "transport",
                    "name": "Fuel",
                    "amount": "500",
                    "payment_status": "paid",
                    "supplier_name": "Shell Station",
                    "supplier_phone": "0712 345 678",
                    "staff_code": "654321",
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["ok"])
        convo = self.feed("expense")
        msg = convo.messages.get()
        self.assertIn("expense", msg.meta)
        self.assertEqual(msg.meta["by"], "Member One")
        self.assertIn("FUEL", msg.body)

    def test_pending_confirm_posts_to_feed(self):
        """A request confirmed by a member still lands in the shared
        feed — named for the member who confirmed it."""
        session = self.client.session
        session["chatbot_employee_id"] = 1
        session.save()
        resp = self.post_stockin(staff_code="")
        self.assertTrue(resp.json()["pending"])
        msg = Message.objects.order_by("-id").first()
        resp = self.client.post(
            reverse("chat:api_pending_confirm", args=[msg.pk]),
            data=json.dumps({"staff_code": "654321"}),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["ok"])
        feed_msg = self.feed("stockin").messages.get()
        self.assertIn("stock_in", feed_msg.meta)
        self.assertEqual(feed_msg.meta["by"], "Member One")
        self.assertEqual(feed_msg.sender_shop_id, self.shop_a.pk)

    def test_feed_group_is_separate_from_user_groups(self):
        # A user group may share the name — the feed is keyed, not named.
        user_group = services.create_group(
            self.shop_a, "Expenses", [self.shop_b]
        )
        self.client.post(
            reverse("chat:api_expense"),
            data=json.dumps(
                {
                    "category": "transport",
                    "name": "Fuel",
                    "amount": "500",
                    "payment_status": "paid",
                    "supplier_name": "Shell Station",
                    "supplier_phone": "0712 345 678",
                    "staff_code": "654321",
                }
            ),
            content_type="application/json",
        )
        feed = self.feed("expense")
        self.assertNotEqual(feed.pk, user_group.pk)
        self.assertEqual(user_group.messages.count(), 1)  # system msg only
