FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

VOLUME ["/app/data"]

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

# Fix ownership of the mounted /app/data volume before dropping to appuser -
# it's mounted from the host and may not match appuser's UID (a fresh dir,
# a root-owned one, or a leftover UID from before this image added a
# non-root user), which would otherwise make every write inside it -
# webui_secret_key, bot.db - fail with PermissionError and crash-loop the
# container.
EXPOSE 8080
CMD chown -R appuser:appuser /app/data && exec gosu appuser uvicorn main:app --host 0.0.0.0 --port 8080
