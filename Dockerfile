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

# Placeholder DB URL used ONLY for build-time Django commands
ARG BUILD_DATABASE_URL=postgres://build:build@localhost:5432/build

# Verify Django installation and settings
RUN python -c "import django; print('Django version:', django.get_version())"

RUN DATABASE_URL="$BUILD_DATABASE_URL" python manage.py check

# Collect static files during build
RUN DATABASE_URL="$BUILD_DATABASE_URL" python manage.py collectstatic --no-input

# Railway dynamically assigns a PORT. We default to 8080.
EXPOSE 8080

# Run migrations FIRST, then start Gunicorn
CMD ["sh", "-c", "python manage.py migrate && gunicorn bank_site.wsgi:application --bind 0.0.0.0:${PORT:-8080} --workers 2 --threads 2 --timeout 120 --access-logfile - --error-logfile - --log-level info"]
