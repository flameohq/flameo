from types import SimpleNamespace

import pytest

from flameo import DBType, Flameo, TransactionError
from flameo.flameo import QueryManager, _MongoPool, _mongo_client_options


def _matches(doc, query):
    return all(doc.get(key) == value for key, value in (query or {}).items())


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs
        self.length = "unset"

    async def to_list(self, length=None):
        self.length = length
        if length is None:
            return list(self.docs)
        return list(self.docs[:length])


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
        for doc in self.docs:
            if _matches(doc, query):
                return dict(doc)
        return None

    def find(self, query=None):
        self._record("find", query)
        return FakeCursor([dict(doc) for doc in self.docs if _matches(doc, query)])

    async def insert_one(self, doc):
        self._record("insert_one", doc)
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=len(self.docs))

    async def insert_many(self, docs):
        self._record("insert_many", list(docs))
        self.docs.extend(dict(doc) for doc in docs)
        return SimpleNamespace(inserted_ids=list(range(len(docs))))

    async def update_one(self, query, update):
        self._record("update_one", query, update)
        for doc in self.docs:
            if _matches(doc, query):
                doc.update(update.get("$set", {}))
                return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    async def update_many(self, query, update):
        self._record("update_many", query, update)
        count = 0
        for doc in self.docs:
            if _matches(doc, query):
                doc.update(update.get("$set", {}))
                count += 1
        return SimpleNamespace(modified_count=count)

    async def delete_one(self, query):
        self._record("delete_one", query)
        for index, doc in enumerate(self.docs):
            if _matches(doc, query):
                del self.docs[index]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def delete_many(self, query):
        self._record("delete_many", query)
        before = len(self.docs)
        self.docs[:] = [doc for doc in self.docs if not _matches(doc, query)]
        return SimpleNamespace(deleted_count=before - len(self.docs))

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
    def __init__(self, session, end_session):
        self.session = session
        self.end_session = end_session

    async def __aenter__(self):
        self.session.bind_entered += 1
        self.session.client.bound_session = self.session
        return self.session

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.session.client.bound_session = None
        self.session.bind_exited += 1
        if self.end_session:
            await self.session.end_session()
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
        return FakeBindContext(self, end_session)

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


@pytest.fixture
def mongo_type(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.MongoDB)


def test_mongo_client_options_defaults_and_aliases():
    options = _mongo_client_options({
        "db_type": DBType.MongoDB,
        "mongo_uri": "mongodb://example",
        "db": "flameo",
    })
    assert options == {
        "minPoolSize": 10,
        "maxPoolSize": 100,
        "maxConnecting": 5,
        "serverSelectionTimeoutMS": 5000,
        "connectTimeoutMS": 10000,
        "waitQueueTimeoutMS": 5000,
    }

    aliased = _mongo_client_options({
        "min_size": 2,
        "max_size": 40,
        "max_connecting": 8,
        "server_selection_timeout": 123,
        "connect_timeout": 456,
        "wait_queue": 789,
        "retryReads": False,
        "retryWrites": False,
    })
    assert aliased["minPoolSize"] == 2
    assert aliased["maxPoolSize"] == 40
    assert aliased["maxConnecting"] == 8
    assert aliased["serverSelectionTimeoutMS"] == 123
    assert aliased["connectTimeoutMS"] == 456
    assert aliased["waitQueueTimeoutMS"] == 789
    assert aliased["retryReads"] is False
    assert aliased["retryWrites"] is False


def test_mongo_client_options_canonical_names_win_or_conflict():
    options = _mongo_client_options({
        "minPoolSize": 3,
        "maxPoolSize": 30,
        "maxConnecting": 9,
        "serverSelectionTimeoutMS": 111,
        "connectTimeoutMS": 222,
        "waitQueueTimeoutMS": 333,
        "appname": "flameo-test",
    })
    assert options["minPoolSize"] == 3
    assert options["maxPoolSize"] == 30
    assert options["maxConnecting"] == 9
    assert options["serverSelectionTimeoutMS"] == 111
    assert options["connectTimeoutMS"] == 222
    assert options["waitQueueTimeoutMS"] == 333
    assert options["appname"] == "flameo-test"

    with pytest.raises(ValueError, match="cannot set both"):
        _mongo_client_options({"min_size": 5, "minPoolSize": 10})


def test_mongo_pool_request_handle_shapes():
    client = FakeMongoClient("mongodb://example")
    single = _MongoPool(client, "flameo")
    many = _MongoPool(client, ["flameo", "analytics"])
    assert single.request_handle() is client["flameo"]
    assert many.request_handle() is client


@pytest.mark.asyncio
async def test_init_mongo_pool_and_shutdown(app_factory, monkeypatch):
    import sys

    monkeypatch.setitem(
        sys.modules, "pymongo", SimpleNamespace(AsyncMongoClient=FakeMongoClient)
    )
    app = app_factory(db_config={
        "db_type": DBType.MongoDB,
        "mongo_uri": "mongodb://example",
        "db": ["flameo", "analytics"],
        "min_size": 3,
        "maxPoolSize": 25,
    })
    await app.init_db_pool()
    pool = app.DB_POOL
    assert isinstance(pool, _MongoPool)
    assert pool.client.uri == "mongodb://example"
    assert pool.client.options["minPoolSize"] == 3
    assert pool.client.options["maxPoolSize"] == 25
    assert pool.client.pings == ["ping"]
    await app.shutdown_db_pool()
    assert pool.client.closed


@pytest.mark.asyncio
async def test_init_mongo_pool_requires_uri_and_db(app_factory):
    with pytest.raises(ValueError, match="mongo_uri"):
        await app_factory(db_config={
            "db_type": DBType.MongoDB,
            "db": "flameo",
        }).init_db_pool()
    with pytest.raises(ValueError, match="'db'"):
        await app_factory(db_config={
            "db_type": DBType.MongoDB,
            "mongo_uri": "mongodb://example",
        }).init_db_pool()
    with pytest.raises(ValueError, match="'db'"):
        await app_factory(db_config={
            "db_type": DBType.MongoDB,
            "mongo_uri": "mongodb://example",
            "db": [],
        }).init_db_pool()


@pytest.mark.asyncio
async def test_mongo_fetchrow_and_fetch(mongo_type):
    client = FakeMongoClient("mongodb://example")
    database = client["flameo"]
    qm = QueryManager(database)
    await database["users"].insert_many([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])

    assert await qm.fetchrow("users", {"id": 1}) == {"id": 1, "name": "a"}
    assert await qm.fetchrow("users", {"id": 99}) is None
    assert await qm.fetch("users", {}) == [
        {"id": 1, "name": "a"}, {"id": 2, "name": "b"},
    ]
    assert await qm.fetch("users", {}, batch_size=1) == [{"id": 1, "name": "a"}]
    assert await qm.fetch("users", {"id": 99}) == []

    class User:
        id: int
        name: str

    bound = await qm.fetchrow("users", {"id": 2}, model=User)
    assert isinstance(bound, User) and bound.id == 2 and bound.name == "b"


@pytest.mark.asyncio
async def test_mongo_child_database_and_driver_methods(mongo_type):
    client = FakeMongoClient("mongodb://example")
    qm = QueryManager(client)
    with pytest.raises(ValueError, match="db\\[db_name\\]"):
        await qm.fetchrow("flameo", {})

    child = qm["flameo"]
    assert isinstance(child, QueryManager)
    assert await child.fetchrow("users", {"id": 1}) is None

    collection = child["users"]
    assert collection is not qm
    await collection.insert_one({"id": 1, "name": "a"})
    await collection.update_one({"id": 1}, {"$set": {"name": "b"}})
    assert await collection.count_documents({}) == 1
    assert await collection.find_one({"id": 1}) == {"id": 1, "name": "b"}
    assert len(await child.fetch("users", {})) == 1
    deleted = await collection.delete_many({})
    assert deleted.deleted_count == 1


@pytest.mark.asyncio
async def test_mongo_transaction_commit_and_rollback(mongo_type):
    client = FakeMongoClient("mongodb://example")
    database = client["flameo"]
    qm = QueryManager(database)

    async with qm.transaction():
        assert client.bound_session is not None
        await database["users"].insert_one({"id": 1})
    session = client.sessions[-1]
    assert session.started and session.committed and session.ended
    assert session.bind_entered == 1 and session.bind_exited == 1
    assert database.docs["users"] == [{"id": 1}]

    with pytest.raises(TransactionError):
        async with qm.transaction():
            assert client.bound_session is not None
            await database["users"].insert_one({"id": 2})
            raise RuntimeError("rollback")
    failed = client.sessions[-1]
    assert failed.started and failed.aborted and failed.ended
    assert failed.bind_entered == 1 and failed.bind_exited == 1


@pytest.mark.asyncio
async def test_mongo_transaction_on_client_handle(mongo_type):
    client = FakeMongoClient("mongodb://example")
    qm = QueryManager(client)
    async with qm.transaction():
        assert client.bound_session is not None
    session = client.sessions[-1]
    assert session.started and session.committed and session.ended


@pytest.mark.asyncio
async def test_mongo_close_ends_session_without_closing_client(mongo_type):
    client = FakeMongoClient("mongodb://example")
    qm = QueryManager(client["flameo"])
    await qm.begin_transaction()
    session = client.sessions[-1]
    await qm.close()
    assert session.aborted and session.ended
    assert client.closed is False
