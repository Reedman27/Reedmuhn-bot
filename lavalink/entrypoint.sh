#!/bin/sh
# Refuse to run with no password rather than falling back to a known one.
if [ -z "$LAVALINK_SERVER_PASSWORD" ]; then
  echo "LAVALINK_PASSWORD is not set in .env - set a long random string (openssl rand -hex 24) and restart." >&2
  exit 1
fi
exec java -jar Lavalink.jar
