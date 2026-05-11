FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt pyproject.toml README.md LICENSE ./
COPY autodl_helper ./autodl_helper
COPY main.py config.example.yaml docker-entrypoint.sh ./

RUN pip install --upgrade pip \
    && pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && python -m playwright install --with-deps chromium \
    && pip install . --no-deps \
    && cp config.example.yaml /app/config.yaml

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["autodl-helper", "run", "daemon", "--config", "/app/config.yaml"]
