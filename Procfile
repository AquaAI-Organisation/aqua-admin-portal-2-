release: python manage.py migrate --noinput && python manage.py collectstatic --noinput
web: gunicorn aqua_admin.wsgi --bind 0.0.0.0:$PORT --workers 3 --timeout 120
# Always-on automation: polls the mailbox (auto-analyses, creates DSARs, sends
# verification links) and confirms/delivers DSARs — every 2 minutes. Stays inert
# (0 dynos, no cost) until enabled with `heroku ps:scale worker=1`. Alternatively
# run `poll_inbox` and `confirm_dsar_logins` on Heroku Scheduler (see README).
worker: python manage.py run_automation --interval 120
