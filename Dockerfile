FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 nabemono \
    && mkdir -p /app/data \
    && chown nabemono:nabemono /app/data

COPY --chown=nabemono:nabemono . .

USER nabemono

EXPOSE 8080

CMD ["python", "-m", "src.main"]
