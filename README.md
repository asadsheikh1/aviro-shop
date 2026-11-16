# AVIRO — demo shop with fault injection

AVIRO is a small online shop built as a realistic **system to monitor**. It is the "patient" that [SENTINEL](https://github.com/asadsheikh1/sentinel) diagnoses. It's an ordinary application that knows nothing about SENTINEL: it emits standard OpenTelemetry and can be broken on purpose.

```
               ┌────────────┐
 shoppers ───▶ │  gateway   │ :8080 (only public port)
               └─────┬──────┘
          ┌──────────┼──────────────┐
          ▼          ▼              ▼
    ┌──────────┐ ┌──────────┐  ┌──────────┐   reserve stock → charge card →
    │inventory │ │ orders   │─▶│ payments │   save → write receipt → queue job
    └────┬─────┘ └──┬───┬───┘  └────┬─────┘
         │          │   │ Celery     │
         ▼          ▼   ▼            ▼
      ┌──────────────┐ ┌────────┐ ┌────────┐
      │  PostgreSQL  │ │ Valkey │◀│ worker │  marks orders fulfilled
      └──────────────┘ └────────┘ └────────┘
```

| Service | What it does |
|---|---|
| `gateway` | Public API (`/api/products`, `/api/orders`), admin proxy (`/admin/<service>/…`) |
| `inventory` | Catalogue and atomic stock reservations |
| `payments` | Simulated card charges. Tokens starting `tok_decline` are declined |
| `orders` | Checkout orchestration, receipts on local disk, queues fulfilment |
| `worker` | Celery (Valkey broker): fulfils orders |

Stack: Python 3.12, FastAPI, psycopg 3, Celery + Valkey, PostgreSQL 17, Podman.

## Quickstart

Needs [uv](https://docs.astral.sh/uv/), Podman with `podman machine` running, and podman-compose.

```bash
make install
make up                 # http://127.0.0.1:8080
curl 127.0.0.1:8080/api/health
curl 127.0.0.1:8080/api/products
curl -X POST 127.0.0.1:8080/api/orders -H 'content-type: application/json' \
     -d '{"customer_email":"me@example.com","items":[{"sku":"KB-101","qty":1}],"card_token":"tok_visa_12345678"}'
make load USERS=10      # synthetic shoppers (Locust); make load-stop
make down
```

## Telemetry (OpenTelemetry)

Every service runs under the standard zero-code instrumentation (`opentelemetry-instrument`), so traces, metrics and logs follow the OTel SDK environment variables. Exporters are off by default.

```bash
make up-otel            # also starts a debug collector that prints what it receives
make otel-logs

# Send to SENTINEL (or any OTLP endpoint):
OTEL_EXPORTER=otlp \
OTEL_EXPORTER_OTLP_ENDPOINT=https://ingest.<your-sentinel> \
OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer <ingest-token>" make up
```

Logs are JSON on stdout with `trace_id` and `span_id`, so they correlate with traces.

## Breaking it on purpose

### Runtime faults (admin API, token required)

```bash
make fault ARGS="set payments latency ms=8000"            # S05: slow dependency
make fault ARGS="set inventory memory_leak mb_per_s=50"   # S06: leak → OOM kill → restart
make fault ARGS="set orders disk_fill"                    # disk full → receipt writes fail
make fault ARGS="set payments cpu_hog threads=2"
make fault ARGS="list payments"
make fault ARGS="stats inventory"
make fault ARGS="clear payments"
```

| Fault | Effect |
|---|---|
| `latency` | Delay requests by `ms` (with `probability`) |
| `error_rate` | Fail requests with HTTP 500 (with `probability`) |
| `dependency_timeout` | Calls to `target` hang. `db` → Postgres cancels at `statement_timeout`. A service name → the caller's HTTP timeout fires |
| `memory_leak` | Allocate `mb_per_s` up to `max_mb`. `inventory` has a 300 MB limit, so it gets OOM-killed |
| `cpu_hog` | `threads` busy loops |
| `disk_fill` | Fill the data dir (`orders` has a 200 MB tmpfs, so writes really fail) |
| `process_crash` | Exit after `delay_s`. Podman restarts the container |

Faults switch off after `duration_s` (default 300 s). The admin token is `AVIRO_ADMIN_TOKEN` (dev default `aviro-admin-dev-only`).

Infrastructure and data scenarios:
- **S07, broker down:** `podman stop aviro_valkey_1`.
- **S10, latent bug triggered by a data change:**
  ```
  curl -X POST -H 'X-Aviro-Admin-Token: …' 127.0.0.1:8080/admin/inventory/scenario-data/s10
  ```

### Code faults (real commits and tags)

Bugs introduced by "deploys" are real commits on `scenario/*` branches, tagged `scenario/SXX-bad` and `scenario/SXX-fix`. Deploy any ref to the running stack with:

```bash
make deploy REF=scenario/S01-bad   # builds that ref, rolls it out, emits a deployment event
make deploy REF=main
```

See [scenarios/README.md](scenarios/README.md).

## Tests

```bash
make lint && make test     # unit tests (no containers)
make up && make smoke      # live: every fault reproduces its symptom
make up-otel && make smoke # also checks telemetry reaches a collector
```

## Licence

[Apache-2.0](LICENSE)
