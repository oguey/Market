# One image recipe for the 4 services:  docker build --build-arg SERVICE=orders services/
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --system app
ARG SERVICE
ENV SERVICE=${SERVICE}
COPY common ./common
COPY ${SERVICE} ./${SERVICE}
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD uvicorn ${SERVICE}.main:create_app --factory --host 0.0.0.0 --port 8000
