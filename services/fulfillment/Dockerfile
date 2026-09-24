# Build context: services/   (docker build -f fulfillment/Dockerfile services/)
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY fulfillment/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared ./shared
COPY fulfillment/worker ./worker
RUN useradd --uid 10001 --no-create-home app
USER 10001
CMD ["python", "-m", "worker.main"]
