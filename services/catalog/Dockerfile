# Build context: services/   (docker build -f catalog/Dockerfile services/)
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY catalog/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY catalog/app ./app
RUN useradd --uid 10001 --no-create-home app
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
