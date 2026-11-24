from __future__ import annotations

import ast
from pathlib import Path

import pytest

from flameo import cli


@pytest.fixture(autouse=True)
def clean_granian_env(monkeypatch):
    for name in ("GRANIAN_HOST", "GRANIAN_PORT"):
        monkeypatch.delenv(name, raising=False)


def _compile_file(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _make_project(tmp_path: Path, name: str = "demo", **kwargs) -> Path:
    args = ["startproject", name, "--directory", str(tmp_path), "--no-input"]
    args.extend(kwargs.pop("extra", []))
    assert cli.main(args) == 0
    return tmp_path / name


def _capture_launch(monkeypatch):
    captured = {}

    def fake_launch(forwarded):
        captured["argv"] = list(forwarded)
        return 0

    monkeypatch.setattr(cli, "_launch_granian", fake_launch)
    return captured


# ---------------------------------------------------------------------------
# startproject
# ---------------------------------------------------------------------------


def test_startproject_default_creates_expected_files(tmp_path, capsys):
    project = _make_project(tmp_path)
    assert (project / "app.py").is_file()
    assert (project / "routers" / "__init__.py").is_file()
    assert (project / "README.md").is_file()
    assert (project / ".gitignore").is_file()
    assert (project / ".env").is_file()
    assert not (project / "tests").exists()
    assert not (project / "pyproject.toml").exists()
    assert not (project / ".env.example").exists()

    app_source = (project / "app.py").read_text(encoding="utf-8")
    assert "app = Flameo()" in app_source
    assert '@app.get("/health")' in app_source
    assert "flameo run app:app --reload" in (project / "README.md").read_text(encoding="utf-8")
    assert ".env" in (project / ".gitignore").read_text(encoding="utf-8")
    assert "No external services configured" in (project / ".env").read_text(encoding="utf-8")
    _compile_file(project / "app.py")
    assert "Created Flameo project" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("database", "enum_name", "env_key"),
    [
        ("sqlite", "DBType.SQLite", "DB_PATH=app.db"),
        ("postgresql", "DBType.PostgreSQL", "DB_PORT=5432"),
        ("mysql", "DBType.MySQL", "DB_PORT=3306"),
        ("oracle", "DBType.Oracle", "DB_PORT=1521"),
        ("mongodb", "DBType.MongoDB", "MONGO_URI="),
    ],
)
def test_startproject_database_options(tmp_path, database, enum_name, env_key):
    project = _make_project(
        tmp_path, database, extra=["--database", database]
    )
    app_source = (project / "app.py").read_text(encoding="utf-8")
    env_source = (project / ".env").read_text(encoding="utf-8")
    assert "DBType" in app_source
    assert enum_name in app_source
    assert "db_config=db_config" in app_source
    assert env_key in env_source
    _compile_file(project / "app.py")


@pytest.mark.parametrize(
    ("cache", "enum_name"),
    [("redis", "CacheType.Redis"), ("valkey", "CacheType.Valkey"), ("dragonfly", "CacheType.Dragonfly")],
)
def test_startproject_cache_options(tmp_path, cache, enum_name):
    project = _make_project(tmp_path, cache, extra=["--cache", cache])
    app_source = (project / "app.py").read_text(encoding="utf-8")
    env_source = (project / ".env").read_text(encoding="utf-8")
    assert "CacheType" in app_source
    assert enum_name in app_source
    assert "cache_config=cache_config" in app_source
    assert "CACHE_URL=redis://localhost:6379/0" in env_source
    _compile_file(project / "app.py")


def test_startproject_static_and_templates(tmp_path):
    project = _make_project(tmp_path, extra=["--static", "--templates"])
    app_source = (project / "app.py").read_text(encoding="utf-8")
    assert 'app.use_static("/static", "static")' in app_source
    assert 'app.use_template("templates")' in app_source
    assert 'await res.render("index.html", {"project": "demo"})' in app_source
    assert (project / "static" / ".gitkeep").is_file()
    assert (project / "templates" / "base.html").is_file()
    assert (project / "templates" / "index.html").is_file()
    _compile_file(project / "app.py")


def test_startproject_interactive_wizard(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_is_tty", lambda: True)
    answers = iter(["y", "2", "y", "2", "y", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    assert cli.main(["startproject", "wizard", "--directory", str(tmp_path)]) == 0
    project = tmp_path / "wizard"
    app_source = (project / "app.py").read_text(encoding="utf-8")
    assert "DBType.PostgreSQL" in app_source
    assert "CacheType.Valkey" in app_source
    assert "use_static" in app_source
    assert "use_template" in app_source
    assert (project / "static" / ".gitkeep").is_file()
    assert (project / "templates" / "index.html").is_file()
    _compile_file(project / "app.py")
    assert "Select database:" in capsys.readouterr().out


def test_startproject_invalid_prompt_reprompts(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_is_tty", lambda: True)
    answers = iter(["maybe", "n", "n", "n", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli.main(["startproject", "retry", "--directory", str(tmp_path)]) == 0
    app_source = (tmp_path / "retry" / "app.py").read_text(encoding="utf-8")
    assert "DBType" not in app_source
    assert "CacheType" not in app_source


def test_startproject_requires_no_input_when_not_tty(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_is_tty", lambda: False)
    assert cli.main(["startproject", "demo", "--directory", str(tmp_path)]) == 1
    assert not (tmp_path / "demo").exists()
    assert "--no-input" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "bad name", "bad.name"])
def test_startproject_rejects_invalid_names(tmp_path, name):
    assert cli.main(["startproject", name, "--directory", str(tmp_path), "--no-input"]) == 1


def test_startproject_rejects_and_forces_non_empty_directory(tmp_path, capsys):
    target = tmp_path / "demo"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    assert cli.main(["startproject", "demo", "--directory", str(tmp_path), "--no-input"]) == 1
    assert "not empty" in capsys.readouterr().err
    assert cli.main(
        ["startproject", "demo", "--directory", str(tmp_path), "--no-input", "--force"]
    ) == 0
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


# ---------------------------------------------------------------------------
# startapp
# ---------------------------------------------------------------------------


def test_startapp_creates_router_and_wires_generated_app(tmp_path, capsys):
    project = _make_project(tmp_path)
    assert cli.main(["startapp", "users", "--directory", str(project)]) == 0
    router_path = project / "routers" / "users.py"
    assert router_path.is_file()
    assert (project / "routers" / "__init__.py").is_file()
    app_source = (project / "app.py").read_text(encoding="utf-8")
    assert "from routers.users import router" in app_source
    assert 'app.use_router("/users", router)' in app_source
    _compile_file(project / "app.py")
    _compile_file(router_path)
    out = capsys.readouterr().out
    assert "Wired" in out and "use_router" in out


def test_startapp_is_idempotent(tmp_path):
    project = _make_project(tmp_path)
    assert cli.main(["startapp", "users", "--directory", str(project)]) == 0
    assert cli.main(
        ["startapp", "users", "--directory", str(project), "--force"]
    ) == 0
    app_source = (project / "app.py").read_text(encoding="utf-8")
    assert app_source.count("from routers.users import router") == 1
    assert app_source.count('app.use_router("/users", router)') == 1


@pytest.mark.parametrize(
    ("source", "variable"),
    [
        ("from flameo import Flameo\n\napplication = Flameo()\n", "application"),
        ("from flameo import Flameo as App\n\nserver = App()\n", "server"),
        ("import flameo\n\nbackend = flameo.Flameo()\n", "backend"),
        ("from flameo import Flameo\n\napp: Flameo = Flameo()\n", "app"),
    ],
)
def test_startapp_discovers_flameo_instances(tmp_path, source, variable):
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text(source, encoding="utf-8")
    assert cli.main(["startapp", "users", "--directory", str(project)]) == 0
    app_source = (project / "app.py").read_text(encoding="utf-8")
    assert f'{variable}.use_router("/users", router)' in app_source
    _compile_file(project / "app.py")


def test_startapp_package_relative_import(tmp_path):
    root = tmp_path / "project"
    package = root / "myapp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "app.py").write_text(
        "from flameo import Flameo\n\napp = Flameo()\n", encoding="utf-8"
    )
    assert cli.main(
        [
            "startapp",
            "users",
            "--directory",
            str(root),
            "--routers-dir",
            "myapp/routers",
        ]
    ) == 0
    app_source = (package / "app.py").read_text(encoding="utf-8")
    assert "from .routers.users import router" in app_source


def test_startapp_multiple_instances_noninteractive_falls_back(
    tmp_path, monkeypatch, capsys
):
    project = tmp_path / "project"
    project.mkdir()
    original = "from flameo import Flameo\n\nfirst = Flameo()\nsecond = Flameo()\n"
    (project / "app.py").write_text(original, encoding="utf-8")
    monkeypatch.setattr(cli, "_is_tty", lambda: False)
    assert cli.main(["startapp", "users", "--directory", str(project)]) == 0
    assert (project / "app.py").read_text(encoding="utf-8") == original
    captured = capsys.readouterr()
    assert "Multiple Flameo instances" in captured.err
    assert "--app-file" in captured.err
    assert "Mount the router manually" in captured.out


def test_startapp_multiple_instances_interactive_choice(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text(
        "from flameo import Flameo\n\nfirst = Flameo()\nsecond = Flameo()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_is_tty", lambda: True)
    answers = iter(["2"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert cli.main(["startapp", "users", "--directory", str(project)]) == 0
    app_source = (project / "app.py").read_text(encoding="utf-8")
    assert 'second.use_router("/users", router)' in app_source
    assert "first.use_router" not in app_source


def test_startapp_no_flameo_instance_falls_back(tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    original = "value = 1\n"
    (project / "app.py").write_text(original, encoding="utf-8")
    assert cli.main(["startapp", "users", "--directory", str(project)]) == 0
    assert (project / "app.py").read_text(encoding="utf-8") == original
    captured = capsys.readouterr()
    assert "No Flameo instance" in captured.err
    assert "from routers.users import router" in captured.out


def test_startapp_app_file_and_app_var(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "one.py").write_text(
        "from flameo import Flameo\n\none = Flameo()\n", encoding="utf-8"
    )
    (project / "two.py").write_text(
        "from flameo import Flameo\n\ntwo = Flameo()\n", encoding="utf-8"
    )
    assert cli.main(
        [
            "startapp",
            "users",
            "--directory",
            str(project),
            "--app-file",
            "two.py",
            "--app-var",
            "two",
        ]
    ) == 0
    assert 'two.use_router("/users", router)' in (project / "two.py").read_text(
        encoding="utf-8"
    )
    assert "use_router" not in (project / "one.py").read_text(encoding="utf-8")


def test_startapp_prefix_and_no_wire(tmp_path):
    project = _make_project(tmp_path)
    original = (project / "app.py").read_text(encoding="utf-8")
    assert cli.main(
        ["startapp", "users", "--directory", str(project), "--no-wire"]
    ) == 0
    assert (project / "app.py").read_text(encoding="utf-8") == original
    assert cli.main(
        [
            "startapp",
            "accounts",
            "--directory",
            str(project),
            "--prefix",
            "/v1/accounts",
        ]
    ) == 0
    app_source = (project / "app.py").read_text(encoding="utf-8")
    assert 'app.use_router("/v1/accounts", router)' in app_source


def test_startapp_existing_router_requires_force(tmp_path):
    project = _make_project(tmp_path)
    assert cli.main(["startapp", "users", "--directory", str(project)]) == 0
    assert cli.main(["startapp", "users", "--directory", str(project)]) == 1
    assert cli.main(
        ["startapp", "users", "--directory", str(project), "--force"]
    ) == 0


def test_startapp_rejects_invalid_names(tmp_path):
    project = _make_project(tmp_path)
    for name in ("bad-name", "class", "with space"):
        assert cli.main(["startapp", name, "--directory", str(project)]) == 1


def test_startapp_invalid_app_file_source_is_rejected(tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    original = "def broken(\n"
    (project / "app.py").write_text(original, encoding="utf-8")
    assert cli.main(
        ["startapp", "users", "--directory", str(project), "--app-file", "app.py"]
    ) == 1
    assert (project / "app.py").read_text(encoding="utf-8") == original
    assert "cannot parse" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# flameo run
# ---------------------------------------------------------------------------


def test_run_injects_interface_and_defaults(monkeypatch):
    captured = _capture_launch(monkeypatch)
    assert cli.main(["run", "app:app"]) == 0
    assert captured["argv"] == [
        "--interface",
        "rsgi",
        "--host",
        "127.0.0.1",
        "--port",
        "9000",
        "app:app",
    ]


def test_run_explicit_host_and_port_override_defaults(monkeypatch):
    captured = _capture_launch(monkeypatch)
    assert cli.main(
        ["run", "app:app", "--host", "0.0.0.0", "--port", "8080"]
    ) == 0
    assert captured["argv"] == [
        "--interface",
        "rsgi",
        "app:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8080",
    ]


def test_run_only_host_or_only_port(monkeypatch):
    captured = _capture_launch(monkeypatch)
    assert cli.main(["run", "app:app", "--host", "::1"]) == 0
    assert captured["argv"] == [
        "--interface",
        "rsgi",
        "--port",
        "9000",
        "app:app",
        "--host",
        "::1",
    ]
    assert cli.main(["run", "app:app", "--port", "9001"]) == 0
    assert captured["argv"] == [
        "--interface",
        "rsgi",
        "--host",
        "127.0.0.1",
        "app:app",
        "--port",
        "9001",
    ]


def test_run_equals_forms(monkeypatch):
    captured = _capture_launch(monkeypatch)
    assert cli.main(["run", "app:app", "--host=127.0.0.2", "--port=9002"]) == 0
    assert captured["argv"] == [
        "--interface",
        "rsgi",
        "app:app",
        "--host=127.0.0.2",
        "--port=9002",
    ]


def test_run_respects_granian_env(monkeypatch):
    captured = _capture_launch(monkeypatch)
    monkeypatch.setenv("GRANIAN_HOST", "0.0.0.0")
    monkeypatch.setenv("GRANIAN_PORT", "7000")
    assert cli.main(["run", "app:app"]) == 0
    assert captured["argv"] == ["--interface", "rsgi", "app:app"]


@pytest.mark.parametrize("host", ["localhost", "999.1.1.1", "example.com", "[::1]", ""])
def test_run_rejects_invalid_hosts(monkeypatch, host, capsys):
    captured = _capture_launch(monkeypatch)
    assert cli.main(["run", "app:app", "--host", host]) == 1
    assert "argv" not in captured
    assert "invalid host" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["0", "65536", "abc", "-1", "", "１２３"])
def test_run_rejects_invalid_ports(monkeypatch, port, capsys):
    captured = _capture_launch(monkeypatch)
    assert cli.main(["run", "app:app", "--port", port]) == 1
    assert "argv" not in captured
    assert "invalid port" in capsys.readouterr().err


def test_run_accepts_port_boundaries(monkeypatch):
    captured = _capture_launch(monkeypatch)
    assert cli.main(["run", "app:app", "--port", "1"]) == 0
    assert cli.main(["run", "app:app", "--port", "65535"]) == 0
    assert "argv" in captured


def test_run_rejects_invalid_env_values(monkeypatch, capsys):
    captured = _capture_launch(monkeypatch)
    monkeypatch.setenv("GRANIAN_HOST", "not-an-ip")
    assert cli.main(["run", "app:app"]) == 1
    assert "invalid host" in capsys.readouterr().err
    assert "argv" not in captured
    monkeypatch.delenv("GRANIAN_HOST")
    monkeypatch.setenv("GRANIAN_PORT", "99999")
    assert cli.main(["run", "app:app"]) == 1
    assert "invalid port" in capsys.readouterr().err


def test_run_rejects_missing_values(monkeypatch, capsys):
    captured = _capture_launch(monkeypatch)
    assert cli.main(["run", "app:app", "--host"]) == 1
    assert "missing value for --host" in capsys.readouterr().err
    assert cli.main(["run", "app:app", "--port"]) == 1
    assert "missing value for --port" in capsys.readouterr().err
    assert "argv" not in captured


def test_run_rejects_interface_override(monkeypatch, capsys):
    captured = _capture_launch(monkeypatch)
    assert cli.main(["run", "app:app", "--interface", "asgi"]) == 1
    assert "always uses RSGI" in capsys.readouterr().err
    assert cli.main(["run", "app:app", "--interface=asgi"]) == 1
    assert "argv" not in captured


def test_run_strips_leading_separator(monkeypatch):
    captured = _capture_launch(monkeypatch)
    assert cli.main(["run", "--", "app:app", "--port", "8000"]) == 0
    assert captured["argv"][-3:] == ["app:app", "--port", "8000"]


def test_run_help_and_version_bypass_validation(monkeypatch):
    captured = _capture_launch(monkeypatch)
    assert cli.main(["run", "--help", "--port", "bad"]) == 0
    assert captured["argv"] == ["--interface", "rsgi", "--help", "--port", "bad"]
    assert cli.main(["run", "--version"]) == 0
    assert captured["argv"] == ["--interface", "rsgi", "--version"]


def test_run_uds_suppresses_defaults(monkeypatch):
    captured = _capture_launch(monkeypatch)
    assert cli.main(["run", "app:app", "--uds", "/tmp/flameo.sock"]) == 0
    assert captured["argv"] == [
        "--interface",
        "rsgi",
        "app:app",
        "--uds",
        "/tmp/flameo.sock",
    ]


def test_run_uds_still_validates_explicit_host(monkeypatch, capsys):
    captured = _capture_launch(monkeypatch)
    assert cli.main(
        ["run", "app:app", "--uds", "/tmp/flameo.sock", "--host", "bad"]
    ) == 1
    assert "invalid host" in capsys.readouterr().err
    assert "argv" not in captured


def test_run_missing_granian_reports_reinstall(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_granian_available", lambda: False)
    assert cli.main(["run", "app:app"]) == 1
    error = capsys.readouterr().err
    assert "required Flameo dependency" in error
    assert "pip install --force-reinstall flameo" in error


def test_run_propagates_exit_code(monkeypatch):
    monkeypatch.setattr(cli, "_launch_granian", lambda forwarded: 7)
    assert cli.main(["run", "app:app"]) == 7


def test_top_level_help_lists_run(capsys):
    assert cli.main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "startproject" in output
    assert "startapp" in output
    assert "run" in output
