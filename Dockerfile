FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hydros ./hydros

# State is held in memory; nothing is written to disk.
RUN useradd --uid 1000 --user-group --home-dir /app --no-log-init hydros \
    && chown -R hydros:hydros /app
USER 1000:1000

EXPOSE 8080

CMD ["python", "-m", "hydros"]
