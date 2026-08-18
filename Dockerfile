FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt && \
    playwright install --with-deps chromium

COPY . ./

EXPOSE 8000

CMD ["uvicorn", "product_analyzer:app", "--host", "0.0.0.0", "--port", "8000"]
