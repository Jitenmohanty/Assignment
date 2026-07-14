"""Celery application for the Artikate backend (Section 2).

Run a worker with:
    celery -A config worker -l info -Q emails,dead_letter
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("artikate")

# Pull every CELERY_* setting from Django settings.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks.py in every installed app.
app.autodiscover_tasks()
