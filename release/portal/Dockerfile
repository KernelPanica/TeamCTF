FROM python:3.12-slim
WORKDIR /app
COPY *.whl /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/*.whl && mkdir -p /data
LABEL org.opencontainers.image.version="0.1.0"
EXPOSE 8000
CMD ["sh", "-c", "cyberrange migrate && exec uvicorn portal.app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
