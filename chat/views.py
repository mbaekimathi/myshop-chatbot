"""Pages + JSON polling API for MY-CHATBOT."""

import json
import logging
import mimetypes
import os
import re

from django.core.cache import cache
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import bot as bot_engine
from . import services
from . import stock as stock_ops
from .auth import (
    api_shop_required,
    authenticate_employee,
    authenticate_shop,
    current_employee,
    current_shop,
    employee_allocated_to_shop,
    login_employee,
    login_shop,
    logout_shop,
    shop_required,
)
from .models import Conversation, ConversationKind, Message, Participant
from .myshop_models import CompanyProfile, Shop

logger = logging.getLogger(__name__)

HISTORY_PAGE_SIZE = 40
LOGIN_MAX_ATTEMPTS = 8
LOGIN_WINDOW_SECONDS = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_body(request) -> dict:
    if request.content_type and "json" in request.content_type:
        try:
            return json.loads(request.body or b"{}")
        except (ValueError, TypeError):
            return {}
    return request.POST.dict()


def _err(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"ok": False, "error": message}, status=status)


def _needs_member_confirmation(request, shop) -> bool:
    """Staff acting on a shop they aren't allocated to need a member's code.

    Employees signed in through the staff portal (admin / manager / IT
    support) can operate any shop via the picker. When the acting employee
    isn't allocated to the shop, stock-in and transfer only proceed after a
    member of that shop confirms by entering their own staff code. Shop
    portal sessions act as the shop itself, so they skip this check.
    """
    emp = current_employee(request)
    if emp is None:
        return False
    return not employee_allocated_to_shop(emp["id"], shop.pk)


def _client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _company_name() -> str:
    try:
        row = CompanyProfile.objects.first()
        if row and row.name.strip():
            return row.name.strip()
    except Exception:
        pass
    return "MY-SHOP"


def _participants(convo: Conversation, me: Shop):
    me_part = peer = None
    for p in convo.participants.all():
        if p.shop_id == me.pk:
            me_part = p
        else:
            peer = p
    return me_part, peer


def _serialize_active(convo: Conversation, me: Shop, after_id: int = 0) -> dict:
    me_part, peer = _participants(convo, me)
    qs = convo.messages.select_related().order_by("id")
    if after_id:
        qs = qs.filter(id__gt=after_id)
    msgs = list(qs[:400])
    peer_shop = services.shop_by_id(peer.shop_id) if peer else None
    bot_thread = convo.kind == ConversationKind.BOT
    data = {
        "id": convo.pk,
        "kind": convo.kind,
        "messages": [
            services.serialize_message(m, me, peer, bot_thread) for m in msgs
        ],
        "peer": services.serialize_peer(peer, peer_shop),
        "suggestions": bot_engine.BOT_SUGGESTIONS
        if convo.kind == ConversationKind.BOT
        else [],
    }
    if convo.kind == ConversationKind.BOT:
        data["peer"] = {
            "online": True,
            "typing": False,
            "last_seen": "",
            "name": services.BOT_NAME,
            "location": services.BOT_SUBTITLE,
            "read_up_to": 0,
        }
    return data


def _conversations_payload(me: Shop) -> list:
    services.get_or_create_bot(me)  # assistant thread always exists
    convos = list(services.my_conversations(me))
    shops_map = services.shops_map_for(convos)
    return [services.serialize_conversation(c, me, shops_map) for c in convos]


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def home(request):
    if current_shop(request) is not None:
        return redirect("chat:app")
    return redirect("chat:login")


def login_view(request):
    if request.method == "GET":
        if current_shop(request) is not None:
            return redirect("chat:app")
        if current_employee(request) is not None:
            return redirect("chat:all_shops")

    error = ""
    code = ""
    mode = "shop"
    if request.method == "POST":
        mode = "employee" if request.POST.get("mode") == "employee" else "shop"
        code = re.sub(r"\D+", "", request.POST.get("login_code") or "")[:6]
        password = request.POST.get("password") or ""
        key = f"chatbot:login:{_client_ip(request)}"
        attempts = cache.get(key, 0)
        if attempts >= LOGIN_MAX_ATTEMPTS:
            error = "Too many attempts. Wait a minute and try again."
        elif len(code) != 6 or not password:
            error = "Enter the 6-digit code and password."
        elif mode == "employee":
            employee = authenticate_employee(code, password)
            if employee is None:
                cache.set(key, attempts + 1, LOGIN_WINDOW_SECONDS)
                error = (
                    "Invalid staff ID or password. Only admin, manager "
                    "and IT support accounts can sign in here."
                )
            else:
                cache.delete(key)
                request.session.cycle_key()
                login_employee(request, employee)
                return redirect("chat:all_shops")
        else:
            shop = authenticate_shop(code, password)
            if shop is None:
                cache.set(key, attempts + 1, LOGIN_WINDOW_SECONDS)
                error = "Invalid shop code or password. Please try again."
            else:
                cache.delete(key)
                request.session.cycle_key()
                login_shop(request, shop)
                next_url = request.GET.get("next") or request.POST.get("next") or ""
                if next_url.startswith("/"):
                    return redirect(next_url)
                return redirect("chat:app")

    return render(
        request,
        "chat/login.html",
        {
            "error": error,
            "login_code": code,
            "mode": mode,
            "company_name": _company_name(),
        },
    )


def _shop_overview():
    """Per-shop today aggregates for the employee all-shops dashboard."""
    from django.db.models import Count, F, Sum
    from decimal import Decimal
    from .myshop_models import Expense, ShopReceipt

    today = timezone.localdate()
    start_dt, end_dt = bot_engine._window(today, today)

    sales = {
        r["shop_id"]: r
        for r in ShopReceipt.objects.filter(
            kind=bot_engine.SALE_KIND,
            status__in=bot_engine.COUNTED_STATUSES,
            created_at__gte=start_dt,
            created_at__lt=end_dt,
        )
        .values("shop_id")
        .annotate(t=Sum("total"), c=Count("id"))
    }
    expenses = {
        r["shop_id"]: r["t"]
        for r in Expense.objects.filter(
            created_at__gte=start_dt, created_at__lt=end_dt
        )
        .values("shop_id")
        .annotate(t=Sum("amount"))
    }
    credits = {
        r["shop_id"]: r["t"]
        for r in (
            ShopReceipt.objects.filter(
                kind=bot_engine.CREDIT_KIND,
                status__in=bot_engine.COUNTED_STATUSES,
            )
            .annotate(balance=F("total") - F("amount_paid"))
            .filter(balance__gt=0)
            .values("shop_id")
            .annotate(t=Sum("balance"))
        )
    }

    rows = []
    totals = {"sales": Decimal("0"), "expenses": Decimal("0"),
              "credit": Decimal("0"), "receipts": 0}
    for shop in services.active_shops():
        s = sales.get(shop.pk, {})
        row = {
            "shop": shop,
            "sales": s.get("t") or 0,
            "receipts": s.get("c") or 0,
            "expenses": expenses.get(shop.pk) or 0,
            "credit": credits.get(shop.pk) or 0,
        }
        totals["sales"] += row["sales"]
        totals["receipts"] += row["receipts"]
        totals["expenses"] += row["expenses"]
        totals["credit"] += row["credit"]
        rows.append(row)
    return rows, totals


@require_POST
def pick_shop(request):
    """Employee-only shop switcher — the /all/ sidebar posts here."""
    employee = current_employee(request)
    if employee is None:
        return redirect("chat:login")

    if str(request.POST.get("shop_id")) == "all":
        request.session["chatbot_all_shops"] = True
        request.session.pop("chatbot_shop_id", None)
        return redirect("chat:all_shops")
    try:
        shop = services.shop_by_id(int(request.POST.get("shop_id") or 0))
    except (TypeError, ValueError):
        shop = None
    if shop is None:
        return redirect("chat:all_shops")
    request.session.pop("chatbot_all_shops", None)
    login_shop(request, shop)
    fab = (request.POST.get("fab") or "").strip()
    if fab in {"sale", "stockin", "transfer"}:
        return redirect(f"{reverse('chat:app')}?fab={fab}")
    return redirect("chat:app")


def all_shops_view(request):
    """Employee-only combined assistant view across every shop."""
    employee = current_employee(request)
    if employee is None:
        return redirect("chat:login")
    # Landing here means combined view — drop any single-shop session.
    request.session["chatbot_all_shops"] = True
    request.session.pop("chatbot_shop_id", None)
    rows, totals = _shop_overview()
    for row in rows:
        row["sales_fmt"] = bot_engine.money(row["sales"])
        row["expenses_fmt"] = bot_engine.money(row["expenses"])
        row["credit_fmt"] = bot_engine.money(row["credit"])
    shops = [
        {
            "id": s.pk,
            "name": s.name,
            "location": s.location,
            "initials": services.initials(s.name),
            "tone": services.avatar_tone(s.pk),
        }
        for s in services.active_shops()
    ]
    return render(
        request,
        "chat/all.html",
        {
            "employee": employee,
            "company_name": _company_name(),
            "shops": shops,
            "shop_rows": rows,
            "totals": {
                "sales": bot_engine.money(totals["sales"]),
                "receipts": totals["receipts"],
                "expenses": bot_engine.money(totals["expenses"]),
                "credit": bot_engine.money(totals["credit"]),
            },
        },
    )


@require_POST
def api_all_chat(request):
    """POST {text} → {reply} — the all-shops assistant."""
    if current_employee(request) is None:
        return JsonResponse(
            {"ok": False, "error": "authentication required"}, status=401
        )
    data = _json_body(request)
    text = (data.get("text") or "").strip()
    if not text:
        return JsonResponse({"ok": False, "error": "Empty message."})
    return JsonResponse(
        {
            "ok": True,
            "reply": bot_engine.reply_for_all(text),
            "card": bot_engine.card_for_all(text),
        }
    )


@require_POST
def logout_view(request):
    logout_shop(request)
    return redirect("chat:login")


@shop_required
def app_view(request):
    me = request.chat_shop
    contacts = [
        {
            "id": s.pk,
            "name": s.name,
            "location": s.location,
            "phone": s.phone_number,
            "initials": services.initials(s.name),
            "tone": services.avatar_tone(s.pk),
        }
        for s in services.active_shops().exclude(pk=me.pk)
    ]
    context = {
        "company_name": _company_name(),
        "me": {
            "id": me.pk,
            "name": me.name,
            "location": me.location,
            "code": me.login_code,
            "initials": services.initials(me.name),
            "tone": services.avatar_tone(me.pk),
            "staff_view": current_employee(request) is not None,
            "needs_member_confirm": _needs_member_confirmation(request, me),
        },
        "conversations_json": json.dumps(_conversations_payload(me)),
        "contacts_json": json.dumps(contacts),
        "suggestions_json": json.dumps(bot_engine.BOT_SUGGESTIONS),
    }
    return render(request, "chat/app.html", context)


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


@api_shop_required
@require_GET
def api_poll(request):
    """Combined heartbeat: presence touch + conversation list + active thread."""
    me = request.chat_shop
    convo_id = request.GET.get("conversation_id") or ""
    after_id = request.GET.get("after_id") or "0"
    try:
        after_id = int(after_id)
    except (TypeError, ValueError):
        after_id = 0

    active = None
    if convo_id:
        convo = services.get_member_conversation(convo_id, me)
        if convo is None:
            return _err("Conversation not found.", 404)
        active = _serialize_active(convo, me, after_id=after_id)

    # One small UPDATE marks this device online in every thread we share.
    Participant.objects.filter(shop_id=me.pk).update(
        last_seen_at=timezone.now()
    )

    return JsonResponse(
        {
            "ok": True,
            "now": timezone.now().isoformat(),
            "conversations": _conversations_payload(me),
            "active": active,
        }
    )


@api_shop_required
@require_GET
def api_history(request, conversation_id):
    me = request.chat_shop
    convo = services.get_member_conversation(conversation_id, me)
    if convo is None:
        return _err("Conversation not found.", 404)
    try:
        before_id = int(request.GET.get("before_id") or 0)
    except (TypeError, ValueError):
        before_id = 0
    me_part, peer = _participants(convo, me)
    qs = convo.messages.order_by("-id")
    if before_id:
        qs = qs.filter(id__lt=before_id)
    msgs = list(qs[: HISTORY_PAGE_SIZE + 1])
    has_more = len(msgs) > HISTORY_PAGE_SIZE
    msgs = msgs[:HISTORY_PAGE_SIZE]
    msgs.reverse()
    return JsonResponse(
        {
            "ok": True,
            "has_more": has_more,
            "messages": [
                services.serialize_message(
                    m, me, peer, convo.kind == ConversationKind.BOT
                )
                for m in msgs
            ],
        }
    )


@api_shop_required
@require_POST
def api_open(request):
    me = request.chat_shop
    data = _json_body(request)
    target = (data.get("target") or "").strip()

    if target == "assistant":
        convo = services.get_or_create_bot(me)
    else:
        try:
            other_id = int(target)
        except (TypeError, ValueError):
            return _err("Pick a shop to chat with.")
        if other_id == me.pk:
            return _err("You cannot chat with your own shop.")
        other = services.shop_by_id(other_id)
        if other is None:
            return _err("That shop is not available.")
        convo = services.get_or_create_direct(me, other)

    me_part, _peer = _participants(convo, me)
    if me_part:
        services.touch_presence(me_part)
    shops_map = services.shops_map_for([convo])
    return JsonResponse(
        {
            "ok": True,
            "conversation": services.serialize_conversation(convo, me, shops_map),
            "active": _serialize_active(convo, me),
        }
    )


@api_shop_required
@require_POST
def api_send(request, conversation_id):
    me = request.chat_shop
    convo = services.get_member_conversation(conversation_id, me)
    if convo is None:
        return _err("Conversation not found.", 404)
    me_part, peer = _participants(convo, me)
    if me_part:
        services.touch_presence(me_part)

    upload = request.FILES.get("image")
    if upload is not None:
        msg, error = services.post_image(convo, me, upload)
    else:
        data = _json_body(request)
        msg, error = services.post_text(
            convo, me, data.get("body") or "", data.get("client_tag") or ""
        )
    if error:
        return _err(error)

    # Mark my own messages read on my side.
    if me_part:
        services.mark_read(me_part, msg.pk)

    reply_msg = None
    if convo.kind == ConversationKind.BOT and upload is None:
        reply_msg = services.post_bot_reply(
            convo,
            bot_engine.reply_for(me, msg.body),
            bot_engine.card_meta(me, msg.body),
        )
    elif convo.kind == ConversationKind.BOT and upload is not None:
        reply_msg = services.post_bot_reply(
            convo,
            "I can only read text for now — ask me things like *sales today* "
            "or *low stock*.",
        )

    bot_thread = convo.kind == ConversationKind.BOT
    payload = {
        "ok": True,
        "message": services.serialize_message(msg, me, peer, bot_thread),
    }
    if reply_msg is not None:
        payload["reply"] = services.serialize_message(
            reply_msg, me, peer, bot_thread
        )
    return JsonResponse(payload)


@api_shop_required
@require_POST
def api_read(request, conversation_id):
    me = request.chat_shop
    convo = services.get_member_conversation(conversation_id, me)
    if convo is None:
        return _err("Conversation not found.", 404)
    me_part, _peer = _participants(convo, me)
    if me_part is None:
        return _err("Not a participant.", 403)
    data = _json_body(request)
    try:
        up_to_id = int(data.get("up_to_id") or 0)
    except (TypeError, ValueError):
        up_to_id = 0
    latest = convo.messages.order_by("-id").values_list("id", flat=True).first() or 0
    services.mark_read(me_part, min(up_to_id or latest, latest))
    return JsonResponse({"ok": True})


@api_shop_required
@require_POST
def api_typing(request, conversation_id):
    me = request.chat_shop
    convo = services.get_member_conversation(conversation_id, me)
    if convo is None:
        return _err("Conversation not found.", 404)
    if convo.kind == ConversationKind.BOT:
        return JsonResponse({"ok": True})
    me_part, _peer = _participants(convo, me)
    if me_part is None:
        return _err("Not a participant.", 403)
    services.set_typing(me_part)
    return JsonResponse({"ok": True})


@api_shop_required
@require_GET
def api_item_search(request):
    """Live item suggestions for transfer / stock-in pickers."""
    me = request.chat_shop
    return JsonResponse(
        {"ok": True, "items": stock_ops.item_search(me, request.GET.get("q") or "")}
    )


@api_shop_required
@require_GET
def api_serial_search(request):
    """Live serial-number suggestions (in-stock at the signed-in shop)."""
    me = request.chat_shop
    return JsonResponse(
        {
            "ok": True,
            "serials": stock_ops.serial_search(
                me,
                request.GET.get("item_id"),
                request.GET.get("q") or "",
            ),
        }
    )


@api_shop_required
@require_GET
def api_supplier_search(request):
    """Live supplier suggestions (name or phone) for stock-in."""
    return JsonResponse(
        {
            "ok": True,
            "suppliers": stock_ops.supplier_search(request.GET.get("q") or ""),
        }
    )


@api_shop_required
@require_POST
def api_transfer(request, conversation_id):
    """Create a pending stock transfer to the chat peer (or a chosen shop).

    Mirrors MY-SHOP's transfer request: stock moves only when the receiving
    shop confirms it in MY-SHOP. The card message is the instant notice;
    `supplier_notified=False` flags it in their pending stock requests.
    """
    me = request.chat_shop
    convo = services.get_member_conversation(conversation_id, me)
    if convo is None:
        return _err("Conversation not found.", 404)
    if convo.kind != ConversationKind.DIRECT:
        return _err("Transfers can only be sent in a shop chat.")
    me_part, peer = _participants(convo, me)

    data = _json_body(request)
    dest_id = data.get("destination_shop_id")
    if dest_id:
        destination = services.shop_by_id(dest_id)
    else:
        destination = services.shop_by_id(peer.shop_id) if peer else None

    if _needs_member_confirmation(request, me) and not (
        data.get("staff_code") or ""
    ).strip():
        if destination is not None:
            data = dict(data)
            data["destination_shop_id"] = destination.pk
        return _post_pending(request, me, "transfer", data)

    try:
        dest_convo, msg = _create_transfer(
            me, destination, data,
            require_member=_needs_member_confirmation(request, me),
        )
    except stock_ops.StockError as exc:
        return _err(str(exc))

    payload = {"ok": True, "conversation_id": dest_convo.pk}
    if dest_convo.pk == convo.pk:
        payload["message"] = services.serialize_message(msg, me, peer)
    return JsonResponse(payload)


def _create_transfer(me, destination, data, require_member=False):
    """Shared transfer creation — returns (dest_convo, card_message)."""
    movement, lines = stock_ops.create_transfer(
        me, destination, data.get("lines") or [],
        staff_code=data.get("staff_code") or "",
        require_member=require_member,
    )
    movement_lines = list(
        stock_ops.StockMovementLine.objects.filter(
            movement_id=movement.pk
        ).order_by("pk")
    )
    summary = ", ".join(
        f"{line['quantity']}× {line['item'].name}" for line in lines
    )
    body = f"Stock transfer → {destination.name}: {summary}"
    card = stock_ops.transfer_card(movement, me, destination, movement_lines)
    # Post the notification into the conversation with the actual receiving
    # shop — creating it if needed — so they are always notified, even when
    # the dropdown destination differs from the current chat peer.
    dest_convo = services.get_or_create_direct(me, destination)
    msg = services.post_card(dest_convo, me, body, card)
    # Tag lets us find this card again when the transfer is responded to.
    msg.client_tag = f"transfer:{movement.pk}"
    msg.save(update_fields=["client_tag"])
    dest_me_part, _ = _participants(dest_convo, me)
    if dest_me_part:
        services.mark_read(dest_me_part, msg.pk)
        services.touch_presence(dest_me_part)
    return dest_convo, msg


@api_shop_required
@require_POST
def api_transfers(request):
    """Conversation-free transfer — used from the chat list page FAB."""
    me = request.chat_shop
    data = _json_body(request)
    destination = services.shop_by_id(data.get("destination_shop_id"))
    if destination is None:
        return _err("Select a shop to transfer to.")
    if _needs_member_confirmation(request, me) and not (
        data.get("staff_code") or ""
    ).strip():
        return _post_pending(request, me, "transfer", data)
    try:
        dest_convo, msg = _create_transfer(
            me, destination, data,
            require_member=_needs_member_confirmation(request, me),
        )
    except stock_ops.StockError as exc:
        return _err(str(exc))
    return JsonResponse(
        {
            "ok": True,
            "conversation_id": dest_convo.pk,
            "message": services.serialize_message(
                msg, me, _participants(dest_convo, me)[1]
            ),
        }
    )


@api_shop_required
@require_POST
def api_transfer_respond(request, movement_id):
    """Accept (with received quantities) or decline a pending transfer.

    Mirrors MY-SHOP's respond_to_stock_request — stock only moves on accept
    when every received quantity matches what was sent.
    """
    me = request.chat_shop
    data = _json_body(request)
    try:
        movement = stock_ops.respond_to_transfer(
            me,
            movement_id,
            data.get("decision") or "",
            data.get("received") or {},
            data.get("reason") or "",
            data.get("serials") or {},
        )
    except stock_ops.StockError as exc:
        return _err(str(exc))

    # Update the original card message so the status persists on reload.
    card_msg = Message.objects.filter(
        client_tag=f"transfer:{movement.pk}"
    ).order_by("id").first()
    convo = card_msg.conversation if card_msg else None
    if card_msg is not None:
        meta = dict(card_msg.meta or {})
        transfer = dict(meta.get("transfer") or {})
        transfer["status"] = movement.request_status
        if movement.response_note:
            transfer["response_note"] = movement.response_note
        meta["transfer"] = transfer
        card_msg.meta = meta
        card_msg.save(update_fields=["meta"])

    # Post a follow-up note both sides can see.
    note = None
    if convo is not None:
        if movement.request_status == "fulfilled":
            text = "Transfer confirmed — stock moved to " + me.name + "."
        else:
            text = "Transfer declined: " + (movement.response_note or "")
        note = services.post_card(convo, me, text, {})

    me_part, peer = _participants(convo, me) if convo else (None, None)
    if me_part:
        services.mark_read(me_part, note.pk if note else movement.pk)
    payload = {
        "ok": True,
        "status": movement.request_status,
        "card_message_id": card_msg.pk if card_msg else None,
        "card_meta": card_msg.meta if card_msg else None,
    }
    if note is not None:
        payload["message"] = services.serialize_message(note, me, peer)
    return JsonResponse(payload)


def _post_pending(request, me, action: str, data: dict) -> JsonResponse:
    """Dry-run the action and post a pending-confirmation card.

    Non-member staff can't complete stock actions directly — the validated
    payload is stored on a card in the shop's Assistant thread until a
    member of that shop confirms with their staff code.
    """
    emp = current_employee(request)
    payload = {
        k: v
        for k, v in data.items()
        if k not in ("staff_code", "csrfmiddlewaretoken")
    }
    try:
        body, meta = stock_ops.build_pending(
            action,
            me,
            payload,
            requested_by=stock_ops.employee_label(emp["id"]) if emp else "",
            requested_by_id=emp["id"] if emp else None,
        )
    except stock_ops.StockError as exc:
        return _err(str(exc))
    convo = services.get_or_create_bot(me)
    msg = services.post_bot_reply(convo, body, meta)
    me_part, _ = _participants(convo, me)
    if me_part:
        services.mark_read(me_part, msg.pk)
    return JsonResponse(
        {
            "ok": True,
            "pending": True,
            "message": services.serialize_message(msg, me, None, True),
            "conversation_id": convo.pk,
        }
    )


@api_shop_required
@require_POST
def api_pending_confirm(request, message_id):
    """A shop member's staff code executes a stored pending request.

    The pending card becomes the real receipt/movement card in place; for
    transfers the destination card is also posted into the direct
    conversation so the receiving shop is notified.
    """
    me = request.chat_shop
    msg = (
        Message.objects.filter(
            pk=message_id,
            conversation__participants__shop_id=me.pk,
        )
        .select_related("conversation")
        .first()
    )
    pa = (msg.meta or {}).get("pending_action") if msg else None
    if msg is None or not isinstance(pa, dict):
        return _err("Request not found.", 404)
    if pa.get("status") != "pending":
        return _err("This request was already handled.")

    data = _json_body(request)
    emp_id = stock_ops.member_employee_id(
        data.get("staff_code") or "", me
    )
    if emp_id is None:
        return _err(
            "Only a member of this shop can confirm — "
            "ask a shop staff member to enter their code."
        )
    try:
        result = stock_ops.execute_pending(me, pa, emp_id)
    except stock_ops.StockError as exc:
        return _err(str(exc))

    meta = dict(msg.meta or {})
    meta.pop("pending_action", None)
    meta.update(result["meta"])
    meta["confirmed"] = {"by": stock_ops.employee_label(emp_id)}
    msg.meta = meta
    msg.body = result["body"]
    msg.save(update_fields=["meta", "body"])

    payload = {
        "ok": True,
        "message": services.serialize_message(msg, me, None, True),
    }
    if result["action"] == "transfer":
        destination = result["destination"]
        dest_convo = services.get_or_create_direct(me, destination)
        card_msg = services.post_card(
            dest_convo, me, result["body"], result["meta"]
        )
        card_msg.client_tag = f"transfer:{result['movement'].pk}"
        card_msg.save(update_fields=["client_tag"])
        dest_me_part, dest_peer = _participants(dest_convo, me)
        if dest_me_part:
            services.mark_read(dest_me_part, card_msg.pk)
            services.touch_presence(dest_me_part)
        payload["card"] = services.serialize_message(
            card_msg, me, dest_peer
        )
        payload["card_conversation_id"] = dest_convo.pk
    return JsonResponse(payload)


@api_shop_required
@require_POST
def api_stock_in(request):
    """Record stock bought into the signed-in shop (immediate movement).

    Supplier name + phone are required — the row is upserted into
    items_supplier so it auto-suggests next time. The confirmation card is
    posted privately to the shop's Assistant thread.
    """
    me = request.chat_shop
    data = _json_body(request)
    supplier_name = (data.get("supplier_name") or "").strip()
    supplier_phone = (data.get("supplier_phone") or "").strip()
    if _needs_member_confirmation(request, me) and not (
        data.get("staff_code") or ""
    ).strip():
        return _post_pending(request, me, "stockin", data)
    try:
        movement, lines = stock_ops.create_stock_in(
            me,
            data.get("lines") or [],
            supplier_name=supplier_name,
            supplier_phone=supplier_phone,
            staff_code=data.get("staff_code") or "",
            require_member=_needs_member_confirmation(request, me),
        )
    except stock_ops.StockError as exc:
        return _err(str(exc))

    convo = services.get_or_create_bot(me)
    summary = ", ".join(
        f"{line['quantity']}× {line['item'].name}" for line in lines
    )
    body = f"Stock received: {summary} — {supplier_name.upper()}"
    card = stock_ops.stock_in_card(movement, me, lines, supplier_name.upper())
    msg = services.post_bot_reply(convo, body, card)
    me_part, _peer = _participants(convo, me)
    if me_part:
        services.mark_read(me_part, msg.pk)
    return JsonResponse(
        {
            "ok": True,
            "message": services.serialize_message(msg, me, None, True),
            "conversation_id": convo.pk,
        }
    )


@api_shop_required
@require_POST
def api_sale(request):
    """Record a sale — mirrors MY-SHOP's checkout (receipt + stock + serials).

    The receipt card is posted privately to the shop's Assistant thread.
    """
    me = request.chat_shop
    data = _json_body(request)
    if _needs_member_confirmation(request, me) and not (
        data.get("staff_code") or ""
    ).strip():
        return _post_pending(request, me, "sale", data)
    due = None
    if data.get("credit_due_date"):
        try:
            from datetime import date

            due = date.fromisoformat(str(data["credit_due_date"])[:10])
        except (TypeError, ValueError):
            return _err("Enter a valid payment due date.")
    try:
        receipt, lines = stock_ops.create_sale(
            me,
            data.get("lines") or [],
            kind=(data.get("kind") or "sale").strip().lower(),
            payment_method=data.get("payment_method") or "",
            cash_amount=data.get("cash_amount"),
            mpesa_amount=data.get("mpesa_amount"),
            client_name=data.get("client_name") or "",
            client_phone=data.get("client_phone") or "",
            credit_due_date=due,
            staff_code=data.get("staff_code") or "",
            require_member=_needs_member_confirmation(request, me),
        )
    except stock_ops.StockError as exc:
        return _err(str(exc))

    convo = services.get_or_create_bot(me)
    total = sum((l["line_total"] for l in lines))
    label = receipt.kind.capitalize()
    body = (
        f"{label} {receipt.receipt_number} — {len(lines)} item"
        f"{'s' if len(lines) != 1 else ''}, KSh {total:,.2f}"
    )
    card = stock_ops.sale_card(receipt, lines, total)
    msg = services.post_bot_reply(convo, body, card)
    me_part, _ = _participants(convo, me)
    if me_part:
        services.mark_read(me_part, msg.pk)
    return JsonResponse(
        {
            "ok": True,
            "receipt": receipt.receipt_number,
            "message": services.serialize_message(msg, me, None, True),
            "conversation_id": convo.pk,
        }
    )


@api_shop_required
@require_GET
def api_client_search(request):
    """Live client suggestions (name or phone) for checkout."""
    return JsonResponse(
        {
            "ok": True,
            "clients": stock_ops.client_search(request.GET.get("q") or ""),
        }
    )


@api_shop_required
@require_GET
def api_attachment(request, message_id):
    """Stream a message image — only to participants of that conversation."""
    me = request.chat_shop
    try:
        msg = Message.objects.select_related("conversation").get(pk=message_id)
    except Message.DoesNotExist:
        raise Http404
    if not msg.conversation.participants.filter(shop_id=me.pk).exists():
        raise Http404
    if not msg.attachment:
        raise Http404
    try:
        path = msg.attachment.path
    except (ValueError, NotImplementedError):
        raise Http404
    if not os.path.exists(path):
        raise Http404
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    response = FileResponse(open(path, "rb"), content_type=content_type)
    response["Cache-Control"] = "private, max-age=86400"
    return response
