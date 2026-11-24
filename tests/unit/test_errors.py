import pytest

from flameo import DBType, FlameoError, TransactionError
from flameo.flameo import QueryManager


class RawConnection:
    def __init__(self, *, begin_error=None, commit_error=None, rollback_error=None):
        self.begin_error = begin_error
        self.commit_error = commit_error
        self.rollback_error = rollback_error
        self.begins = 0
        self.commits = 0
        self.rollbacks = 0

    async def begin(self):
        self.begins += 1
        if self.begin_error is not None:
            raise self.begin_error

    async def commit(self):
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self):
        self.rollbacks += 1
        if self.rollback_error is not None:
            raise self.rollback_error


def _manager(monkeypatch, raw):
    import flameo.flameo as framework

    monkeypatch.setattr(framework, "_db_type", DBType.MySQL)
    return QueryManager(raw)


def test_error_types_and_messages():
    assert issubclass(TransactionError, FlameoError)
    assert FlameoError.status == 500
    assert FlameoError.code == "flameo_error"
    assert str(FlameoError("boom")) == "boom"

    error = FlameoError("nope", status=409, code="conflict", details={"field": "id"})
    assert error.status == 409
    assert error.code == "conflict"
    assert error.details == {"field": "id"}


@pytest.mark.asyncio
async def test_transaction_body_error_raises_transaction_error(monkeypatch):
    raw = RawConnection()
    qm = _manager(monkeypatch, raw)

    with pytest.raises(TransactionError) as info:
        async with qm.transaction():
            raise RuntimeError("body exploded")

    error = info.value
    assert isinstance(error.cause, RuntimeError)
    assert error.rolled_back is True
    assert error.rollback_error is None
    assert "body exploded" in str(error)
    assert "was rolled back" in str(error)
    assert raw.rollbacks == 1
    assert qm._transaction is None


@pytest.mark.asyncio
async def test_transaction_commit_failure_rolls_back(monkeypatch):
    raw = RawConnection(commit_error=RuntimeError("commit exploded"))
    qm = _manager(monkeypatch, raw)

    with pytest.raises(TransactionError) as info:
        async with qm.transaction():
            pass

    error = info.value
    assert isinstance(error.cause, RuntimeError)
    assert error.rolled_back is True
    assert "commit exploded" in str(error)
    assert raw.commits == 1
    assert raw.rollbacks == 1
    assert qm._transaction is None


@pytest.mark.asyncio
async def test_transaction_commit_and_rollback_failure(monkeypatch):
    raw = RawConnection(
        commit_error=RuntimeError("commit exploded"),
        rollback_error=RuntimeError("rollback exploded"),
    )
    qm = _manager(monkeypatch, raw)

    with pytest.raises(TransactionError) as info:
        async with qm.transaction():
            pass

    error = info.value
    assert isinstance(error.cause, RuntimeError)
    assert error.rolled_back is False
    assert isinstance(error.rollback_error, RuntimeError)
    assert "commit exploded" in str(error)
    assert "rollback exploded" in str(error)
    assert "rollback also failed" in str(error)
    assert qm._transaction is None


@pytest.mark.asyncio
async def test_transaction_begin_failure(monkeypatch):
    raw = RawConnection(begin_error=RuntimeError("begin exploded"))
    qm = _manager(monkeypatch, raw)

    with pytest.raises(TransactionError) as info:
        async with qm.transaction():
            pass

    error = info.value
    assert isinstance(error.cause, RuntimeError)
    assert error.rolled_back is False
    assert "begin exploded" in str(error)
    assert raw.begins == 1
    assert raw.rollbacks == 0
    assert qm._transaction is None


@pytest.mark.asyncio
async def test_transaction_preserves_flameo_error_status(monkeypatch):
    class BusinessError(FlameoError):
        status = 409
        code = "insufficient_funds"

    raw = RawConnection()
    qm = _manager(monkeypatch, raw)

    with pytest.raises(TransactionError) as info:
        async with qm.transaction():
            raise BusinessError("not enough funds")

    error = info.value
    assert isinstance(error.cause, BusinessError)
    assert error.status == 409
    assert error.code == "insufficient_funds"
    assert "not enough funds" in str(error)
