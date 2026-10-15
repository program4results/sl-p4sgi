FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 0.4.26: optional adb client for RUSTAiDMIN (off by default; build with INSTALL_ADB=1)
ARG INSTALL_ADB=0
RUN if [ "$INSTALL_ADB" = "1" ]; then apt-get update && apt-get install -y --no-install-recommends adb && rm -rf /var/lib/apt/lists/*; fi

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY sql ./sql
COPY scripts ./scripts

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
