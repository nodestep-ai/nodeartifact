# Server

`nodeartifact serve` receives traces over OTLP/HTTP, stores them in SQLite and serves the [web UI](ui.md). It needs the `server` extra.

```bash
uv run nodeartifact serve
```

The UI is at http://127.0.0.1:4318/ and traces go to http://127.0.0.1:4318/v1/traces. The spans are kept in `nodeartifact.db` in the current folder.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | Address to bind. Clients must name it in the `Host` header, or name a host given to `--allowed-host` |
| `--port` | `4318` | Port to bind. 4318 is the standard OTLP/HTTP port |
| `--database` | `./nodeartifact.db` | SQLite database file. It is created when missing; its folder must exist |
| `--allowed-host` | none | Another `Host` header to accept, such as `localhost:4318`. Repeat it for each name clients use |

To keep the traces in one place whatever folder you start in:

```bash
uv run nodeartifact serve --database ~/traces/nodeartifact.db
```

## Security

The server has no authentication. Keep it on `127.0.0.1`, the default, and do not expose it on a network you do not trust. Traces can hold prompts, model answers and tool results.

It answers only requests whose `Host` header names the `--host` address or an `--allowed-host`, and others get 400. This blocks DNS rebinding. On a loopback address, `localhost`, `127.0.0.1` and `::1` all work.

Bound to `0.0.0.0`, as in a container, pass `--allowed-host` for every name clients use:

```bash
uv run nodeartifact serve --host 0.0.0.0 --allowed-host localhost:4318 --allowed-host nodeartifact:4318
```

Without any `--allowed-host`, the server warns at startup and every other name gets 400.

## Run in Docker

The image installs nodestep from the sibling checkout, so build it from the folder that holds `nodestep/` and `nodeartifact/`:

```bash
docker build -f nodeartifact/docker/Dockerfile -t nodeartifact .
docker run --rm -p 127.0.0.1:4318:4318 -v nodeartifact-data:/data nodeartifact
```

The UI is at http://localhost:4318/ and traces go to http://localhost:4318/v1/traces. The server runs as a non-root user and keeps its database in the `/data` volume. The image accepts the `Host` headers `localhost:4318`, `127.0.0.1:4318` and `nodeartifact:4318`; other names or ports need their own `--allowed-host`.

### Change the port

Arguments after the image name replace the whole default command, so repeat all of it. For host port 8080:

```bash
docker run --rm -p 127.0.0.1:8080:4318 -v nodeartifact-data:/data nodeartifact \
  serve --host 0.0.0.0 --database /data/nodeartifact.db \
  --allowed-host localhost:8080 --allowed-host 127.0.0.1:8080
```

Keep port 4318 inside the container and change only the published port: the health check calls `http://127.0.0.1:4318/health`. Publish on `127.0.0.1` as above, since there is no authentication.

## Routes

| Route | Returns |
|---|---|
| `POST /v1/traces` | The OTLP/HTTP receiver |
| `GET /` | The trace list; `?q=` searches it |
| `GET /results?q=` | The rows of the trace list alone, which the search field loads as you type |
| `GET /live` | The trace list as server-sent events, sent again when new spans arrive |
| `GET /traces/{trace_id}` | The graph view, or the timeline for a trace without a traced graph |
| `GET /traces/{trace_id}/timeline` | The timeline |
| `GET /traces/{trace_id}/spans/{span_id}` | One span |
| `GET /traces/{trace_id}/live` | The new spans of a trace as server-sent events, resuming after `Last-Event-ID`. It ends with an `end` event when the run span arrives, or an `idle` event after ten minutes without new spans |
| `GET /health` | `ok`, for any `Host` header |

## Rules and defaults

| Rule | What happens |
|---|---|
| Database | In WAL mode. A span sent twice is stored once |
| Database errors | The server exits with code 1 when it cannot open the file, when nodeartifact did not create it, or when it has another schema version |
| `--allowed-host` format | `host`, `host:port`, `[ipv6]` or `[ipv6]:port`, with no wildcard. Without a port it matches any port. A `Host` header without a port means port 80 |
| Request body | At most 32 MiB, before and after gzip |
| Content types | `application/x-protobuf` and `application/json`, plain or gzip. Other types get 415 |
| Content-Security-Policy | Every page allows scripts only from the server. The graph view also allows the Mermaid file from jsdelivr, pinned with an integrity hash, and inline styles, which Mermaid needs |
