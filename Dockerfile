FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends rsync gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

VOLUME /config
EXPOSE 8686
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import httpx; httpx.get('http://localhost:8686/')" || exit 1

ENV RELOCARR_CONFIG_DIR=/config
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "relocarr.main:app", "--host", "0.0.0.0", "--port", "8686", "--app-dir", "src"]
