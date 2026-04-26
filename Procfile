release: python manage.py collectstatic --noinput
web: gunicorn aqua_admin.wsgi --bind 0.0.0.0:$PORT --workers 3 --timeout 120