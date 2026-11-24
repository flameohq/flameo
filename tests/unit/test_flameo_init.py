"""Init-slice coverage for flameo/flameo.py.

Covers module-level helpers and app-construction kwargs:
  * _static_file_metadata (incl. no-mime fallback)
  * _static_request_not_modified branches
  * _State dunder/get/set/pop/update/clear/keys/values/items/to_dict
  * _inject_flags validation errors
  * route-method decorators (decorator + imperative forms)
  * use() middleware ordering
  * use_session / auth_header / docs_url / redoc_url / openapi_url validation
"""
from __future__ import annotations

import email.utils
from types import SimpleNamespace

import pytest

from flameo.flameo import (
    _State,
    _acquire_request_qm,
    _inject_flags,
    _mw_meta,
    _release_request_qm,
    _run_middlewares,
    _static_file_metadata,
    _static_request_not_modified,
)


def _params(*names):
    return [SimpleNamespace(name=n) for n in names]


# ---------------------------------------------------------------------------
# _static_file_metadata
# ---------------------------------------------------------------------------

class TestStaticFileMetadata:
    def test_known_mime_and_header_shape(self):
        mime, disposition, etag, last_modified = _static_file_metadata(
            "/srv/meta-page.html", 1234, 1_700_000_000_000_000_000
        )
        assert mime == b"text/html"
        assert etag == b'W/"4d2-17979cfe362a0000"'
        assert disposition.startswith(b"inline; filename=")
        assert b"meta-page.html" in disposition
        assert last_modified.endswith(b"GMT")

    def test_unknown_extension_falls_back_to_text_plain(self):
        mime, disposition, etag, last_modified = _static_file_metadata(
            "/srv/blob.verbosely-unknown-ext", 16, 1_700_000_001_000_000_000
        )
        assert mime == b"text/plain; charset=utf-8"
        assert etag.startswith(b'W/"')
        assert last_modified.endswith(b"GMT")

    def test_non_ascii_basename_is_sanitized(self):
        mime, disposition, etag, last_modified = _static_file_metadata(
            "/srv/caf\xe9.txt", 8, 1_700_000_002_000_000_000
        )
        assert mime == b"text/plain"
        assert b"filename*=UTF-8''" in disposition


# ---------------------------------------------------------------------------
# _static_request_not_modified
# ---------------------------------------------------------------------------

def _req(**headers):
    return SimpleNamespace(
        headers={k.lower().replace("_", "-"): v for k, v in headers.items()}
    )


class TestStaticRequestNotModified:
    ETAG = b'W/"10-abc123"'
    MTIME_NS = 1_700_000_000_000_000_000

    def test_no_validators_is_unmodified_false(self):
        assert _static_request_not_modified(_req(), self.ETAG, self.MTIME_NS) is False

    def test_exact_etag_match(self):
        req = _req(if_none_match='"10-abc123"')
        assert _static_request_not_modified(req, self.ETAG, self.MTIME_NS) is True

    def test_weak_etag_match(self):
        req = _req(if_none_match='W/"10-abc123"')
        assert _static_request_not_modified(req, self.ETAG, self.MTIME_NS) is True

    def test_star_matches_anything(self):
        req = _req(if_none_match="*")
        assert _static_request_not_modified(req, self.ETAG, self.MTIME_NS) is True

    def test_list_with_one_match(self):
        req = _req(if_none_match='"stale", "10-abc123" , "other"')
        assert _static_request_not_modified(req, self.ETAG, self.MTIME_NS) is True

    def test_etag_mismatch_short_circuits_ims(self):
        ims = email.utils.formatdate(self.MTIME_NS / 1_000_000_000 + 100, usegmt=True)
        req = _req(if_none_match='"different"', if_modified_since=ims)
        assert _static_request_not_modified(req, self.ETAG, self.MTIME_NS) is False

    def test_ims_newer_than_mtime(self):
        ims = email.utils.formatdate(self.MTIME_NS / 1_000_000_000 + 60, usegmt=True)
        req = _req(if_modified_since=ims)
        assert _static_request_not_modified(req, self.ETAG, self.MTIME_NS) is True

    def test_ims_older_than_mtime(self):
        ims = email.utils.formatdate(self.MTIME_NS / 1_000_000_000 - 60, usegmt=True)
        req = _req(if_modified_since=ims)
        assert _static_request_not_modified(req, self.ETAG, self.MTIME_NS) is False

    def test_ims_naive_datetime_assumed_utc(self):
        req = _req(if_modified_since="Tue, 11 Nov 2025 00:00:00")
        assert _static_request_not_modified(req, self.ETAG, self.MTIME_NS) is True

    def test_ims_garbage_is_not_modified_false(self):
        req = _req(if_modified_since="not a date")
        assert _static_request_not_modified(req, self.ETAG, self.MTIME_NS) is False


# ---------------------------------------------------------------------------
# _State
# ---------------------------------------------------------------------------

class TestState:
    def test_attr_get_set_del_and_missing(self):
        st = _State({})
        assert st.anything is None
        st.answer = 42
        assert st.answer == 42
        del st.answer
        assert st.answer is None
        with pytest.raises(AttributeError):
            del st.nope

    def test_underscore_attr_raises(self):
        st = _State({})
        with pytest.raises(AttributeError):
            _ = st._hidden

    def test_store_slot_branches(self):
        st = _State({"a": 1})
        st._store = {"b": 2}
        assert st.b == 2
        del st._store
        with pytest.raises(AttributeError):
            _ = st._store

    def test_container_protocol(self):
        st = _State({})
        st.x = 1
        st.y = 2
        assert set(iter(st)) == {"x", "y"}
        assert len(st) == 2
        assert "x" in st
        assert "zzz" not in st
        assert repr(st) == "State({'x': 1, 'y': 2})"

    def test_dict_helpers(self):
        st = _State({})
        assert st.get("k") is None
        assert st.get("k", "dflt") == "dflt"
        assert st.setdefault("k", 5) == 5
        assert st.setdefault("k", 9) == 5
        assert st.pop("k") == 5
        assert st.pop("k", "fallback") == "fallback"
        with pytest.raises(KeyError):
            st.pop("k")
        st.update({"a": 1}, b=2)
        assert st.keys() == {"a": 1, "b": 2}.keys() or set(st.keys()) == {"a", "b"}
        assert set(st.values()) == {1, 2}
        assert dict(st.items()) == {"a": 1, "b": 2}
        assert st.to_dict() == {"a": 1, "b": 2}
        assert st.to_dict() is not st._store
        st.clear()
        assert len(st) == 0


# ---------------------------------------------------------------------------
# _inject_flags validation
# ---------------------------------------------------------------------------

class TestInjectFlags:
    def test_valid_combinations(self):
        assert _inject_flags(_params("req", "res"), "handler") == (False, False, False, False)
        assert _inject_flags(_params("req", "res", "db"), "handler") == (True, False, False, False)
        assert _inject_flags(_params("req", "res", "cache"), "handler") == (False, True, False, False)
        assert _inject_flags(_params("req", "res", "app"), "handler") == (False, False, True, False)
        assert _inject_flags(_params("req", "res", "state"), "handler") == (False, False, False, True)
        assert _inject_flags(
            _params("req", "res", "db", "cache"), "handler"
        ) == (True, True, False, False)

    def test_too_many_params(self):
        with pytest.raises(ValueError, match="at most 5 parameters"):
            _inject_flags(_params("req", "res", "db", "cache", "app", "extra"), "handler")

    def test_unknown_inject_name(self):
        with pytest.raises(ValueError, match="must be named 'db', 'cache', 'app', or 'state'"):
            _inject_flags(_params("req", "res", "bogus"), "handler")

    def test_duplicate_param_name(self):
        with pytest.raises(ValueError, match="Duplicate parameter name"):
            _inject_flags(_params("req", "res", "db", "db"), "handler")

    def test_app_and_state_cannot_coexist(self):
        with pytest.raises(ValueError, match="app and state cannot co-exist"):
            _inject_flags(_params("req", "res", "app", "state"), "handler")


@pytest.mark.asyncio
async def test_handler_signature_errors_surface_at_registration(app_factory):
    app = app_factory()

    with pytest.raises(ValueError, match="must be named"):
        @app.get("/bad-name")
        async def bad_name(req, res, bogus):
            pass  # pragma: no cover

    with pytest.raises(ValueError, match="app and state cannot co-exist"):
        @app.get("/app-state")
        async def app_state(req, res, app, state):
            pass  # pragma: no cover

    with pytest.raises(ValueError, match="at most 5 parameters"):
        @app.get("/too-many")
        async def too_many(req, res, db, cache, app, extra):
            pass  # pragma: no cover

    async def bad_mw_fn(req, res, bogus):
        pass  # pragma: no cover

    with pytest.raises(ValueError, match="must be named"):
        @app.get("/bad-route-mw", middlewares=[bad_mw_fn])
        async def route_with_bad_mw(req, res):
            pass  # pragma: no cover


# ---------------------------------------------------------------------------
# route-method decorators
# ---------------------------------------------------------------------------

METHODS = ["get", "post", "put", "patch", "delete", "options", "head", "query"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", METHODS)
async def test_route_method_decorators_register_and_respond(
    app_factory, asgi_client, method
):
    app = app_factory()
    register = getattr(app, method)
    path = f"/verb-{method}"

    @register(path)
    async def handler(req, res):
        await res.send(f"got-{method}")

    resp = await asgi_client(app, method=method.upper(), path=path)
    assert resp["status"] == 200
    assert resp["body"] == f"got-{method}".encode()


@pytest.mark.asyncio
async def test_route_imperative_form_with_positional_middleware(
    app_factory, asgi_client
):
    app = app_factory()
    calls = []

    async def mw(req, res):
        calls.append("mw")

    async def handler(req, res):
        calls.append("handler")
        await res.send("ok")

    returned = app.route("POST", "/imperative", mw, handler)
    assert returned is handler
    resp = await asgi_client(app, method="POST", path="/imperative")
    assert resp["status"] == 200
    assert calls == ["mw", "handler"]


@pytest.mark.asyncio
async def test_route_path_params_reach_handler(app_factory, asgi_client):
    app = app_factory()

    @app.get("/users/:id")
    async def by_id(req, res):
        await res.json({"id": req.params["id"]})

    @app.get("/files/{name}")
    async def by_name(req, res):
        await res.json({"name": req.params["name"]})

    first = await asgi_client(app, path="/users/7")
    assert first["body"] == b'{"id":"7"}'
    second = await asgi_client(app, path="/files/report")
    assert second["body"] == b'{"name":"report"}'


@pytest.mark.asyncio
async def test_route_requires_path(app_factory):
    app = app_factory()
    with pytest.raises(ValueError, match="At least a path must be provided"):
        app.route("GET")


@pytest.mark.asyncio
async def test_route_path_colliding_with_docs_url_rejected(app_factory):
    app = app_factory(docs_url="/docs")
    with pytest.raises(ValueError, match="collides with a docs URL"):
        @app.get("/docs")
        async def docs(req, res):
            pass  # pragma: no cover


# ---------------------------------------------------------------------------
# use() middleware ordering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_global_middlewares_run_in_registration_order(
    app_factory, asgi_client
):
    app = app_factory()
    order = []

    async def first(req, res):
        order.append("first")

    async def second(req, res):
        order.append("second")

    async def short_circuit(req, res):
        order.append("short")
        await res.send("stopped")

    app.use(first)
    app.use(second)

    @app.get("/open")
    async def open_route(req, res):
        order.append("handler")
        await res.send("ok")

    resp = await asgi_client(app, path="/open")
    assert resp["body"] == b"ok"
    assert order == ["first", "second", "handler"]

    order.clear()
    app.use(short_circuit)

    @app.get("/closed")
    async def closed_route(req, res):
        order.append("handler")  # pragma: no cover
        await res.send("unreached")  # pragma: no cover

    resp = await asgi_client(app, path="/closed")
    assert resp["body"] == b"stopped"
    assert order == ["first", "second", "short"]


@pytest.mark.asyncio
async def test_middleware_injection_slots(app_factory, asgi_client):
    app = app_factory()
    seen = {}

    async def mw_app(req, res, app):
        seen["is_app"] = app is req.app

    async def mw_state(req, res, state):
        seen["state"] = state.get("flag", "missing")
        state.flag = "set-by-mw"

    app.use(mw_app)
    app.use(mw_state)

    @app.get("/whoami")
    async def whoami(req, res, state):
        await res.json({"flag": state.flag, "same_state": state.get("flag")})

    resp = await asgi_client(app, path="/whoami")
    assert seen == {"is_app": True, "state": "missing"}
    assert resp["body"] == b'{"flag":"set-by-mw","same_state":"set-by-mw"}'


# ---------------------------------------------------------------------------
# use_session validation
# ---------------------------------------------------------------------------

class TestUseSessionValidation:
    def test_missing_secret_rejected(self, app_factory):
        with pytest.raises(ValueError, match="secret_key"):
            app_factory().use_session()

    def test_short_secret_rejected(self, app_factory):
        with pytest.raises(ValueError, match="secret_key"):
            app_factory().use_session(secret_key="short")

    def test_bad_max_age_rejected(self, app_factory):
        with pytest.raises(ValueError, match="max_age"):
            app_factory().use_session(secret_key="long-enough-secret", max_age="old")

    def test_bad_same_site_rejected(self, app_factory):
        with pytest.raises(ValueError, match="same_site"):
            app_factory().use_session(secret_key="long-enough-secret", same_site="sometimes")

    def test_bad_path_and_name_rejected(self, app_factory):
        app = app_factory()
        with pytest.raises(ValueError, match="'path'"):
            app.use_session(secret_key="long-enough-secret", path="")
        with pytest.raises(ValueError, match="'name'"):
            app.use_session(secret_key="long-enough-secret", name="")

    def test_valid_session_registers_middleware(self, app_factory):
        app = app_factory()
        before = len(app.global_middlewares)
        mw = app.use_session(secret_key="long-enough-secret")
        assert callable(mw)
        assert len(app.global_middlewares) == before + 1

    def test_camel_case_aliases_accepted(self, app_factory):
        app = app_factory()
        mw = app.use_session(secretKey="long-enough-secret", maxAge=60, sameSite="strict")
        assert callable(mw)


# ---------------------------------------------------------------------------
# auth_header + docs kwargs validation
# ---------------------------------------------------------------------------

class TestAppConstructorDocsValidation:
    def test_auth_header_true_becomes_authorization(self, app_factory):
        assert app_factory(auth_header=True).auth_header == "Authorization"

    def test_auth_header_custom_string_kept(self, app_factory):
        assert app_factory(auth_header="X-Api-Key").auth_header == "X-Api-Key"

    def test_auth_header_non_string_rejected(self, app_factory):
        with pytest.raises(ValueError, match="auth_header"):
            app_factory(auth_header=123)

    def test_docs_and_swagger_aliases_mutually_exclusive(self, app_factory):
        with pytest.raises(ValueError, match="only one of docs_url or swagger_url"):
            app_factory(docs_url="/docs", swagger_url="/swagger")

    def test_swagger_url_alias_enables_docs(self, app_factory):
        app = app_factory(swagger_url="/swagger")
        assert app._docs_ui_path == "/swagger"
        assert app._docs_openapi_path == "/openapi.json"

    def test_redoc_requires_docs_url(self, app_factory):
        with pytest.raises(ValueError, match="redoc_url requires docs_url"):
            app_factory(redoc_url="/redoc")

    def test_openapi_requires_docs_url(self, app_factory):
        with pytest.raises(ValueError, match="openapi_url requires docs_url"):
            app_factory(openapi_url="/spec.json")

    def test_openapi_defaults_when_only_docs_given(self, app_factory):
        app = app_factory(docs_url="/docs")
        assert app._docs_ui_path == "/docs"
        assert app._docs_openapi_path == "/openapi.json"
        assert app._docs_redoc_path is None

    def test_duplicate_docs_paths_rejected(self, app_factory):
        with pytest.raises(ValueError, match="distinct paths"):
            app_factory(docs_url="/docs", redoc_url="/docs")
        with pytest.raises(ValueError, match="distinct paths"):
            app_factory(docs_url="/docs", openapi_url="/docs")

    def test_full_docs_triplet_accepted(self, app_factory):
        app = app_factory(
            docs_url="/docs", redoc_url="/redoc", openapi_url="/spec.json"
        )
        assert app._docs_ui_path == "/docs"
        assert app._docs_redoc_path == "/redoc"
        assert app._docs_openapi_path == "/spec.json"

    def test_non_string_docs_path_rejected(self, app_factory):
        with pytest.raises(ValueError, match="docs_url/swagger_url"):
            app_factory(docs_url=123)

    def test_no_docs_by_default(self, app_factory):
        app = app_factory()
        assert app._docs_ui_path is None
        assert app._docs_registry is None


@pytest.mark.asyncio
async def test_docs_routes_serve_when_enabled(app_factory, asgi_client):
    app = app_factory(title="Init", version="1", docs_url="/docs")
    ui = await asgi_client(app, path="/docs")
    assert ui["status"] == 200
    spec = await asgi_client(app, path="/openapi.json")
    assert spec["status"] == 200


# ---------------------------------------------------------------------------
# _call_middleware sync path / _acquire+_release_request_qm / _run_middlewares
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_middleware_return_value_not_awaited(app_factory, asgi_client):
    app = app_factory()
    calls = []

    def sync_mw(req, res):
        calls.append("sync")

    app.use(sync_mw)

    @app.get("/sync-mw")
    async def handler(req, res):
        calls.append("handler")
        await res.send("ok")

    resp = await asgi_client(app, path="/sync-mw")
    assert resp["body"] == b"ok"
    assert calls == ["sync", "handler"]


def _qm_req(qm=None, pool=None, ctx=None):
    return SimpleNamespace(
        _db_query_manager=qm, _db_pool=pool, _db_acquire_context=ctx
    )


class TestAcquireRequestQm:
    @pytest.mark.asyncio
    async def test_existing_qm_same_pool_returned(self):
        qm, pool = object(), object()
        assert await _acquire_request_qm(_qm_req(qm, pool), pool) is qm

    @pytest.mark.asyncio
    async def test_existing_qm_other_pool_raises(self):
        with pytest.raises(RuntimeError, match="multiple database pools"):
            await _acquire_request_qm(_qm_req(object(), object()), object())

    @pytest.mark.asyncio
    async def test_no_qm_no_pool_returns_none(self):
        assert await _acquire_request_qm(_qm_req(), None) is None

    @pytest.mark.asyncio
    async def test_pool_acquire_binds_lease_to_request(self):
        class Ctx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *exc):
                return False

        class Pool:
            def acquire(self):
                self.ctx = Ctx()
                return self.ctx

        pool = Pool()
        req = _qm_req()
        qm = await _acquire_request_qm(req, pool)
        assert req._db_pool is pool
        assert req._db_query_manager is qm
        assert req._db_acquire_context is pool.ctx

    @pytest.mark.asyncio
    async def test_qm_construction_failure_releases_lease(self, monkeypatch):
        import flameo.flameo as flameo_mod

        exited = []

        class Ctx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *exc):
                exited.append(exc)
                return False

        class Pool:
            def acquire(self):
                return Ctx()

        def boom(conn):
            raise ConnectionError("nope")

        monkeypatch.setattr(flameo_mod, "QueryManager", boom)
        with pytest.raises(ConnectionError, match="nope"):
            await _acquire_request_qm(_qm_req(), Pool())
        assert exited and exited[0][0] is ConnectionError


class TestReleaseRequestQm:
    @pytest.mark.asyncio
    async def test_release_without_lease_is_noop(self):
        req = _qm_req(qm=object())
        assert await _release_request_qm(req) is None

    @pytest.mark.asyncio
    async def test_release_closes_and_clears(self):
        closed, exited = [], []

        class QM:
            async def close(self):
                closed.append(True)

        class Ctx:
            async def __aexit__(self, *exc):
                exited.append(exc)
                return False

        req = _qm_req(qm=QM(), pool=object(), ctx=Ctx())
        await _release_request_qm(req)
        assert closed == [True]
        assert exited == [(None, None, None)]
        assert req._db_pool is None
        assert req._db_acquire_context is None
        assert req._db_query_manager is None

    @pytest.mark.asyncio
    async def test_release_with_exc_passes_info_and_no_qm(self):
        exited = []

        class Ctx:
            async def __aexit__(self, *exc):
                exited.append(exc)
                return False

        req = _qm_req(pool=object(), ctx=Ctx())
        try:
            raise ValueError("boom")
        except ValueError as err:
            await _release_request_qm(req, err)
        assert exited[0][0] is ValueError
        assert isinstance(exited[0][1], ValueError)

    @pytest.mark.asyncio
    async def test_release_close_error_still_runs_aexit(self):
        exited = []

        class QM:
            async def close(self):
                raise RuntimeError("close-fail")

        class Ctx:
            async def __aexit__(self, *exc):
                exited.append(exc)
                return False

        req = _qm_req(qm=QM(), pool=object(), ctx=Ctx())
        with pytest.raises(RuntimeError, match="close-fail"):
            await _release_request_qm(req)
        assert len(exited) == 1


class TestRunMiddlewares:
    @pytest.mark.asyncio
    async def test_acquires_qm_when_middleware_needs_db(self):
        seen = []

        async def mw(req, res, db):
            seen.append(db)

        req = _qm_req()
        await _run_middlewares(
            [mw], [_mw_meta(mw)], req, None, qm=None,
            app=SimpleNamespace(DB_POOL=None), state=None,
        )
        assert seen == [None]

    @pytest.mark.asyncio
    async def test_no_db_middleware_uses_request_qm(self):
        ran = []

        async def mw(req, res):
            ran.append(True)

        req = _qm_req(qm="QM")
        await _run_middlewares(
            [mw], [_mw_meta(mw)], req, None, qm=None,
            app=SimpleNamespace(DB_POOL=None), state=None,
        )
        assert ran == [True]

    @pytest.mark.asyncio
    async def test_explicit_qm_skips_acquire(self):
        ran = []

        async def mw(req, res):
            ran.append(True)

        await _run_middlewares(
            [mw], [_mw_meta(mw)], _qm_req(), None, qm="QM",
            app=None, state=None,
        )
        assert ran == [True]


# ---------------------------------------------------------------------------
# route() cache-spec warn + filters env wraps
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_on_post_warns_decorator_form(app_factory, asgi_client):
    app = app_factory()
    with pytest.warns(UserWarning):
        @app.post("/cached-post")
        @app.cache(ttl=30)
        async def cached_post(req, res):
            await res.send("p")

    resp = await asgi_client(app, method="POST", path="/cached-post")
    assert resp["status"] == 200
    assert resp["body"] == b"p"


@pytest.mark.asyncio
async def test_cache_warn_and_filters_imperative_form(app_factory, asgi_client):
    app = app_factory()

    def shout(value):
        return str(value).upper()

    @app.cache(ttl=30)
    async def imp_cached(req, res):
        await res.send("imp")

    with pytest.warns(UserWarning):
        app.route("POST", "/imp-cached", imp_cached)

    @app.get("/filtered", filters=[shout])
    async def filtered(req, res):
        await res.send("f")

    async def imp_filtered(req, res):
        await res.send("g")

    app.route("GET", "/imp-filtered", imp_filtered, filters=[shout])

    cached = await asgi_client(app, method="POST", path="/imp-cached")
    assert cached["body"] == b"imp"
    assert (await asgi_client(app, path="/filtered"))["body"] == b"f"
    assert (await asgi_client(app, path="/imp-filtered"))["body"] == b"g"
