from django.urls import path

from . import views

app_name = "chat"

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("login/pick/", views.pick_shop, name="pick_shop"),
    path("all/", views.all_shops_view, name="all_shops"),
    path("api/all/chat/", views.api_all_chat, name="api_all_chat"),
    path("chat/", views.app_view, name="app"),
    path("api/poll/", views.api_poll, name="api_poll"),
    path("api/open/", views.api_open, name="api_open"),
    path("api/groups/", views.api_group_create, name="api_group_create"),
    path("api/items/search/", views.api_item_search, name="api_item_search"),
    path(
        "api/suppliers/search/",
        views.api_supplier_search,
        name="api_supplier_search",
    ),
    path(
        "api/serials/search/",
        views.api_serial_search,
        name="api_serial_search",
    ),
    path("api/stock-in/", views.api_stock_in, name="api_stock_in"),
    path("api/sales/", views.api_sale, name="api_sale"),
    path("api/expenses/", views.api_expense, name="api_expense"),
    path("api/clients/search/", views.api_client_search, name="api_client_search"),
    path(
        "api/conversations/<int:conversation_id>/transfer/",
        views.api_transfer,
        name="api_transfer",
    ),
    path("api/transfers/", views.api_transfers, name="api_transfers"),
    path(
        "api/transfers/<int:movement_id>/respond/",
        views.api_transfer_respond,
        name="api_transfer_respond",
    ),
    path(
        "api/pending/<int:message_id>/confirm/",
        views.api_pending_confirm,
        name="api_pending_confirm",
    ),
    path(
        "api/conversations/<int:conversation_id>/quick-stockin/",
        views.api_quick_stockin,
        name="api_quick_stockin",
    ),
    path(
        "api/conversations/<int:conversation_id>/send/",
        views.api_send,
        name="api_send",
    ),
    path(
        "api/conversations/<int:conversation_id>/read/",
        views.api_read,
        name="api_read",
    ),
    path(
        "api/conversations/<int:conversation_id>/typing/",
        views.api_typing,
        name="api_typing",
    ),
    path(
        "api/conversations/<int:conversation_id>/history/",
        views.api_history,
        name="api_history",
    ),
    path(
        "media/chat/<int:message_id>/",
        views.api_attachment,
        name="attachment",
    ),
]
