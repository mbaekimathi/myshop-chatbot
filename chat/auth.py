"""Shop session auth — same credentials as the MY-SHOP shop portal.

A shop signs in with its shared 6-digit `login_code` and password (the hash
lives in `shops_shop.password_hash`, written by MY-SHOP with Django's
hasher). Verified sessions store the shop id under a namespaced session key
independent of any MY-SHOP cookies.
"""

import re
from functools import wraps

from django.contrib.auth.hashers import check_password, make_password
from django.http import JsonResponse
from django.shortcuts import redirect

from .myshop_models import Shop

SESSION_SHOP_KEY = "chatbot_shop_id"
SESSION_EMPLOYEE_KEY = "chatbot_employee_id"
LOGIN_CODE_RE = re.compile(r"^\d{6}$")

# Roles allowed to sign in with an employee account — these see all shops.
EMPLOYEE_ROLES = {
    "super_admin",
    "company_manager",
    "shop_manager",
    "store_manager",
    "it_support",
}

_DUMMY_HASH = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = make_password("chatbot-dummy-not-a-real-secret")
    return _DUMMY_HASH


def authenticate_shop(login_code: str, password: str):
    """Return the Shop on valid credentials, else None.

    Suspended/hidden shops cannot sign in. A dummy check runs when the code
    is unknown so response timing does not reveal which codes exist.
    """
    code = re.sub(r"\D+", "", login_code or "")
    password = password or ""
    if not LOGIN_CODE_RE.match(code) or not password:
        return None
    shop = Shop.objects.filter(login_code=code).first()
    if shop is None or shop.is_hidden or shop.is_suspended:
        check_password(password, shop.password_hash if shop else _dummy_hash())
        return None
    if not check_password(password, shop.password_hash or _dummy_hash()):
        return None
    return shop


def authenticate_employee(employee_id: str, password: str):
    """Return the active staff profile on valid credentials, else None.

    Mirrors MY-SHOP's employee sign-in: the 6-digit employee_id maps to an
    auth_user row; the Argon2 hash on it does the password check. Only
    admin/manager/IT-support roles may use the all-shops view.
    """
    code = re.sub(r"\D+", "", employee_id or "")
    password = password or ""
    if not LOGIN_CODE_RE.match(code) or not password:
        return None
    from django.contrib.auth.models import User
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute(
            "SELECT id, user_id, role FROM employees_employeeprofile "
            "WHERE employee_id=%s AND status='active' LIMIT 1",
            [code],
        )
        row = cur.fetchone()
    if row is None or row[2] not in EMPLOYEE_ROLES:
        check_password(password, _dummy_hash())
        return None
    user = User.objects.filter(pk=row[1]).first()
    if user is None or not check_password(password, user.password):
        return None
    return {
        "id": row[0],
        "role": row[2],
        "username": user.username,
    }


def login_employee(request, employee: dict) -> None:
    request.session[SESSION_EMPLOYEE_KEY] = employee["id"]
    request.session["chatbot_employee_role"] = employee["role"]


def current_employee(request):
    """Signed-in staff member (all-shops view) or None."""
    emp_id = request.session.get(SESSION_EMPLOYEE_KEY)
    if not emp_id:
        return None
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute(
            "SELECT id, role FROM employees_employeeprofile "
            "WHERE id=%s AND status='active' LIMIT 1",
            [emp_id],
        )
        row = cur.fetchone()
    if row is None or row[1] not in EMPLOYEE_ROLES:
        request.session.pop(SESSION_EMPLOYEE_KEY, None)
        request.session.pop("chatbot_employee_role", None)
        return None
    return {"id": row[0], "role": row[1]}


def login_shop(request, shop: Shop) -> None:
    request.session[SESSION_SHOP_KEY] = shop.pk


def logout_shop(request) -> None:
    request.session.pop(SESSION_SHOP_KEY, None)
    request.session.pop(SESSION_EMPLOYEE_KEY, None)
    request.session.pop("chatbot_employee_role", None)


def current_shop(request):
    """Return the signed-in Shop or None. Refreshes suspension flags."""
    shop_id = request.session.get(SESSION_SHOP_KEY)
    if not shop_id:
        return None
    try:
        shop = Shop.objects.get(pk=shop_id)
    except Shop.DoesNotExist:
        request.session.pop(SESSION_SHOP_KEY, None)
        return None
    if shop.is_suspended or shop.is_hidden:
        request.session.pop(SESSION_SHOP_KEY, None)
        return None
    return shop


def shop_required(view):
    """Page guard — redirect to login when no shop session exists."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        shop = current_shop(request)
        if shop is None:
            return redirect(f"{_login_url()}?next={request.path}")
        request.chat_shop = shop
        return view(request, *args, **kwargs)

    return wrapper


def api_shop_required(view):
    """API guard — 401 JSON instead of a redirect."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        shop = current_shop(request)
        if shop is None:
            return JsonResponse(
                {"ok": False, "error": "authentication required"}, status=401
            )
        request.chat_shop = shop
        return view(request, *args, **kwargs)

    return wrapper


def _login_url() -> str:
    from django.urls import reverse

    return reverse("chat:login")
