# Changelog

## [0.1.0a1] - 2026-10-05

First alpha.

### Added

- `configure()` and `instrument()`: OpenTelemetry spans for nodestep runs, nodes, model calls and tool calls, sent over OTLP/HTTP.
- `nodeartifact serve` in the `server` extra: an OTLP/HTTP receiver that stores spans in SQLite and shows them in a web UI.
- In the UI: a live trace list with search, a graph view with replay, a timeline with sub-agent runs, and a data viewer for state and chat messages.
- Light and dark themes from the nodestep stylesheet.
- A `Host` header check with `--allowed-host`, a 32 MiB body limit and a Content-Security-Policy.
- A Dockerfile that runs the server as a non-root user.

[0.1.0a1]: https://github.com/nodestep-ai/nodeartifact/releases/tag/v0.1.0a1
