from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from flameo import DBType, Flameo, Router, TransactionError


_DATABASES = {
    "postgresql": (DBType.PostgreSQL, "5432"),
    "mysql": (DBType.MySQL, "3306"),
    "oracle": (DBType.Oracle, "1521"),
}

_PROCEDURE_SQL = {
    "postgresql": {
        "create": [
            """CREATE OR REPLACE PROCEDURE flameo_test_recalc(p_value integer)
               LANGUAGE plpgsql AS $$ BEGIN PERFORM 1; END $$""",
            """CREATE OR REPLACE FUNCTION flameo_test_status(p_value integer)
               RETURNS TABLE (status integer) LANGUAGE sql AS $$ SELECT p_value + 1 $$""",
            """CREATE OR REPLACE FUNCTION flameo_test_list_items(p_min integer)
               RETURNS TABLE (status integer, id integer, name text) LANGUAGE sql AS $$
                 SELECT 1, 1, 'alpha' WHERE p_min <= 1
                 UNION ALL SELECT 1, 2, 'beta' WHERE p_min <= 2
               $$""",
        ],
        "drop": [
            "DROP PROCEDURE IF EXISTS flameo_test_recalc(integer)",
            "DROP FUNCTION IF EXISTS flameo_test_status(integer)",
            "DROP FUNCTION IF EXISTS flameo_test_list_items(integer)",
        ],
    },
    "mysql": {
        "create": [
            "CREATE PROCEDURE flameo_test_calc(IN p_value INT, OUT p_status INT) "
            "BEGIN SET p_status = p_value + 37; END",
            "CREATE PROCEDURE flameo_test_list_items(IN p_min INT) "
            "BEGIN SELECT 1 AS id, 'alpha' AS name FROM DUAL WHERE p_min <= 1 "
            "UNION ALL SELECT 2, 'beta' AS name FROM DUAL WHERE p_min <= 2; END",
        ],
        "drop": [
            "DROP PROCEDURE IF EXISTS flameo_test_calc",
            "DROP PROCEDURE IF EXISTS flameo_test_list_items",
        ],
    },
    "oracle": {
        "create": [
            "CREATE OR REPLACE PROCEDURE flameo_test_charge("
            "p_id IN NUMBER, p_status OUT NUMBER) AS "
            "BEGIN p_status := p_id + 35; END;",
            "CREATE OR REPLACE PROCEDURE flameo_test_list_items("
            "p_min IN NUMBER, p_status OUT NUMBER, p_rows OUT SYS_REFCURSOR) AS "
            "BEGIN p_status := 7; OPEN p_rows FOR "
            "SELECT 1 AS id, 'alpha' AS name FROM DUAL WHERE p_min <= 1 "
            "UNION ALL SELECT 2, 'beta' AS name FROM DUAL WHERE p_min <= 2; END;",
            "CREATE OR REPLACE PROCEDURE flameo_test_items("
            "p_min IN NUMBER, p_rows OUT SYS_REFCURSOR) AS "
            "BEGIN OPEN p_rows FOR "
            "SELECT 1 AS id, 'alpha' AS name FROM DUAL WHERE p_min <= 1 "
            "UNION ALL SELECT 2, 'beta' AS name FROM DUAL WHERE p_min <= 2; END;",
        ],
        "drop": [
            "BEGIN EXECUTE IMMEDIATE 'DROP PROCEDURE flameo_test_charge'; "
            "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -4043 THEN RAISE; END IF; END;",
            "BEGIN EXECUTE IMMEDIATE 'DROP PROCEDURE flameo_test_list_items'; "
            "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -4043 THEN RAISE; END IF; END;",
            "BEGIN EXECUTE IMMEDIATE 'DROP PROCEDURE flameo_test_items'; "
            "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -4043 THEN RAISE; END IF; END;",
        ],
    },
}


async def _run_procedure_sql(app, backend, statements):
    async with app.DB_POOL.acquire() as conn:
        if backend == "postgresql":
            for statement in statements:
                await conn.execute(statement)
        else:
            async with conn.cursor() as cur:
                for statement in statements:
                    await cur.execute(statement)
            await conn.commit()


def _database_config(backend):
    db_type, default_port = _DATABASES[backend]
    prefix = f"FLAMEO_TEST_{backend.upper()}_"
    names = {
        "host": prefix + "HOST",
        "username": prefix + "USER",
        "password": prefix + "PASSWORD",
        "database": prefix + "DATABASE",
    }
    missing = [env_name for env_name in names.values() if env_name not in os.environ]
    if missing:
        pytest.skip(f"Set {', '.join(missing)} to enable the {backend} integration test")

    return {
        "db_type": db_type,
        **{key: os.environ[env_name] for key, env_name in names.items()},
        "port": int(os.getenv(prefix + "PORT", default_port)),
        "min_size": 1,
        "max_size": 2,
    }


@pytest.mark.integration
@pytest.mark.parametrize("backend", ["postgresql", "mysql", "oracle"])
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.asyncio
async def test_live_database_injection_for_app_and_router(
    backend, protocol, app_factory, asgi_client, rsgi_client,
):
    app = app_factory(db_config=_database_config(backend))
    router = Router()

    query_by_backend = {
        "postgresql": "SELECT 1 AS value",
        "mysql": "SELECT 1 AS value",
        "oracle": "SELECT 1 AS value FROM DUAL",
    }
    query = query_by_backend[backend]

    async def report_database_value(req, res, db):
        row = await db.fetchrow(query)
        await res.json({"value": next(iter(row.values()))})

    app.get("/database", report_database_value)
    router.get("/database", report_database_value)
    app.use_router("/api", router)

    client = asgi_client if protocol == "asgi" else rsgi_client
    try:
        await app.init_db_pool()
        for path in ("/database", "/api/database"):
            response = await client(app, path=path)
            assert response["status"] == 200
            assert json.loads(response["body"]) == {"value": 1}
    finally:
        await app.shutdown_db_pool()


@pytest.mark.integration
@pytest.mark.parametrize("backend", ["postgresql", "mysql", "oracle"])
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.parametrize("mount", ["app", "router"])
@pytest.mark.asyncio
async def test_declared_procedures_for_app_and_router(
    backend, protocol, mount, app_factory, asgi_client, rsgi_client,
):
    config = _database_config(backend)
    app = app_factory(db_config=config)
    router = Router()

    if backend == "postgresql":
        async def out_params(req, res, db):
            result = await db.call_procedure(
                "flameo_test_status", (5,), out={"status": int}
            )
            await res.json(result)

        async def rows(req, res, db):
            result = await db.call_procedure(
                "flameo_test_list_items", (1,),
                out={"status": int}, returns_rows=True,
            )
            await res.json(result["rows"])

        expected_out = {"status": 6}
        expected_rows = [
            {"status": 1, "id": 1, "name": "alpha"},
            {"status": 1, "id": 2, "name": "beta"},
        ]
    elif backend == "mysql":
        async def out_params(req, res, db):
            result = await db.call_procedure(
                "flameo_test_calc", (5, None), out={"status": int}
            )
            await res.json(result)

        async def rows(req, res, db):
            result = await db.call_procedure(
                "flameo_test_list_items", (1,), returns_rows=True
            )
            await res.json(result["rows"])

        expected_out = {"status": 42}
        expected_rows = [
            {"id": 1, "name": "alpha"},
            {"id": 2, "name": "beta"},
        ]
    else:
        async def out_params(req, res, db):
            result = await db.call_procedure(
                "flameo_test_charge", {"p_id": 5}, out={"p_status": int}
            )
            await res.json({"p_status": int(result["p_status"])})

        async def rows(req, res, db):
            result = await db.call_procedure(
                "flameo_test_list_items", (1,),
                out={"p_status": int}, returns_rows=True,
            )
            await res.json([
                {"id": int(row["ID"]), "name": row["NAME"]}
                for row in result["rows"]
            ])

        async def cursor_rows(req, res, db):
            result = await db.ref_cursor("flameo_test_items", (1,))
            await res.json([
                {"id": int(row["ID"]), "name": row["NAME"]} for row in result
            ])

        expected_out = {"p_status": 40}
        expected_rows = [
            {"id": 1, "name": "alpha"},
            {"id": 2, "name": "beta"},
        ]

    handlers = [("/out-params", out_params), ("/rows", rows)]
    if backend == "oracle":
        handlers.append(("/ref-cursor", cursor_rows))
    prefix = ""
    for path, handler in handlers:
        if mount == "app":
            app.get(path, handler)
        else:
            router.get(path, handler)
    if mount == "router":
        app.use_router("/api", router)
        prefix = "/api"

    client = asgi_client if protocol == "asgi" else rsgi_client
    try:
        await app.init_db_pool()
        await _run_procedure_sql(app, backend, _PROCEDURE_SQL[backend]["drop"])
        await _run_procedure_sql(app, backend, _PROCEDURE_SQL[backend]["create"])
        response = await client(app, path=prefix + "/out-params")
        assert response["status"] == 200
        assert json.loads(response["body"]) == expected_out
        response = await client(app, path=prefix + "/rows")
        assert response["status"] == 200
        assert json.loads(response["body"]) == expected_rows
        if backend == "oracle":
            response = await client(app, path=prefix + "/ref-cursor")
            assert response["status"] == 200
            assert json.loads(response["body"]) == expected_rows
    finally:
        if app.DB_POOL is not None:
            await _run_procedure_sql(app, backend, _PROCEDURE_SQL[backend]["drop"])
        await app.shutdown_db_pool()


@pytest.mark.integration
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.asyncio
async def test_sqlite_database_injection_for_app_and_router(
    protocol, tmp_path, app_factory, asgi_client, rsgi_client,
):
    app = app_factory(db_config={
        "db_type": DBType.SQLite,
        "file_path": str(tmp_path / "injection.db"),
    })
    router = Router()

    async def report_database_value(req, res, db):
        row = await db.fetchrow("SELECT 1 AS value")
        await res.json({"value": next(iter(row.values()))})

    app.get("/database", report_database_value)
    router.get("/database", report_database_value)
    app.use_router("/api", router)

    client = asgi_client if protocol == "asgi" else rsgi_client
    try:
        await app.init_db_pool()
        for path in ("/database", "/api/database"):
            response = await client(app, path=path)
            assert response["status"] == 200
            assert json.loads(response["body"]) == {"value": 1}
    finally:
        await app.shutdown_db_pool()


@pytest.mark.integration
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.asyncio
async def test_sqlite_crud_and_rollback(
    protocol, tmp_path, app_factory, asgi_client, rsgi_client,
):
    app = app_factory(db_config={
        "db_type": DBType.SQLite,
        "file_path": str(tmp_path / "crud.db"),
    })

    @app.post("/items")
    async def add_item(req, res, db):
        body = await req.body
        await db.execute("INSERT INTO items (name) VALUES (?)", (body["name"],))
        await res.json({"ok": True})

    @app.post("/fail")
    async def fail_item(req, res, db):
        try:
            async with db.transaction():
                await db.execute("INSERT INTO items (name) VALUES (?)", ("doomed",))
                raise RuntimeError("boom")
        except TransactionError as exc:
            await res.json({
                "rolled_back": True,
                "cause": type(exc.cause).__name__,
            })

    @app.get("/items")
    async def list_items(req, res, db):
        rows = await db.fetch("SELECT name FROM items ORDER BY name")
        await res.json({"names": [row["name"] for row in rows]})

    client = asgi_client if protocol == "asgi" else rsgi_client
    headers = {"Content-Type": "application/json"}
    try:
        await app.init_db_pool()
        async with app.DB_POOL.acquire() as conn:
            await conn.execute("CREATE TABLE items (name TEXT)")

        created = await client(
            app, "POST", "/items", headers=headers,
            body=json.dumps({"name": "alpha"}).encode(),
        )
        assert created["status"] == 200
        assert json.loads(created["body"]) == {"ok": True}

        failed = await client(app, "POST", "/fail", headers=headers, body=b"{}")
        assert failed["status"] == 200
        assert json.loads(failed["body"]) == {
            "rolled_back": True,
            "cause": "RuntimeError",
        }

        listed = await client(app, path="/items")
        assert json.loads(listed["body"]) == {"names": ["alpha"]}
    finally:
        await app.shutdown_db_pool()


@pytest.mark.integration
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.parametrize("mount", ["app", "router"])
@pytest.mark.asyncio
async def test_sqlite_commit_failure_is_reported_to_client(
    protocol, mount, tmp_path, app_factory, asgi_client, rsgi_client,
):
    app = app_factory(db_config={
        "db_type": DBType.SQLite,
        "file_path": str(tmp_path / "transaction-error.db"),
    })
    router = Router()

    async def create_deferred_child(req, res, db):
        try:
            await db.execute("PRAGMA foreign_keys = ON")
            async with db.transaction():
                await db.execute(
                    "INSERT INTO children (parent_id) VALUES (?)", (404,)
                )
        except TransactionError as exc:
            await res.status(409).json({
                "error": "transaction_failed",
                "detail": str(exc),
                "rolled_back": exc.rolled_back,
                "cause": type(exc.cause).__name__,
            })
            return
        await res.json({"ok": True})

    if mount == "app":
        app.post("/deferred", create_deferred_child)
        paths = ["/deferred"]
    else:
        router.post("/deferred", create_deferred_child)
        app.use_router("/api", router)
        paths = ["/api/deferred"]

    client = asgi_client if protocol == "asgi" else rsgi_client
    try:
        await app.init_db_pool()
        async with app.DB_POOL.acquire() as conn:
            await conn.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
            await conn.execute(
                "CREATE TABLE children ("
                "id INTEGER PRIMARY KEY, "
                "parent_id INTEGER REFERENCES parents(id) "
                "DEFERRABLE INITIALLY DEFERRED)"
            )

        for path in paths:
            response = await client(
                app, "POST", path,
                headers={"Content-Type": "application/json"}, body=b"{}",
            )
            assert response["status"] == 409
            payload = json.loads(response["body"])
            assert payload["error"] == "transaction_failed"
            assert payload["rolled_back"] is True
            assert payload["cause"] == "IntegrityError"
            assert "foreign key" in payload["detail"].lower()
    finally:
        await app.shutdown_db_pool()


@pytest.mark.integration
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.asyncio
async def test_unhandled_transaction_error_stays_generic_500(
    protocol, tmp_path, app_factory, asgi_client, rsgi_client,
):
    app = app_factory(db_config={
        "db_type": DBType.SQLite,
        "file_path": str(tmp_path / "transaction-500.db"),
    })

    @app.get("/boom")
    async def boom(req, res, db):
        async with db.transaction():
            raise RuntimeError("private implementation detail")

    client = asgi_client if protocol == "asgi" else rsgi_client
    try:
        await app.init_db_pool()
        response = await client(app, path="/boom")
        assert response["status"] == 500
        assert response["body"] == b"Internal Server Error"
        assert b"private implementation detail" not in response["body"]
    finally:
        await app.shutdown_db_pool()


@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.parametrize("mount", ["app", "router"])
@pytest.mark.asyncio
async def test_ref_cursor_response_for_app_and_router(
    protocol, mount, app_factory, asgi_client, rsgi_client, monkeypatch,
):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.Oracle)
    monkeypatch.setattr(framework, "_oracle_cursor_type", "CURSOR")

    class FakeRefCursor:
        description = (("id",), ("name",))

        def __init__(self):
            self.rows = [(1, "alpha"), (2, "beta")]
            self.closed = False

        async def fetchall(self):
            rows, self.rows = self.rows, []
            return rows

        async def close(self):
            self.closed = True

    class FakeCursor:
        def __init__(self, calls):
            self.calls = calls
            self.refcursor = FakeRefCursor()

        async def callproc(self, name, args):
            self.calls.append((name, args))
            return args

        def var(self, kind):
            return SimpleNamespace(getvalue=lambda: self.refcursor)

    class FakeConnection:
        def __init__(self):
            self.calls = []
            self.cursor_obj = FakeCursor(self.calls)
            self.commits = 0

        def cursor(self):
            return self.cursor_obj

        async def commit(self):
            self.commits += 1

    class FakeAcquire:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, *exc):
            return False

    connection = FakeConnection()

    class FakePool:
        def acquire(self):
            return FakeAcquire(connection)

    app = app_factory()
    app.DB_POOL = FakePool()
    router = Router()

    async def report_rows(req, res, db):
        rows = await db.ref_cursor("flameo_test_items", {"min_id": 1})
        await res.json(rows)

    if mount == "app":
        app.get("/ref-cursor", report_rows)
        path = "/ref-cursor"
    else:
        router.get("/ref-cursor", report_rows)
        app.use_router("/api", router)
        path = "/api/ref-cursor"

    client = asgi_client if protocol == "asgi" else rsgi_client
    response = await client(app, path=path)
    assert response["status"] == 200
    assert json.loads(response["body"]) == [
        {"id": 1, "name": "alpha"},
        {"id": 2, "name": "beta"},
    ]
    name, args = connection.calls[-1]
    assert name == "flameo_test_items"
    assert args[0] == 1
    assert hasattr(args[1], "getvalue")
    assert connection.cursor_obj.refcursor.closed
    assert connection.commits == 1


@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.parametrize("mount", ["app", "router"])
@pytest.mark.asyncio
async def test_call_procedure_response_for_app_and_router(
    protocol, mount, app_factory, asgi_client, rsgi_client, monkeypatch,
):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.MySQL)

    class FakeCursor:
        def __init__(self, calls):
            self.calls = calls
            self.rows = [(42,)]
            self.description = (("id",), ("name",))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def callproc(self, name, args):
            self.calls.append(("callproc", name, args))
            return args

        async def nextset(self):
            return False

        async def execute(self, sql, args=None):
            self.calls.append(("execute", sql))

        async def fetchone(self):
            return self.rows.pop(0) if self.rows else None

        async def fetchall(self):
            rows, self.rows = self.rows, []
            return rows

    class FakeConnection:
        def __init__(self):
            self.calls = []
            self.cursor_obj = FakeCursor(self.calls)
            self.commits = 0

        def cursor(self):
            return self.cursor_obj

        async def commit(self):
            self.commits += 1

    class FakeAcquire:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, *exc):
            return False

    connection = FakeConnection()

    class FakePool:
        def acquire(self):
            return FakeAcquire(connection)

    app = app_factory()
    app.DB_POOL = FakePool()
    router = Router()

    async def report_status(req, res, db):
        result = await db.call_procedure("calc", (5,), out={"status": int})
        await res.json({"status": result["status"]})

    async def report_rows(req, res, db):
        result = await db.call_procedure("list_items", (5,), returns_rows=True)
        await res.json(result["rows"])

    if mount == "app":
        app.post("/procedure", report_status)
        app.post("/procedure-rows", report_rows)
        path = "/procedure"
        rows_path = "/procedure-rows"
    else:
        router.post("/procedure", report_status)
        router.post("/procedure-rows", report_rows)
        app.use_router("/api", router)
        path = "/api/procedure"
        rows_path = "/api/procedure-rows"

    client = asgi_client if protocol == "asgi" else rsgi_client
    response = await client(app, "POST", path)
    assert response["status"] == 200
    assert json.loads(response["body"]) == {"status": 42}
    assert connection.calls == [
        ("callproc", "calc", [5]),
        ("execute", "SELECT @_calc_0"),
    ]
    assert connection.commits == 1

    connection.cursor_obj.rows = [(1, "alpha"), (2, "beta")]
    rows_response = await client(app, "POST", rows_path)
    assert rows_response["status"] == 200
    assert json.loads(rows_response["body"]) == [
        {"id": 1, "name": "alpha"},
        {"id": 2, "name": "beta"},
    ]
    assert connection.calls[-1] == ("callproc", "list_items", [5])


@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.parametrize("mount", ["app", "router"])
@pytest.mark.asyncio
async def test_postgres_named_bind_response_for_app_and_router(
    protocol, mount, app_factory, asgi_client, rsgi_client, monkeypatch,
):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.PostgreSQL)

    class FakeConnection:
        def __init__(self):
            self.queries = []

        async def fetchrow(self, sql, *args):
            self.queries.append((sql, args))
            return {"value": args[0]}

    class FakeAcquire:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, *exc):
            return False

    connection = FakeConnection()

    class FakePool:
        def acquire(self):
            return FakeAcquire(connection)

    app = app_factory()
    app.DB_POOL = FakePool()
    router = Router()

    async def report_value(req, res, db):
        row = await db.fetchrow("SELECT :value AS value", await req.body)
        await res.json({"value": row["value"]})

    if mount == "app":
        app.post("/named", report_value)
        path = "/named"
    else:
        router.post("/named", report_value)
        app.use_router("/api", router)
        path = "/api/named"

    client = asgi_client if protocol == "asgi" else rsgi_client
    response = await client(
        app, "POST", path, headers={"Content-Type": "application/json"},
        body=b'{"value": 42, "extra": "ignored"}',
    )
    assert response["status"] == 200
    assert json.loads(response["body"]) == {"value": 42}
    assert connection.queries == [("SELECT $1 AS value", (42,))]


@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.parametrize("mount", ["app", "router"])
@pytest.mark.asyncio
async def test_model_binding_response_for_app_and_router(
    protocol, mount, app_factory, asgi_client, rsgi_client, monkeypatch,
):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.PostgreSQL)

    class Record:
        identifier: int
        label: str = "default"

    class FakeConnection:
        async def fetchrow(self, sql, *args):
            return {"IDENTIFIER": str(args[0])}

        async def fetch(self, sql, *args):
            return [
                {"IDENTIFIER": str(args[0])},
                {"IDENTIFIER": str(args[0] + 1), "LABEL": "extra"},
            ]

    class FakeAcquire:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, *exc):
            return False

    class FakePool:
        def acquire(self):
            return FakeAcquire()

    app = app_factory()
    app.DB_POOL = FakePool()
    router = Router()

    async def report_one(req, res, db):
        row = await db.fetchrow("SELECT :value AS identifier", await req.body,
                                model=Record)
        await res.json({"row": row})

    async def report_many(req, res, db):
        rows = await db.fetch("SELECT :value AS identifier", await req.body,
                              model=Record)
        await res.json({"rows": rows})

    if mount == "app":
        app.post("/model-one", report_one)
        app.post("/model-many", report_many)
        paths = ("/model-one", "/model-many")
    else:
        router.post("/model-one", report_one)
        router.post("/model-many", report_many)
        app.use_router("/api", router)
        paths = ("/api/model-one", "/api/model-many")

    client = asgi_client if protocol == "asgi" else rsgi_client
    cases = (
        (paths[0], "row", {"identifier": 42, "label": "default"}),
        (paths[1], "rows", [
            {"identifier": 42, "label": "default"},
            {"identifier": 43, "label": "extra"},
        ]),
    )
    for path, key, expected in cases:
        response = await client(
            app, "POST", path, headers={"Content-Type": "application/json"},
            body=b'{"value": 42}',
        )
        assert response["status"] == 200
        assert json.loads(response["body"]) == {key: expected}


@pytest.mark.integration
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.parametrize("mount", ["app", "router"])
@pytest.mark.asyncio
async def test_sqlite_query_manager_contract_for_app_and_router(
    protocol, mount, tmp_path, app_factory, asgi_client, rsgi_client,
):
    app = app_factory(db_config={
        "db_type": DBType.SQLite,
        "file_path": str(tmp_path / "query-manager.db"),
    })
    router = Router()

    async def write(req, res, db):
        inserted = await db.execute("INSERT INTO items (name) VALUES (?)", ("one",))
        inserted_many = await db.executemany(
            "INSERT INTO items (name) VALUES (?)", [("two",), ("three",)]
        )
        await res.json({"inserted": inserted, "inserted_many": inserted_many})

    async def write_nested(req, res, db):
        async with db.transaction():
            async with db.transaction():
                await db.execute("INSERT INTO items (name) VALUES (?)", ("nested",))
        await res.json({"ok": True})

    async def list_names(req, res, db):
        rows = await db.fetch("SELECT name FROM items ORDER BY name")
        await res.json({"names": [row["name"] for row in rows]})

    async def bad_fetch(req, res, db):
        await db.fetchrow("INSERT INTO items (name) VALUES (?)", ("bad",))

    handlers = (
        ("/write", "POST", write),
        ("/nested", "POST", write_nested),
        ("/names", "GET", list_names),
        ("/bad-fetch", "GET", bad_fetch),
    )
    prefix = ""
    for path, method, handler in handlers:
        if mount == "app":
            getattr(app, method.lower())(path, handler)
        else:
            getattr(router, method.lower())(path, handler)
    if mount == "router":
        app.use_router("/api", router)
        prefix = "/api"

    client = asgi_client if protocol == "asgi" else rsgi_client
    try:
        await app.init_db_pool()
        async with app.DB_POOL.acquire() as conn:
            await conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")

        written = await client(app, "POST", prefix + "/write")
        assert written["status"] == 200
        assert json.loads(written["body"]) == {"inserted": 1, "inserted_many": 2}

        nested = await client(app, "POST", prefix + "/nested")
        assert nested["status"] == 200
        assert json.loads(nested["body"]) == {"ok": True}

        listed = await client(app, path=prefix + "/names")
        assert json.loads(listed["body"]) == {
            "names": ["nested", "one", "three", "two"]
        }

        bad = await client(app, path=prefix + "/bad-fetch")
        assert bad["status"] == 500
        assert b"only work with statements" not in bad["body"]
    finally:
        await app.shutdown_db_pool()
