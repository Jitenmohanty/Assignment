"""Submit a burst of email jobs onto the Celery queue (Section 2 demo).

Simulates the flash-sale burst from the scenario.

    # start a worker in another terminal first:
    celery -A config worker -l info

    python manage.py submit_emails --count 2000
"""

from django.core.management.base import BaseCommand

from emailq.tasks import send_email


class Command(BaseCommand):
    help = "Enqueue N email jobs to demonstrate rate-limited async delivery."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=2000)
        parser.add_argument("--fail-every", type=int, default=0,
                            help="make every Nth job fail once then succeed")

    def handle(self, *args, **options):
        count = options["count"]
        fail_every = options["fail_every"]
        for i in range(count):
            job = {
                "job_id": f"job-{i}",
                "to": f"user{i}@example.com",
                "subject": "Your order is confirmed",
                "body": "Thanks for shopping during the flash sale!",
            }
            if fail_every and i % fail_every == 0:
                job["_fail_times"] = 1
            send_email.delay(**job)
        self.stdout.write(self.style.SUCCESS(
            f"Enqueued {count} email jobs. Watch a worker drain them at "
            f"<= 200/min. Inspect Redis: `redis-cli ZCARD emailq:ratelimit:email`."
        ))
