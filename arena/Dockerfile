FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends docker.io iptables ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir 'fastapi>=0.115,<1' 'uvicorn>=0.30,<1' 'pydantic>=2.9,<3' 'PyYAML>=6,<7'
COPY shared/ shared/
COPY arena/ arena/
CMD ["python", "-m", "arena.app.serve"]
