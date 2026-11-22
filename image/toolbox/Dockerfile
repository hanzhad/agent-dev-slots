FROM debian:bookworm-slim

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      jq \
      postgresql-client \
      python3 \
      python3-pip \
      sqlite3 \
    && python3 -m pip install --no-cache-dir --break-system-packages playwright==1.51.0 \
    && python3 -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 toolbox \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash toolbox \
    && mkdir -p /workspace \
    && chown 1000:1000 /workspace

WORKDIR /workspace
USER 1000:1000

CMD ["sleep", "infinity"]
