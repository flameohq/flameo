<div align="center">
  <img src="https://raw.githubusercontent.com/flameohq/flameo/main/flameo_logo.svg" alt="Flameo" width="220">
</div>

<hr />

[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14%20%7C%203.15-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Tests](https://github.com/flameohq/flameo/actions/workflows/tests.yml/badge.svg)](https://github.com/flameohq/flameo/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/flameohq/flameo)
[![Version](https://img.shields.io/badge/version-0.1.1-blue)](https://github.com/flameohq/flameo/blob/main/pyproject.toml)
[![Protocols](https://img.shields.io/badge/protocols-ASGI%20%7C%20RSGI-ff6b35)](#asgi-rsgi-and-uvloop)


<p align="center">
    <em>Fast, Flexible, Less Verbose Framework. Syntax are as easy as you can think</em>
</p>


Flameo is a fast, easy to learn and code framework that pairs a minimal, familiar API with Python's flexibility. The same code supports Granians' **RSGI** and also Uvicon/Hypercor's **ASGI** natively.

```python
from flameo import Flameo

app = Flameo()

@app.get("/")
async def home(req, res):
    await res.json({"message": "Hello from Flameo 🔥"})
```

Handlers receive standard `req` and `res` objects. Add any of `db`, `cache`, `app` or `state` to the signature and **Flameo** injects the managed database handle, cache instance, app instance and app state.

```python
@app.get("/users")
async def get_user_json(req, res, db):     # request-scoped pooled DB handle provided by Flameo
    rows = await db.fetch("SELECT id, name FROM users")            #returns json by default
    await res.json(rows)

@app.get("/visits")
async def visits(req, res, cache):         # cache instance (works both with and without Redis)
    total = await cache.incr("visits")
    await res.json({"visits": total})

@app.get("/debug")
async def debug(req, res, app):            # the Flameo app instance
    await res.json({"debug": app.debug})

@app.get("/counter")
async def counter(req, res, state):        # shared app state
    state.visits = (state.visits or 0) + 1
    await res.json({"visits": state.visits})
```

There is no need to reach through `req.app.state`, `req.app` or fetch handles manually. Just name the parameter and **Flameo** hands you the object directly.


## Why Flameo?
- **Faster by design** 🚀 — **Flameo** speaks Granian's native RSGI protocol and also supports Uvicorn/Hypercorn's ASGI protocol. On benchmark it [beats](https://raw.githubusercontent.com/flameohq/flameo/docs/benchmark.html) the likes of FastAPI, Robyn and Sanic.
- **Named dependencies, not plumbing** — Type `db`, `cache`, `app` or `state` in the handler signature and the managed object arrives.
- **No framework under the framework** — Flameo depends only on `orjson`, `pydantic`, `aiofiles`, `granian`, and `uvloop` (on Linux and macOS). No Starlette, no ASGI toolkit, no wrapper around a wrapper. What you write is what you debug.
- **Batteries included** — Managed DB and cache pools, pydantic validation with file upload, sessions, static files, templates, rate limiting, compression, CORS and OpenAPI docs all out of the box.

## Features
- **Middleware** ⛓️ — global, router, and per-route, sync or async.
- **Dependency injection** 💉 — `db`, `cache`, `app`, and `state` parameters.
- **Validation** ✅ — Pydantic request bodies and uploaded files with structured errors.
- **Template rendering** 🖼️ — built in with Jinja2.
- **Streaming** 🌊 — chunked responses and Server-Sent Events.
- **Static files** 📁 — built-in serving with ETag and cache validators.
- **Compression** 🗜️ — out of the box with gzip, deflate, Brotli, and Zstandard.
- **Caching** ⚡ — built in both with and without Redis, with framework-managed pools.
- **Rate limiting** 🚦 — per endpoint or per key.
- **Sessions and CORS** 🍪 — signed cookies and configurable CORS.
- **Databases** 🗄️ — framework-managed pools for SQLite, PostgreSQL, MySQL, Oracle, and MongoDB.
- **API docs** 📚 — OpenAPI with Swagger UI and ReDoc.
- **Lifecycle** 🧭 — startup and shutdown hooks.
- **Logging** 📝 — built-in request logging.
- **CLI** 🧰 — project scaffolding, router generation, and a Granian RSGI runner.
- **Request limits** 🛠️ — configurable body-size limits.

## Quick Start

```bash
pip install flameo
```

```bash

# RSGI — Granian through the Flameo CLI
flameo run main:app --reload

# ASGI — Uvicorn
uvicorn main:app --reload
```

Optional extras:

```bash
pip install "flameo[postgres,cache,templates,compression]"
```

Replace `postgres` with `mysql`, `sqlite`, `oracle`, or `mongo` for your database.



## Optional extras

| Extra | Adds |
| --- | --- |
| `sqlite` | SQLite driver (`aiosqlite`) |
| `postgres` | PostgreSQL driver (`asyncpg`) |
| `mysql` | MySQL driver (`asyncmy` + `cryptography`) |
| `oracle` | Oracle driver (`oracledb`) |
| `mongo` | MongoDB driver (`pymongo`) |
| `database` | All five database drivers |
| `cache` | Redis client support (works with Redis, Valkey, and Dragonfly) |
| `templates` | Jinja2 template rendering |
| `compression` | Brotli and Zstandard codecs |
| `test` | Test and development dependencies |

## Documentation

- [Feature tour](#features)
- [Optional extras](#optional-extras)
- [Load-test benchmarks](benchmarks/README.md)
- [Test scope and coverage map](tests/README.md)

## Contributing

Contributions are welcome.

- **Report a bug** — open an issue with a minimal reproduction.
- **Propose a feature** — describe the behavior you want to add.
- **Send a pull request** — keep changes focused and add tests for new features.
- **Improve the docs** — examples and clarifications are always appreciated.

## License

Distributed under the [MIT License](LICENSE).
