FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/logs

# The entrypoint waits for the shared database and the schema (DATABASE_URL ?schema=,
# default pettycashv3) that Flask (Petty Cash) owns. `sed` strips CRLF so the script still runs when the repo is checked
# out on Windows with autocrlf.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && \
    chmod +x /usr/local/bin/entrypoint.sh

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8030

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# Shell form so ${PORT} expands: hosts inject PORT; locally it is 8030.
CMD gunicorn -w 4 -b 0.0.0.0:${PORT:-8030} config.wsgi:application
