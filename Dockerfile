
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_ROOT_USER_ACTION=ignore
ENV DJANGO_SETTINGS_MODULE=bank_site.settings

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY bank_site/requirements.txt .

RUN pip install --upgrade pip && \
    pip install -r requirements.txt

COPY bank_site/ .

# Verify Django installation and settings
RUN python -c "import django; print('Django version:', django.get_version())"

# Verify staticfiles registration
RUN python -c "import os; os.environ['DJANGO_SETTINGS_MODULE']='bank_site.settings'; import django; django.setup(); from django.conf import settings; from django.core.management import get_commands; print('Installed apps:', settings.INSTALLED_APPS); print('Collectstatic registered:', 'collectstatic' in get_commands()); assert 'collectstatic' in get_commands(), 'Staticfiles command not registered'"

RUN python manage.py check

RUN python manage.py collectstatic --no-input

EXPOSE 8010

CMD ["gunicorn", "bank_site.wsgi:application", "--bind", "0.0.0.0:8010"]
