from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from flameo import DBType, Router, TransactionError
from flameo.flameo import _MongoPool


def _matches(doc, query):
    return all(doc.get(key) == value for key, value in (query or {}).items())


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return list(self.docs if length is None else self.docs[:length])


class FakeCollection:
    def __init__(self, database, name):
        self.database = database
        self.name = name
        self.docs = database.docs.setdefault(name, [])

    def _record(self, method, *args):
        self.database.calls.append(
            (self.name, method, args, self.database.client.bound_session)
        )

    async def find_one(self, query=None):
        self._record("find_one", query)
        return next((dict(doc) for doc in self.docs if _matches(doc, query)), None)

    def find(self, query=None):
        self._record("find", query)
        return FakeCursor([dict(doc) for doc in self.docs if _matches(doc, query)])

    async def insert_one(self, doc):
        self._record("insert_one", doc)
        self.docs.append(dict(doc))

    async def insert_many(self, docs):
        self._record("insert_many", list(docs))
        self.docs.extend(dict(doc) for doc in docs)

    async def update_one(self, query, update):
        self._record("update_one", query, update)
        for doc in self.docs:
            if _matches(doc, query):
                doc.update(update.get("$set", {}))
                return

    async def update_many(self, query, update):
        self._record("update_many", query, update)
        for doc in self.docs:
            if _matches(doc, query):
                doc.update(update.get("$set", {}))

    async def delete_one(self, query):
        self._record("delete_one", query)
        for index, doc in enumerate(self.docs):
            if _matches(doc, query):
                del self.docs[index]
                return

    async def delete_many(self, query):
        self._record("delete_many", query)
        self.docs[:] = [doc for doc in self.docs if not _matches(doc, query)]

    async def count_documents(self, query):
        self._record("count_documents", query)
        return sum(1 for doc in self.docs if _matches(doc, query))

    def aggregate(self, pipeline):
        self._record("aggregate", pipeline)
        return FakeCursor([dict(doc) for doc in self.docs])


class FakeDatabase:
    def __init__(self, client, name):
        self.client = client
        self.name = name
        self.docs = {}
        self.calls = []

    def __getitem__(self, name):
        return FakeCollection(self, name)

    def list_collection_names(self):
        return list(self.docs)


class FakeAdmin:
    def __init__(self, client):
        self.client = client

    async def command(self, name):
        self.client.pings.append(name)
        return {"ok": 1}


class FakeBindContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        self.session.bind_entered += 1
        self.session.client.bound_session = self.session
        return self.session

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.session.client.bound_session = None
        self.session.bind_exited += 1
        return False


class FakeSession:
    def __init__(self, client):
        self.client = client
        self.started = False
        self.committed = False
        self.aborted = False
        self.ended = False
        self.bind_entered = 0
        self.bind_exited = 0

    async def start_transaction(self):
        self.started = True
        return SimpleNamespace()

    def bind(self, end_session=True):
        return FakeBindContext(self)

    async def commit_transaction(self):
        self.committed = True

    async def abort_transaction(self):
        self.aborted = True

    async def end_session(self):
        self.ended = True


class FakeMongoClient:
    def __init__(self, uri, **options):
        self.uri = uri
        self.options = options
        self.databases = {}
        self.calls = []
        self.pings = []
        self.sessions = []
        self.bound_session = None
        self.closed = False
        self.admin = FakeAdmin(self)

    def __getitem__(self, name):
        if name not in self.databases:
            self.databases[name] = FakeDatabase(self, name)
        return self.databases[name]

    def start_session(self):
        session = FakeSession(self)
        self.sessions.append(session)
        return session

    async def close(self):
        self.closed = True


def _handle(db, shape, name):
    return db if shape == "string" else db[name]


@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.parametrize("mount", ["app", "router"])
@pytest.mark.parametrize("shape", ["string", "list"])
@pytest.mark.asyncio
async def test_mongo_driver_and_fetch_methods_on_app_and_router(
    shape, mount, protocol, app_factory, asgi_client, rsgi_client, monkeypatch,
):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.MongoDB)
    client = FakeMongoClient("mongodb://example")
    app = app_factory()
    app.DB_POOL = _MongoPool(client, "flameo" if shape == "string" else ["flameo"])
    router = Router()

    async def handler(req, res, db):
        handle = _handle(db, shape, "flameo")
        users = handle["users"]
        await users.insert_one({"id": 1, "name": "a"})
        await users.insert_many([{"id": 2, "name": "b"}, {"id": 3, "name": "c"}])
        await users.update_one({"id": 1}, {"$set": {"name": "updated"}})
        await users.update_many({"id": {"$gt": 1}}, {"$set": {"active": True}})
        row = await handle.fetchrow("users", {"id": 1})
        rows = await handle.fetch("users", {})
        count = await users.count_documents({})
        await users.delete_one({"id": 2})
        await res.json({
            "row": row, "rows": rows, "count": count,
            "after_delete": await users.count_documents({}),
            "collections": handle.list_collection_names(),
        })

    if mount == "app":
        app.get("/mongo", handler)
        path = "/mongo"
    else:
        router.get("/mongo", handler)
        app.use_router("/api", router)
        path = "/api/mongo"

    client_http = asgi_client if protocol == "asgi" else rsgi_client
    response = await client_http(app, path=path)
    assert response["status"] == 200
    body = json.loads(response["body"])
    assert body["row"] == {"id": 1, "name": "updated"}
    assert [row["id"] for row in body["rows"]] == [1, 2, 3]
    assert body["count"] == 3
    assert body["after_delete"] == 2
    assert body["collections"] == ["users"]


@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.parametrize("mount", ["app", "router"])
@pytest.mark.parametrize("shape", ["string", "list"])
@pytest.mark.asyncio
async def test_mongo_transaction_commit_and_rollback_on_app_and_router(
    shape, mount, protocol, app_factory, asgi_client, rsgi_client, monkeypatch,
):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.MongoDB)
    client = FakeMongoClient("mongodb://example")
    app = app_factory()
    app.DB_POOL = _MongoPool(client, "flameo" if shape == "string" else ["flameo"])
    router = Router()

    async def handler(req, res, db):
        handle = _handle(db, shape, "flameo")
        async with db.transaction():
            await handle["users"].insert_one({"id": 10})
        committed = client.sessions[-1].committed
        aborted = False
        try:
            async with db.transaction():
                await handle["users"].insert_one({"id": 20})
                raise RuntimeError("rollback")
        except TransactionError:
            aborted = client.sessions[-1].aborted
        await res.json({"committed": committed, "aborted": aborted})

    if mount == "app":
        app.get("/mongo/tx", handler)
        path = "/mongo/tx"
    else:
        router.get("/mongo/tx", handler)
        app.use_router("/api", router)
        path = "/api/mongo/tx"

    client_http = asgi_client if protocol == "asgi" else rsgi_client
    response = await client_http(app, path=path)
    assert response["status"] == 200
    assert json.loads(response["body"]) == {"committed": True, "aborted": True}


def _mongo_config():
    uri = os.environ.get("FLAMEO_TEST_MONGODB_URI")
    database = os.environ.get("FLAMEO_TEST_MONGODB_DB")
    if not uri or not database:
        pytest.skip("Set FLAMEO_TEST_MONGODB_URI and FLAMEO_TEST_MONGODB_DB")
    return {
        "db_type": DBType.MongoDB,
        "mongo_uri": uri,
        "db": database,
        "min_size": 1,
        "max_size": 5,
    }


@pytest.mark.integration
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.parametrize("mount", ["app", "router"])
@pytest.mark.asyncio
async def test_live_mongodb_driver_and_transaction(
    protocol, mount, app_factory, asgi_client, rsgi_client,
):
    pytest.importorskip("pymongo")
    app = app_factory(db_config=_mongo_config())
    router = Router()
    collection = "flameo_mongo_check"

    async def handler(req, res, db):
        await db[collection].delete_many({})
        await db[collection].insert_many([
            {"_id": 1, "name": "a"},
            {"_id": 2, "name": "b"},
        ])
        await db[collection].update_one({"_id": 1}, {"$set": {"name": "updated"}})
        row = await db.fetchrow(collection, {"_id": 1})
        rows = await db.fetch(collection, {})
        await db[collection].delete_one({"_id": 2})

        async with db.transaction():
            await db[collection].insert_one({"_id": 10})

        try:
            async with db.transaction():
                await db[collection].insert_one({"_id": 20})
                raise RuntimeError("rollback")
        except TransactionError:
            rolled_back = True
        else:
            rolled_back = False

        count = await db[collection].count_documents({})
        await db[collection].delete_many({})
        await res.json({
            "row": row, "ids": [doc["_id"] for doc in rows],
            "count": count, "rolled_back": rolled_back,
        })

    if mount == "app":
        app.get("/mongo/live", handler)
        path = "/mongo/live"
    else:
        router.get("/mongo/live", handler)
        app.use_router("/api", router)
        path = "/api/mongo/live"

    client = asgi_client if protocol == "asgi" else rsgi_client
    try:
        await app.init_db_pool()
        response = await client(app, path=path)
    finally:
        await app.shutdown_db_pool()

    assert response["status"] == 200
    body = json.loads(response["body"])
    assert body["row"] == {"_id": 1, "name": "updated"}
    assert body["ids"] == [1, 2]
    assert body["count"] == 2
    assert body["rolled_back"] is True
