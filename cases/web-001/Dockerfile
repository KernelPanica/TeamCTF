FROM ubuntu:20.04
RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*
COPY rootfs/ /opt/service/
RUN mkdir -p /opt/objective && printf 'stage-2-test-objective' > /opt/objective/red-key
EXPOSE 80
CMD ["python3", "/opt/service/server.py"]
