FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/engine
COPY pyproject.toml ./
COPY clustermotion ./clustermotion
RUN pip install --no-cache-dir . && useradd --uid 10001 --no-create-home cm
USER 10001
ENTRYPOINT ["cm"]
