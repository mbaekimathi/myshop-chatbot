"""WSGI config for MY-CHATBOT."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mychatbot.settings")

application = get_wsgi_application()
