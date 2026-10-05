FROM ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc AS uv

FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
COPY nodestep/pyproject.toml nodestep/README.md nodestep/LICENSE /app/nodestep/
COPY nodestep/src /app/nodestep/src
WORKDIR /app/nodeartifact
COPY nodeartifact/pyproject.toml nodeartifact/uv.lock nodeartifact/README.md nodeartifact/LICENSE ./
COPY nodeartifact/src ./src
RUN uv sync --locked --no-dev --extra server --no-editable

FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
RUN groupadd --system --gid 10001 nodeartifact \
 && useradd --system --uid 10001 --gid nodeartifact --home-dir /data --no-create-home --shell /usr/sbin/nologin nodeartifact \
 && install -d -o nodeartifact -g nodeartifact /data
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /data
USER nodeartifact
VOLUME /data
EXPOSE 4318
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4318/health', timeout=4)"]
ENTRYPOINT ["nodeartifact"]
CMD ["serve", "--host", "0.0.0.0", "--port", "4318", "--database", "/data/nodeartifact.db", "--allowed-host", "localhost:4318", "--allowed-host", "127.0.0.1:4318", "--allowed-host", "nodeartifact:4318"]
