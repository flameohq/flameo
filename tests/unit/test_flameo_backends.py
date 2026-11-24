import asyncio
import builtins
import sys
from types import SimpleNamespace

import pytest

from flameo import DBType, Flameo, TransactionError
from flameo.flameo import QueryManager


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


class SyncRefCursor(FakeRefCursor):
    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def close(self):
        self.closed = True


class AsyncCursor:
    def __init__(self, rows=(), rowcount=1, description=(('id',),)):
        self.rows = list(rows)
        self.rowcount = rowcount
        self.description = description
        self.executed = []
        self.refcursor = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=()):
        if (getattr(self, "fail_returning", False) and "RETURNING" in sql) or \
                getattr(self, "fail_all", False):
            raise RuntimeError("server rejected RETURNING")
        self.executed.append((sql, params))
        if sql.startswith("SELECT @"):
            self.rows = [(7,)]

    async def executemany(self, sql, params):
        self.executed.append((sql, params))

    async def callproc(self, name, args):
        self.executed.append((f"CALL {name}", args))
        return args

    async def nextset(self):
        return False

    async def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    async def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    async def fetchmany(self, size):
        rows, self.rows = self.rows[:size], self.rows[size:]
        return rows

    def var(self, kind):
        if kind == "CURSOR":
            self.refcursor = FakeRefCursor()
            return SimpleNamespace(getvalue=lambda: self.refcursor)
        return SimpleNamespace(getvalue=lambda: f"{kind}-value")


class AsyncRawConnection:
    def __init__(self, rows=()):
        self.cursor_obj = AsyncCursor(rows=rows)
        self.commits = 0
        self.rollbacks = 0
        self.begins = 0

    def cursor(self):
        return self.cursor_obj

    async def begin(self):
        self.begins += 1

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class PgTransaction:
    def __init__(self):
        self.started = self.committed = self.rolled_back = False

    async def start(self):
        self.started = True

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class PgConnection:
    def __init__(self):
        self.tx = PgTransaction()
        self.cursor = SimpleNamespace(close=self._close_cursor)
        self.closed_cursor = False
        self.calls = []
        self.queries = []

    def transaction(self):
        return self.tx

    async def _close_cursor(self):
        self.closed_cursor = True

    async def fetchrow(self, sql, *params):
        self.queries.append(sql)
        self.calls.append(("fetchrow", params))
        return {"id": 1} if "empty" not in sql else None

    async def fetch(self, sql, *params):
        self.queries.append(sql)
        self.calls.append(("fetch", params))
        return [{"id": 1}, {"id": 2}]

    async def execute(self, sql, *params):
        self.queries.append(sql)
        self.calls.append(("execute", params))
        return "UPDATE 2"

    async def executemany(self, sql, params):
        return None


@pytest.mark.asyncio
async def test_query_manager_postgresql(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.PostgreSQL)
    raw = PgConnection()
    qm = QueryManager(raw)
    async with qm.transaction():
        assert await qm.fetchrow("SELECT") == {"id": 1}
    assert raw.tx.started and raw.tx.committed
    with pytest.raises(TransactionError) as info:
        async with qm.transaction():
            raise RuntimeError
    assert isinstance(info.value.cause, RuntimeError)
    assert info.value.rolled_back is True
    assert raw.tx.rolled_back
    assert await qm.fetch("SELECT") == [{"id": 1}, {"id": 2}]
    assert await qm.execute("UPDATE") == 2
    assert await qm.executemany("UPDATE", [(1,), (2,)]) == 2
    assert await qm.execute_returning("INSERT") == {"id": 1}
    assert await qm.fetchrow("SELECT :value", {"value": 1}) == {"id": 1}
    assert raw.calls[-1] == ("fetchrow", (1,))
    assert raw.queries[-1] == "SELECT $1"
    assert await qm.fetchrow("SELECT $1", (5,)) == {"id": 1}
    assert raw.calls[-1] == ("fetchrow", (5,))
    assert await qm.fetch("SELECT", (1,)) == [{"id": 1}, {"id": 2}]
    assert raw.calls[-1] == ("fetch", (1,))
    assert await qm.execute("UPDATE", (2,)) == 2
    assert raw.calls[-1] == ("execute", (2,))
    qm._cursor = raw.cursor
    await qm.close()
    assert raw.closed_cursor


@pytest.mark.asyncio
async def test_postgres_named_binds_are_translated(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.PostgreSQL)
    raw = PgConnection()
    qm = QueryManager(raw)
    sql = (
        "SELECT :A AS a, :B::text AS b, ':C' AS lit, 'it''s :A' AS escaped, "
        '"D :D" AS quoted -- comment :E\n /* :F */ FROM t '
        "WHERE x = :B AND y = :A"
    )
    rows = await qm.fetch(sql, {"A": 1, "B": 2, "EXTRA": 3})
    assert rows == [{"id": 1}, {"id": 2}]
    query, args = raw.queries[-1], raw.calls[-1][1]
    assert args == (1, 2)
    assert "SELECT $1 AS a, $2::text AS b" in query
    assert "':C'" in query and "'it''s :A'" in query and '"D :D"' in query
    assert "-- comment :E" in query and "/* :F */" in query
    assert query.count("$1") == 2 and query.count("$2") == 2

    for method in (qm.fetchrow, qm.fetch, qm.execute, qm.execute_returning):
        assert await method("SELECT :A", {"A": 1}) is not None
    assert raw.queries[-4:] == ["SELECT $1"] * 4

    with pytest.raises(ValueError, match="Missing value"):
        await qm.fetch("SELECT :MISSING", {"A": 1})
    with pytest.raises(ValueError, match="positional"):
        await qm.fetch("SELECT $1", {"A": 1})


@pytest.mark.asyncio
async def test_query_manager_mysql(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.MySQL)
    raw = AsyncRawConnection(rows=[(1,), (2,), (3,)])
    raw.cursor_obj.description = [("id",)]
    qm = QueryManager(raw)
    assert await qm.fetchrow("SELECT") == {"id": 1}
    assert await qm.fetch("SELECT", batch_size=2) == [{"id": 2}, {"id": 3}]
    assert await qm.execute("UPDATE") == 1
    assert await qm.executemany("UPDATE", [(1,), (2,)]) == 2
    with pytest.raises(ValueError, match="returning"):
        await qm.execute_returning("INSERT")
    raw.cursor_obj.rows = [(9,)]
    assert await qm.execute_returning("INSERT", returning="id") == {"id": 9}
    raw.cursor_obj.rows = [(9,)]
    assert await qm.execute_returning("INSERT", returning={"id": int}) == {"id": 9}
    assert raw.commits >= 4
    raw.cursor_obj.rows = []
    assert await qm.fetchrow("empty") is None


@pytest.mark.asyncio
async def test_query_manager_oracle(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.Oracle)
    raw = AsyncRawConnection(rows=[(1,), (2,)])
    raw.cursor_obj.description = [("id",)]
    qm = QueryManager(raw)
    assert await qm.fetchrow("SELECT") == {"id": 1}
    assert await qm.fetch("SELECT", batch_size=1) == [{"id": 2}]
    assert await qm.execute("UPDATE") == 1
    assert await qm.executemany("UPDATE", [(1,)]) == 1
    assert await qm.executemany("UPDATE", [(1,), (2,)]) == 2
    with pytest.raises(ValueError, match="returning"):
        await qm.execute_returning("INSERT")
    with pytest.raises(ValueError, match="dict"):
        await qm.execute_returning("INSERT", returning="id")
    with pytest.raises(ValueError, match="named-bind"):
        await qm.execute_returning("INSERT", [1], returning={"id": int})
    framework._python_to_oracle_type = {int: "NUMBER"}
    result = await qm.execute_returning("INSERT RETURNING id", returning={"id": int})
    assert result == {"id": "NUMBER-value"}
    with pytest.raises(ValueError, match="Unsupported Python type"):
        await qm.execute_returning("INSERT", returning={"id": object})
    assert raw.commits >= 2


@pytest.mark.asyncio
async def test_query_manager_oracle_transactions_and_empty_rows(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.Oracle)
    raw = AsyncRawConnection(rows=[])
    qm = QueryManager(raw)
    await qm.begin_transaction()
    await qm.commit_transaction()
    await qm.begin_transaction()
    await qm.rollback_transaction()
    assert raw.commits == 1 and raw.rollbacks == 1
    assert await qm.fetchrow("empty") is None
    assert await qm.fetch("empty") == []
    raw.cursor_obj.rows = [(1,)]
    assert await qm.fetchrow("cached") == {"id": 1}
    raw.cursor_obj.rows = [(2,)]
    assert await qm.fetchrow("cached") == {"id": 2}


@pytest.mark.asyncio
async def test_query_manager_empty_and_unsupported_paths(monkeypatch):
    import aiosqlite
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.SQLite)
    async with aiosqlite.connect(":memory:", isolation_level=None) as connection:
        qm = QueryManager(connection)
        await qm.execute("CREATE TABLE empty (id INTEGER)")
        assert await qm.fetch("SELECT * FROM empty") == []
        assert await qm.fetchrow("SELECT * FROM empty") is None
        assert await qm.execute_returning("SELECT * FROM empty") == {}
        assert await qm.fetch("SELECT * FROM empty") == []
    monkeypatch.setattr(framework, "_db_type", object())
    with pytest.raises(ValueError, match="Unsupported database"):
        await QueryManager(object()).execute_returning("SELECT 1")


@pytest.mark.asyncio
async def test_query_manager_transaction_idempotence_and_cursor_none(monkeypatch):
    import aiosqlite
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.SQLite)
    async with aiosqlite.connect(":memory:", isolation_level=None) as connection:
        qm = QueryManager(connection)
        await qm.begin_transaction()
        await qm.begin_transaction()
        await qm.commit_transaction()
        await qm.commit_transaction()
        await qm.rollback_transaction()
        await qm.rollback_transaction()
        assert qm._get_cursor() is None


@pytest.mark.asyncio
async def test_query_manager_unsupported_noop_branches(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", object())
    qm = QueryManager(object())
    await qm.begin_transaction()
    await qm.commit_transaction()
    await qm.rollback_transaction()
    assert await qm.fetchrow("SELECT") is None
    assert await qm.fetch("SELECT") is None
    assert await qm.execute("UPDATE") is None
    assert await qm.executemany("UPDATE", []) is None


@pytest.mark.asyncio
async def test_query_manager_transactional_write_branches(monkeypatch):
    import flameo.flameo as framework

    for db_type in (DBType.MySQL, DBType.Oracle):
        monkeypatch.setattr(framework, "_db_type", db_type)
        raw = AsyncRawConnection()
        qm = QueryManager(raw)
        await qm.begin_transaction()
        await qm.execute("UPDATE")
        await qm.executemany("UPDATE", [(1,)])
        assert raw.commits == 0
        await qm.rollback_transaction()

    monkeypatch.setattr(framework, "_db_type", DBType.SQLite)
    import aiosqlite
    async with aiosqlite.connect(":memory:", isolation_level=None) as connection:
        qm = QueryManager(connection)
        await qm.execute("CREATE TABLE writes (id INTEGER)")
        await qm.begin_transaction()
        await qm.execute("INSERT INTO writes VALUES (1)")
        await qm.executemany("INSERT INTO writes VALUES (?)", [(2,)])
        await qm.rollback_transaction()
        await qm.execute("INSERT INTO writes VALUES (3)")
        await qm.begin_transaction()
        assert await qm.execute_returning(
            "INSERT INTO writes VALUES (4) RETURNING id"
        ) == {"id": 4}
        await qm.rollback_transaction()


@pytest.mark.asyncio
async def test_sqlite_acquire_context_cleanup_edges(tmp_path):
    from flameo.flameo import _SQLiteAcquireContext

    empty = _SQLiteAcquireContext(str(tmp_path / "empty.db"))
    assert await empty.__aexit__(None, None, None) is False

    class BrokenConnection:
        async def close(self):
            raise RuntimeError("close failed")

    broken = _SQLiteAcquireContext(str(tmp_path / "broken.db"))
    broken._conn = BrokenConnection()
    assert await broken.__aexit__(None, None, None) is False


@pytest.mark.asyncio
async def test_query_manager_close_swallows_postgres_cursor_error(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.PostgreSQL)

    class BrokenCursor:
        async def close(self):
            raise RuntimeError("already closed")

    qm = QueryManager(object())
    qm._cursor = BrokenCursor()
    await qm.close()
    assert qm._closed
    await QueryManager(object()).close()


@pytest.mark.asyncio
async def test_query_manager_oracle_returning_inside_transaction(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.Oracle)
    framework._python_to_oracle_type = {int: "NUMBER"}
    raw = AsyncRawConnection()
    qm = QueryManager(raw)
    await qm.begin_transaction()
    assert await qm.execute_returning("BEGIN INSERT; END;", returning={"id": int}) == {
        "id": "NUMBER-value"
    }
    await qm.close()
    assert raw.rollbacks == 1


@pytest.mark.asyncio
async def test_query_manager_execute_returning_mysql_errors(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.MySQL)
    raw = AsyncRawConnection(rows=[(1,)])
    qm = QueryManager(raw)
    raw.cursor_obj.fail_returning = True
    with pytest.raises(ValueError, match="Original error: server rejected RETURNING"):
        await qm.execute_returning("INSERT RETURNING id", returning="id")
    raw.cursor_obj.rows = []
    assert await qm.execute_returning("INSERT", returning="id") == {}
    assert raw.rollbacks == 1
    raw.cursor_obj.fail_all = True
    with pytest.raises(RuntimeError, match="server rejected"):
        await qm.execute_returning("INSERT", returning="id")


@pytest.mark.asyncio
async def test_mysql_execute_returning_inside_transaction(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.MySQL)
    raw = AsyncRawConnection(rows=[(7,)])
    qm = QueryManager(raw)
    async with qm.transaction():
        assert await qm.execute_returning("INSERT", returning="id") == {"id": 7}
        assert raw.commits == 0
        assert raw.begins == 1
    assert raw.commits == 1

    raw = AsyncRawConnection(rows=[])
    qm = QueryManager(raw)
    raw.cursor_obj.fail_all = True
    with pytest.raises(TransactionError):
        async with qm.transaction():
            await qm.execute_returning("INSERT", returning="id")
    assert raw.rollbacks == 1
    assert raw.commits == 0


@pytest.mark.asyncio
async def test_nested_transaction_commits_and_rolls_back_once(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.MySQL)
    raw = AsyncRawConnection()
    qm = QueryManager(raw)
    async with qm.transaction():
        async with qm.transaction():
            await qm.execute("INSERT")
        assert raw.commits == 0
    assert raw.commits == 1
    assert qm._transaction is None and qm._tx_depth == 0

    raw = AsyncRawConnection()
    qm = QueryManager(raw)
    with pytest.raises(TransactionError):
        async with qm.transaction():
            async with qm.transaction():
                raise RuntimeError("boom")
    assert raw.rollbacks == 1
    assert raw.commits == 0
    assert qm._transaction is None and qm._tx_depth == 0


@pytest.mark.asyncio
async def test_oracle_executemany_commit_scope(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.Oracle)
    raw = AsyncRawConnection()
    qm = QueryManager(raw)
    assert await qm.executemany("INSERT", [(1,), (2,)]) == 2
    assert raw.commits == 1
    await qm.begin_transaction()
    assert await qm.executemany("INSERT", [(3,)]) == 1
    assert raw.commits == 1
    await qm.commit_transaction()
    assert raw.commits == 2


@pytest.mark.asyncio
async def test_call_procedure_postgresql(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.PostgreSQL)
    raw = PgConnection()
    qm = QueryManager(raw)
    assert await qm.call_procedure("recalc", (5,)) == {}
    assert raw.queries[-1] == "CALL recalc($1)"
    assert raw.calls[-1] == ("execute", (5,))
    assert await qm.call_procedure("recalc", {"value": 5}) == {}
    assert raw.queries[-1] == "CALL recalc($1)"
    assert raw.calls[-1] == ("execute", (5,))
    assert await qm.call_procedure("get_status", (5,), out={"status": int}) == {
        "status": 1
    }
    assert raw.queries[-1] == "SELECT * FROM get_status($1)"
    with pytest.raises(ValueError, match="identifier"):
        await qm.call_procedure("recalc; DROP TABLE t", (1,))


@pytest.mark.asyncio
async def test_call_procedure_mysql(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.MySQL)
    raw = AsyncRawConnection(rows=[(7,)])
    qm = QueryManager(raw)
    assert await qm.call_procedure("calc", (5,), out={"status": int}) == {
        "status": 7
    }
    assert raw.cursor_obj.executed[-2] == ("CALL calc", [5])
    assert raw.cursor_obj.executed[-1] == ("SELECT @_calc_0", ())
    assert raw.commits == 1

    result = await qm.call_procedure(
        "calc", {"input": 5, "status": None}, out={"status": int}
    )
    assert result == {"status": 7}
    assert raw.cursor_obj.executed[-1] == ("SELECT @_calc_1", ())
    with pytest.raises(ValueError, match="must be present in params"):
        await qm.call_procedure("calc", {"input": 5}, out={"status": int})

    await qm.begin_transaction()
    assert await qm.call_procedure("calc", (1,), out={"status": int}) == {
        "status": 7
    }
    assert raw.commits == 2
    await qm.commit_transaction()


@pytest.mark.asyncio
async def test_call_procedure_oracle(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.Oracle)
    framework._python_to_oracle_type = {int: "NUMBER"}
    raw = AsyncRawConnection()
    qm = QueryManager(raw)
    result = await qm.call_procedure("charge", {"id": 7}, out={"status": int})
    assert result == {"status": "NUMBER-value"}
    name, args = raw.cursor_obj.executed[-1]
    assert name == "CALL charge"
    assert args[0] == 7 and hasattr(args[1], "getvalue")
    assert raw.commits == 1


@pytest.mark.asyncio
async def test_call_procedure_sqlite_raises(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.SQLite)
    with pytest.raises(NotImplementedError, match="no stored procedures"):
        await QueryManager(object()).call_procedure("charge")


@pytest.mark.asyncio
async def test_call_procedure_returns_rows_postgresql(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.PostgreSQL)
    raw = PgConnection()
    qm = QueryManager(raw)
    result = await qm.call_procedure(
        "list_items", (5,), out={"status": int}, returns_rows=True
    )
    assert result == {"out": {"status": 1}, "rows": [{"id": 1}, {"id": 2}]}
    assert raw.queries[-1] == "SELECT * FROM list_items($1)"
    with pytest.raises(ValueError, match="cannot return rows"):
        await qm.call_procedure("plain_proc", (5,), returns_rows=True)


@pytest.mark.asyncio
async def test_call_procedure_returns_rows_mysql(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.MySQL)
    raw = AsyncRawConnection(rows=[(1, "alpha"), (2, "beta")])
    raw.cursor_obj.description = (("id",), ("name",))
    qm = QueryManager(raw)
    expected_rows = [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]
    assert await qm.call_procedure("list_items", (5,), returns_rows=True) == {
        "out": {},
        "rows": expected_rows,
    }
    raw.cursor_obj.rows = [(1, "alpha"), (2, "beta")]
    result = await qm.call_procedure(
        "list_items", (5,), out={"status": int}, returns_rows=True
    )
    assert result == {"out": {"status": 7}, "rows": expected_rows}


@pytest.mark.asyncio
async def test_call_procedure_returns_rows_oracle(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.Oracle)
    monkeypatch.setattr(framework, "_oracle_cursor_type", "CURSOR")
    framework._python_to_oracle_type = {int: "NUMBER"}
    raw = AsyncRawConnection()
    qm = QueryManager(raw)
    expected_rows = [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]
    assert await qm.call_procedure("list_items", (5,), returns_rows=True) == {
        "out": {},
        "rows": expected_rows,
    }
    assert raw.cursor_obj.refcursor.closed
    result = await qm.call_procedure(
        "list_items", {"id": 5}, out={"status": int}, returns_rows=True
    )
    assert result == {"out": {"status": "NUMBER-value"}, "rows": expected_rows}
    monkeypatch.setattr(framework, "_oracle_cursor_type", None)
    with pytest.raises(ValueError, match="init_db_pool"):
        await qm.call_procedure("list_items", (), returns_rows=True)


@pytest.mark.asyncio
async def test_ref_cursor_oracle_named_params(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.Oracle)
    monkeypatch.setattr(framework, "_oracle_cursor_type", "CURSOR")
    raw = AsyncRawConnection()
    qm = QueryManager(raw)
    rows = await qm.ref_cursor("flameo_test_items", {"min_id": 1})
    assert rows == [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]
    name, args = raw.cursor_obj.executed[-1]
    assert name == "CALL flameo_test_items"
    assert args[0] == 1
    assert hasattr(args[1], "getvalue")
    assert args[1].getvalue().closed
    assert raw.commits == 1


@pytest.mark.asyncio
async def test_ref_cursor_oracle_positional_params(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.Oracle)
    monkeypatch.setattr(framework, "_oracle_cursor_type", "CURSOR")
    raw = AsyncRawConnection()
    qm = QueryManager(raw)
    assert await qm.ref_cursor("flameo_test_items", (1,)) == [
        {"id": 1, "name": "alpha"},
        {"id": 2, "name": "beta"},
    ]
    name, args = raw.cursor_obj.executed[-1]
    assert name == "CALL flameo_test_items"
    assert args[0] == 1
    assert hasattr(args[1], "getvalue")


@pytest.mark.asyncio
async def test_ref_cursor_rejects_invalid_input(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.Oracle)
    monkeypatch.setattr(framework, "_oracle_cursor_type", "CURSOR")
    qm = QueryManager(AsyncRawConnection())
    with pytest.raises(ValueError, match="identifier"):
        await qm.ref_cursor("items; DROP TABLE t")
    monkeypatch.setattr(framework, "_oracle_cursor_type", None)
    with pytest.raises(ValueError, match="init_db_pool"):
        await qm.ref_cursor("flameo_test_items")
    for db_type in (DBType.PostgreSQL, DBType.MySQL, DBType.SQLite):
        monkeypatch.setattr(framework, "_db_type", db_type)
        with pytest.raises(NotImplementedError, match="only supported on Oracle"):
            await qm.ref_cursor("flameo_test_items")


@pytest.mark.asyncio
async def test_ref_cursor_close_accepts_sync_driver_cursor(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.Oracle)
    monkeypatch.setattr(framework, "_oracle_cursor_type", "CURSOR")
    framework._python_to_oracle_type = {int: "NUMBER"}
    raw = AsyncRawConnection()
    cursors = []

    def fake_var(kind):
        if kind == "CURSOR":
            refcur = SyncRefCursor()
            cursors.append(refcur)
            return SimpleNamespace(getvalue=lambda: refcur)
        return SimpleNamespace(getvalue=lambda: f"{kind}-value")

    raw.cursor_obj.var = fake_var
    qm = QueryManager(raw)
    expected_rows = [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]
    assert await qm.ref_cursor("flameo_test_items", (1,)) == expected_rows
    result = await qm.call_procedure(
        "flameo_test_list_items", (1,), out={"status": int}, returns_rows=True
    )
    assert result == {"out": {"status": "NUMBER-value"}, "rows": expected_rows}
    assert len(cursors) == 2
    assert all(cursor.closed for cursor in cursors)


@pytest.mark.asyncio
async def test_ref_cursor_respects_transaction(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.Oracle)
    monkeypatch.setattr(framework, "_oracle_cursor_type", "CURSOR")
    raw = AsyncRawConnection()
    qm = QueryManager(raw)
    await qm.begin_transaction()
    await qm.ref_cursor("flameo_test_items", (1,))
    assert raw.commits == 0
    await qm.commit_transaction()
    assert raw.commits == 1


@pytest.mark.asyncio
async def test_oracle_close_rolls_back_leftover_nested_transaction(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.Oracle)
    raw = AsyncRawConnection()
    qm = QueryManager(raw)
    await qm.begin_transaction()
    await qm.begin_transaction()
    assert qm._tx_depth == 1
    await qm.close()
    assert qm._closed
    assert raw.rollbacks == 1
    assert qm._transaction is None and qm._tx_depth == 0


@pytest.mark.asyncio
async def test_postgres_execute_status_returns_rowcount(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.PostgreSQL)

    class StatusConnection:
        def __init__(self, status):
            self.status = status

        async def execute(self, sql, *params):
            return self.status

    for status, expected in (("UPDATE 2", 2), ("INSERT 0 1", 1),
                             ("DELETE 0", 0), ("CREATE TABLE", 0), (None, 0)):
        assert await QueryManager(StatusConnection(status)).execute("SQL") == expected


@pytest.mark.asyncio
async def test_execute_returning_rejects_invalid_returning_shapes(monkeypatch):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.MySQL)
    qm = QueryManager(AsyncRawConnection())
    with pytest.raises(ValueError, match="single-key"):
        await qm.execute_returning("INSERT", returning={"a": int, "b": int})
    with pytest.raises(ValueError, match="returning"):
        await qm.execute_returning("INSERT", returning=5)

    monkeypatch.setattr(framework, "_db_type", DBType.Oracle)
    qm = QueryManager(AsyncRawConnection())
    with pytest.raises(ValueError, match="dict"):
        await qm.execute_returning("INSERT", returning="id")


@pytest.mark.asyncio
async def test_fetch_helpers_reject_statements_without_rows(monkeypatch):
    import aiosqlite
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.SQLite)
    async with aiosqlite.connect(":memory:", isolation_level=None) as connection:
        qm = QueryManager(connection)
        await qm.execute("CREATE TABLE t (id INTEGER)")
        with pytest.raises(ValueError, match="execute"):
            await qm.fetchrow("INSERT INTO t VALUES (1)")
        with pytest.raises(ValueError, match="execute"):
            await qm.fetch("INSERT INTO t VALUES (2)")

    for db_type in (DBType.MySQL, DBType.Oracle):
        monkeypatch.setattr(framework, "_db_type", db_type)
        raw = AsyncRawConnection()
        raw.cursor_obj.description = None
        qm = QueryManager(raw)
        with pytest.raises(ValueError, match="execute"):
            await qm.fetchrow("INSERT")
        with pytest.raises(ValueError, match="execute"):
            await qm.fetch("INSERT")


@pytest.mark.asyncio
async def test_init_db_pool_sqlite_and_missing_path(app_factory, tmp_path, capsys):
    app = app_factory(db_config={"db_type": DBType.SQLite, "file_path": str(tmp_path / "x.db")})
    await app.init_db_pool()
    assert app.DB_POOL is not None
    assert "Database connected successfully" in capsys.readouterr().out
    await app.init_db_pool()
    await app.shutdown_db_pool()

    bad = app_factory(db_config={"db_type": DBType.SQLite})
    with pytest.raises(ValueError, match="file_path"):
        await bad.init_db_pool()


@pytest.mark.asyncio
async def test_init_db_pool_sqlite_driver_import_error(app_factory, monkeypatch):
    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "aiosqlite":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    app = app_factory(db_config={"db_type": DBType.SQLite, "file_path": "x.db"})
    with pytest.raises(ValueError, match="aiosqlite"):
        await app.init_db_pool()


@pytest.mark.asyncio
async def test_init_db_pool_driver_import_errors(app_factory, monkeypatch):
    import flameo.flameo as framework

    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name in {"asyncpg", "asyncmy", "oracledb"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    for db_type, text in ((DBType.PostgreSQL, "asyncpg"),
                          (DBType.MySQL, "asyncmy"),
                          (DBType.Oracle, "oracledb")):
        app = app_factory(db_config={"db_type": db_type})
        with pytest.raises(ValueError, match=text):
            await app.init_db_pool()


class PoolContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *exc):
        return False


class InitPool:
    def __init__(self, connection):
        self.connection = connection
        self.closed = False

    def acquire(self):
        return PoolContext(self.connection)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_init_db_pool_fake_postgres_mysql_oracle(app_factory, monkeypatch, capsys):
    class PgConn:
        async def execute(self, sql):
            assert sql == "SELECT 1"

    async def create_pg_pool(**kwargs):
        return InitPool(PgConn())

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(create_pool=create_pg_pool))
    pg = app_factory(db_config={"db_type": DBType.PostgreSQL})
    await pg.init_db_pool()
    assert pg.DB_POOL is not None
    await pg.shutdown_db_pool()

    class MyCursor(AsyncCursor):
        async def fetchone(self):
            return ("8.0",)

    class MyConn:
        async def query(self, sql):
            assert sql == "SELECT 1"

        def cursor(self):
            return MyCursor()

    async def create_mysql_pool(**kwargs):
        return InitPool(MyConn())

    monkeypatch.setitem(sys.modules, "asyncmy", SimpleNamespace(create_pool=create_mysql_pool))
    mysql = app_factory(db_config={"db_type": DBType.MySQL})
    await mysql.init_db_pool()
    await mysql.shutdown_db_pool()
    assert "Database connected successfully" in capsys.readouterr().out

    class OracleConn:
        async def execute(self, sql):
            assert sql == "SELECT 1 FROM DUAL"

    oracle_module = SimpleNamespace(
        CURSOR="CURSOR", NUMBER="NUMBER", STRING="STRING", BLOB="BLOB", DATETIME="DATETIME",
        makedsn=lambda host, port, service_name: f"{host}:{port}/{service_name}",
        create_pool_async=lambda **kwargs: InitPool(OracleConn()),
    )
    monkeypatch.setitem(sys.modules, "oracledb", oracle_module)
    oracle = app_factory(db_config={"db_type": DBType.Oracle})
    await oracle.init_db_pool()
    assert oracle.DB_POOL is not None


@pytest.mark.asyncio
async def test_init_db_pool_requires_db_type(app_factory):
    app = app_factory(db_config={"host": "localhost"})
    with pytest.raises(ValueError, match="db_type"):
        await app.init_db_pool()


@pytest.mark.asyncio
async def test_init_db_pool_ignores_unknown_db_type(app_factory):
    app = app_factory(db_config={"db_type": object()})
    await app.init_db_pool()
    assert app.DB_POOL is None


@pytest.mark.asyncio
async def test_init_cache_pool_fake_redis(app_factory, monkeypatch, capsys):
    class Pool:
        def __init__(self):
            self.disconnected = False

        async def disconnect(self):
            self.disconnected = True

    class Client:
        def __init__(self, connection_pool):
            self.pool = connection_pool
            self.pinged = False

        async def ping(self):
            self.pinged = True

        async def aclose(self):
            self.closed = True

    pool = Pool()
    redis = SimpleNamespace(
        BlockingConnectionPool=SimpleNamespace(from_url=lambda *a, **k: pool),
        Redis=Client,
    )
    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(asyncio=redis))
    monkeypatch.setitem(sys.modules, "redis.asyncio", redis)
    from flameo import CacheType
    app = app_factory(cache_config={"cache_type": CacheType.Redis,
                                    "max_connections": 2,
                                    "cache_url": "redis://cache"})
    await app.init_cache_pool()
    assert app.CACHE.pinged
    await app.shutdown_cache_pool()
    assert pool.disconnected
    assert "Cache connected successfully" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_init_cache_pool_reports_connection_error(app_factory, monkeypatch, capsys):
    class BrokenClient:
        async def ping(self):
            raise RuntimeError("cache down")

    redis = SimpleNamespace(
        BlockingConnectionPool=SimpleNamespace(from_url=lambda *a, **k: object()),
        Redis=lambda **kwargs: BrokenClient(),
    )
    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(asyncio=redis))
    monkeypatch.setitem(sys.modules, "redis.asyncio", redis)
    from flameo import CacheType
    app = app_factory(cache_config={"cache_type": CacheType.Redis,
                                    "max_connections": 1,
                                    "cache_url": "redis://cache"})
    with pytest.raises(RuntimeError, match="cache down"):
        await app.init_cache_pool()
    assert "Cache connection failed" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_fetchrow_and_fetch_bind_plain_model_sqlite(monkeypatch):
    import aiosqlite
    import flameo.flameo as framework

    class Record:
        identifier: int
        label: str = "default"

    monkeypatch.setattr(framework, "_db_type", DBType.SQLite)
    async with aiosqlite.connect(":memory:", isolation_level=None) as connection:
        await connection.execute("CREATE TABLE items (identifier INTEGER, label TEXT)")
        await connection.executemany(
            "INSERT INTO items VALUES (?, ?)", [(7, "alpha"), (8, "beta")]
        )
        qm = QueryManager(connection)
        row = await qm.fetchrow(
            "SELECT IDENTIFIER, LABEL, 99 AS IGNORED FROM items WHERE identifier = 7",
            model=Record,
        )
        assert isinstance(row, Record)
        assert (row.identifier, row.label) == (7, "alpha")
        rows = await qm.fetch(
            "SELECT IDENTIFIER, LABEL FROM items", batch_size=1, model=Record
        )
        assert [r.identifier for r in rows] == [7, 8]
        assert all(isinstance(r, Record) for r in rows)
        assert await qm.fetchrow("SELECT * FROM items WHERE identifier = 9",
                                 model=Record) is None
        assert await qm.fetch("SELECT * FROM items WHERE identifier = 9",
                              model=Record) is None
        assert await qm.fetch("SELECT * FROM items WHERE identifier = 9") == []
        assert await qm.fetchrow("SELECT * FROM items WHERE identifier = 7") == {
            "identifier": 7, "label": "alpha",
        }
        for alias in (qm.fetchOne, qm.fetchRow, qm.findOne):
            assert isinstance(await alias("SELECT * FROM items", model=Record), Record)
        for alias in (qm.fetchMany, qm.fetchAll, qm.findAll):
            assert len(await alias("SELECT * FROM items", model=Record)) == 2
        with pytest.raises(TypeError, match="batch_size"):
            await qm.fetch("SELECT * FROM items", batch_size="1", model=Record)


@pytest.mark.asyncio
async def test_fetchrow_and_fetch_bind_plain_model_postgresql(monkeypatch):
    import flameo.flameo as framework

    class Record:
        id: int

    class PgRows:
        def __init__(self, rows):
            self.rows = rows
            self.calls = []

        async def fetchrow(self, sql, *args):
            self.calls.append((sql, args))
            return self.rows[0] if self.rows else None

        async def fetch(self, sql, *args):
            self.calls.append((sql, args))
            return list(self.rows)

    monkeypatch.setattr(framework, "_db_type", DBType.PostgreSQL)
    raw = PgRows([{"ID": "7"}])
    qm = QueryManager(raw)
    row = await qm.fetchrow("SELECT :value AS id", {"value": 7}, model=Record)
    assert isinstance(row, Record) and row.id == 7
    assert raw.calls[-1] == ("SELECT $1 AS id", (7,))
    rows = await qm.fetch("SELECT $1", (7,), model=Record)
    assert [r.id for r in rows] == [7]
    raw.rows = []
    assert await qm.fetchrow("SELECT", model=Record) is None
    assert await qm.fetch("SELECT", model=Record) is None
    assert await qm.fetch("SELECT") == []


@pytest.mark.parametrize("backend", [DBType.MySQL, DBType.Oracle])
@pytest.mark.asyncio
async def test_fetchrow_and_fetch_bind_plain_model_cursor_backends(backend, monkeypatch):
    import flameo.flameo as framework

    class Record:
        id: int
        label: str = "default"

    monkeypatch.setattr(framework, "_db_type", backend)
    raw = AsyncRawConnection(rows=[(7, "alpha"), (8, "beta")])
    raw.cursor_obj.description = (("ID",), ("LABEL",))
    qm = QueryManager(raw)
    row = await qm.fetchrow("SELECT", model=Record)
    assert isinstance(row, Record) and (row.id, row.label) == (7, "alpha")
    rows = await qm.fetch("SELECT", batch_size=1, model=Record)
    assert [(r.id, r.label) for r in rows] == [(8, "beta")]
    raw.cursor_obj.rows = []
    assert await qm.fetchrow("SELECT", model=Record) is None
    assert await qm.fetch("SELECT", model=Record) is None
