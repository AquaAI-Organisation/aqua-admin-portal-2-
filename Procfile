release: python manage.py migrate --noinput && python manage.py collectstatic --noinput
web: gunicorn aqua_admin.wsgi --bind 0.0.0.0:$PORT --workers 3 --timeout 120
# Optional always-on DSAR login watcher. Stays inert (0 dynos, no cost) until you
# enable it with `heroku ps:scale worker=1`. Alternatively run the one-shot
# `confirm_dsar_logins` on Heroku Scheduler — see README "Scheduled jobs".
worker: python manage.py confirm_dsar_logins --loop --interval 300
