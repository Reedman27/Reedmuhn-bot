FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# bot.db lives here via DB_PATH - mount this as a volume (see
# docker-compose.yml) so the database survives container recreation/updates.
VOLUME ["/app/data"]

CMD ["python", "bot.py"]
