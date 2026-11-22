FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        xvfb \
        fluxbox \
        x11vnc \
        novnc \
        websockify \
        fonts-noto-cjk \
        fonts-liberation \
        ca-certificates \
        util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 ns

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY ns_panel ./ns_panel
COPY docker/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod 755 /entrypoint.sh

EXPOSE 8787 6080
ENTRYPOINT ["/entrypoint.sh"]
