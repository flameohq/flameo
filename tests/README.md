# Test coverage and known scope

Run the complete in-process suite with:

```bash
python -m pip install -e ".[test]"
python -m pytest tests/ --cov=flameo --cov-branch --cov-report=term-missing --cov-report=xml:tests/coverage.xml
```

Test modules are grouped by integration protocol, cross-protocol features, and core unit behavior. ASGI tests exercise the ASGI `scope`/`receive`/`send` contract. RSGI tests use an awaitable receive object and an RSGI-style response/stream transport recorder. They verify framework protocol adapters, not Granian's server implementation or network startup.

Current behavioral coverage includes:

- Application and mounted-router endpoints, all seven standard method decorators, typed route parameters, prefixes, not-found handling, and middleware ordering.
- ASGI and RSGI response serialization, request metadata/body parsing, cookies, signed sessions, and SSE streaming.
- CORS, gzip, static file retrieval/validators/traversal protection, endpoint caching, Pydantic request validation, and OpenAPI generation.
- Fixed-window rate limiting on app and router routes, including request-keyed limits and ASGI/RSGI responses.
- Router matching, in-memory cache behavior, session signing, model binding, multipart parsing/cleanup, and application lifespan hooks.
- File uploads on app and router endpoints across both protocols: RAM-backed files up to 1 MiB, disk spooling above the limit, `upload_to()` persistence, `response.after()` cleanup, per-request cleanup isolation under concurrency, and the default 30-minute stale-file sweeper.
- CLI scaffolding (`startproject`, `startapp`) and the `flameo run` Granian RSGI wrapper, including interactive and non-interactive generation, AST-based app discovery, router auto-wiring, host/port defaults, and strict IP/port validation.
- Concurrent independent ASGI requests.
- A coarse warm-route lookup smoke check in `tests/performance/`, with a broad ceiling intended to catch only pathological regressions.
- Optional live PostgreSQL, MySQL, and Oracle app/router database-injection tests in `tests/integration/test_database_services.py`.

The repository exposes additional code paths that are not covered by this suite: live Granian/ASGI server startup and shutdown, external Redis, Brotli/Zstandard codecs (optional extras), file downloads/sendFile, and process/thread logger behavior. Live database tests are optional and skip unless credentials are supplied; Redis and server tests remain separate. Current RSGI streaming coverage uses the protocol's streaming transport shape; it does not exercise a real socket or client disconnect.

To run the live database cases, install drivers and provide credentials through environment variables. For example in PowerShell:

```powershell
python -m pip install -e ".[test,database]"
$env:FLAMEO_TEST_POSTGRESQL_HOST = "127.0.0.1"
$env:FLAMEO_TEST_POSTGRESQL_PORT = "5432"
$env:FLAMEO_TEST_POSTGRESQL_USER = "flameo_test"
$env:FLAMEO_TEST_POSTGRESQL_PASSWORD = "your-local-test-password"
$env:FLAMEO_TEST_POSTGRESQL_DATABASE = "flameo_test"
$env:FLAMEO_TEST_MONGODB_URI = "mongodb://127.0.0.1:27017/?replicaSet=rs0"
$env:FLAMEO_TEST_MONGODB_DB = "flameo_test"
python -m pytest tests/integration/test_database_services.py tests/integration/test_mongo_services.py -m integration
```

Use the corresponding `FLAMEO_TEST_MYSQL_*` or `FLAMEO_TEST_ORACLE_*` variables to run those backends; each needs `HOST`, `USER`, `PASSWORD`, and `DATABASE`, while `PORT` is optional. For Oracle, `DATABASE` is the service name. MongoDB uses `FLAMEO_TEST_MONGODB_URI` and `FLAMEO_TEST_MONGODB_DB`; transaction coverage requires a replica set. Tests only run against configured backends and use both raw driver methods and injected `db` helpers through app and router routes.

In GitHub Actions, a dedicated `database-integration` job (see `.github/workflows/tests.yml`) starts real PostgreSQL, MySQL, Oracle, and MongoDB services and runs these tests against them, so the live database cases pass in CI instead of skipping. The main test-matrix job does not configure credentials, so it still reports them as skipped.

Regression fixes now covered by passing tests:

- ASGI `Response.setHeader()` preserves header values while normalizing names, matching RSGI.
- ASGI unhandled endpoint and middleware exceptions return a generic 500 without leaking exception details. Exceptions after stream start terminate the existing body without sending a second response start.
- Streaming multipart parsing recognizes delimiters across one-byte and uneven transport chunks and leaves boundary-like payload text intact.

These fixes remove the three known release blockers found by this suite.

No minimum coverage percentage is enforced: the framework has substantial code paths beyond this focused baseline, and coverage should be expanded based on measured reports rather than an artificial threshold.

Latest local run: Python 3.13.3 on Windows; **884 passed, 22 skipped**. The 22 skips are the live database cases (six per SQL backend plus four MongoDB cases) because no credentials were configured for this run. With a local MongoDB replica set, the four MongoDB cases pass and the suite reports **888 passed, 18 skipped**. Coverage XML is written to `tests/coverage.xml` when the documented command is used.
