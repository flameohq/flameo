"""Scaffold content used by the Flameo CLI."""

from __future__ import annotations

from typing import List, Tuple

DB_CHOICES = ("sqlite", "postgresql", "mysql", "oracle", "mongodb")
CACHE_CHOICES = ("redis", "valkey", "dragonfly")

_DB_ENUM = {
    "sqlite": "SQLite",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "oracle": "Oracle",
    "mongodb": "MongoDB",
}
_DB_EXTRA = {
    "sqlite": "sqlite",
    "postgresql": "postgres",
    "mysql": "mysql",
    "oracle": "oracle",
    "mongodb": "mongo",
}
_DB_PORT = {
    "postgresql": "5432",
    "mysql": "3306",
    "oracle": "1521",
}
_CACHE_ENUM = {
    "redis": "Redis",
    "valkey": "Valkey",
    "dragonfly": "Dragonfly",
}


def _db_config_lines(database: str) -> List[str]:
    enum_name = _DB_ENUM[database]
    if database == "sqlite":
        return [
            "db_config = {",
            '    "db_type": DBType.SQLite,',
            '    "file_path": os.getenv("DB_PATH", "app.db"),',
            "}",
        ]
    if database == "mongodb":
        return [
            "db_config = {",
            '    "db_type": DBType.MongoDB,',
            '    "mongo_uri": os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017"),',
            '    "db": os.getenv("MONGO_DB", ""),',
            '    "min_size": 1,',
            '    "max_size": 5,',
            "}",
        ]
    return [
        "db_config = {",
        f'    "db_type": DBType.{enum_name},',
        '    "username": os.getenv("DB_USER", ""),',
        '    "password": os.getenv("DB_PASSWORD", ""),',
        '    "database": os.getenv("DB_NAME", ""),',
        '    "host": os.getenv("DB_HOST", "localhost"),',
        f'    "port": int(os.getenv("DB_PORT", "{_DB_PORT[database]}")),',
        '    "min_size": 2,',
        '    "max_size": 10,',
        "}",
    ]


def _cache_config_lines(cache: str) -> List[str]:
    enum_name = _CACHE_ENUM[cache]
    return [
        "cache_config = {",
        f'    "cache_type": CacheType.{enum_name},',
        '    "max_connections": int(os.getenv("CACHE_MAX_CONNECTIONS", "20")),',
        '    "cache_url": os.getenv("CACHE_URL", "redis://localhost:6379/0"),',
        "}",
    ]


def build_app_source(
    project_name: str,
    database: str = "none",
    cache: str = "none",
    static: bool = False,
    templates: bool = False,
) -> str:
    lines: List[str] = []
    if database != "none" or cache != "none":
        lines.extend(["import os", ""])

    flameo_names: List[str] = []
    if cache != "none":
        flameo_names.append("CacheType")
    if database != "none":
        flameo_names.append("DBType")
    flameo_names.append("Flameo")
    lines.append(f"from flameo import {', '.join(flameo_names)}")
    lines.extend(["", ""])

    if database != "none":
        lines.extend(_db_config_lines(database))
        lines.append("")
    if cache != "none":
        lines.extend(_cache_config_lines(cache))
        lines.append("")

    kwargs = []
    if database != "none":
        kwargs.append("db_config=db_config")
    if cache != "none":
        kwargs.append("cache_config=cache_config")
    lines.append(f"app = Flameo({', '.join(kwargs)})")

    if static:
        lines.append('app.use_static("/static", "static")')
    if templates:
        lines.append('app.use_template("templates")')

    lines.extend(["", ""])
    if templates:
        lines.extend([
            '@app.get("/")',
            "async def home(req, res):",
            f'    await res.render("index.html", {{"project": "{project_name}"}})',
        ])
    else:
        lines.extend([
            '@app.get("/")',
            "async def home(req, res):",
            f'    await res.json({{"message": "Hello from {project_name}"}})',
        ])

    lines.extend([
        "",
        "",
        '@app.get("/health")',
        "async def health(req, res):",
        '    await res.json({"status": "ok"})',
    ])

    if database != "none":
        lines.extend([
            "",
            "",
            "# Example: inject the managed database handle into a handler.",
            '# @app.get("/db-check")',
            "# async def db_check(req, res, db):",
            '#     await res.json({"ok": True})',
        ])
    if cache != "none":
        lines.extend([
            "",
            "",
            "# Example: inject the managed cache into a handler.",
            '# @app.get("/cache-check")',
            "# async def cache_check(req, res, cache):",
            '#     await res.json({"ok": True})',
        ])

    return "\n".join(lines).rstrip() + "\n"


def build_env_source(database: str = "none", cache: str = "none") -> str:
    sections: List[str] = []

    if database == "sqlite":
        sections.append("# SQLite\nDB_PATH=app.db")
    elif database in ("postgresql", "mysql", "oracle"):
        sections.append(
            "# Database\n"
            "DB_HOST=localhost\n"
            f"DB_PORT={_DB_PORT[database]}\n"
            "DB_USER=\n"
            "DB_PASSWORD=\n"
            "DB_NAME="
        )
    elif database == "mongodb":
        sections.append(
            "# MongoDB\n"
            "MONGO_URI=mongodb://127.0.0.1:27017\n"
            "MONGO_DB="
        )

    if cache != "none":
        sections.append(
            "# Cache\n"
            "CACHE_URL=redis://localhost:6379/0\n"
            "CACHE_MAX_CONNECTIONS=20"
        )

    if not sections:
        sections.append(
            "# No external services configured. Add environment variables here as needed."
        )

    return "\n\n".join(sections) + "\n"


def build_gitignore() -> str:
    return (
        ".env\n"
        ".env.*\n"
        "__pycache__/\n"
        "*.py[cod]\n"
        ".venv/\n"
        "venv/\n"
        ".pytest_cache/\n"
        "temp/\n"
        "logs/\n"
    )


def _extras(database: str, cache: str, templates: bool) -> List[str]:
    extras: List[str] = []
    if database != "none":
        extras.append(_DB_EXTRA[database])
    if cache != "none":
        extras.append("cache")
    if templates:
        extras.append("templates")
    return extras


def build_readme(
    project_name: str,
    database: str = "none",
    cache: str = "none",
    static: bool = False,
    templates: bool = False,
) -> str:
    extras = _extras(database, cache, templates)
    if extras:
        install = f'pip install "flameo[{",".join(extras)}]"'
    else:
        install = "pip install flameo"

    features: List[str] = []
    if database != "none":
        features.append(f"- Database: {_DB_ENUM[database]}")
    if cache != "none":
        features.append(f"- Cache: {_CACHE_ENUM[cache]}")
    if static:
        features.append("- Static files: `/static`")
    if templates:
        features.append("- Templates: `templates/`")

    feature_block = "\n".join(features) if features else "- No optional services"
    lines = [
        f"# {project_name}",
        "",
        "Generated by the Flameo CLI.",
        "",
        "## Install",
        "",
        "```bash",
        install,
        "```",
        "",
        "Granian is included with Flameo. Configuration lives in `.env`.",
        "",
        "## Run",
        "",
        "```bash",
        "flameo run app:app --reload",
        "```",
        "",
        "The default bind address is `127.0.0.1:9000`.",
        "",
        "## Add an app",
        "",
        "```bash",
        "flameo startapp users",
        "```",
        "",
        "`startapp` creates a router and mounts it in the file containing the",
        "Flameo instance when it can locate it unambiguously.",
        "",
        "## Features",
        "",
        feature_block,
        "",
    ]
    return "\n".join(lines)


def build_base_html(project_name: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"  <title>{{% block title %}}{project_name}{{% endblock %}}</title>\n"
        "</head>\n"
        "<body>\n"
        "  {% block content %}{% endblock %}\n"
        "</body>\n"
        "</html>\n"
    )


def build_index_html(project_name: str) -> str:
    return (
        '{% extends "base.html" %}\n'
        "\n"
        f"{{% block title %}}{project_name}{{% endblock %}}\n"
        "\n"
        "{% block content %}\n"
        f"<h1>Hello from {project_name}</h1>\n"
        "{% endblock %}\n"
    )


def build_router_source(name: str) -> str:
    return (
        "from flameo import Router\n"
        "\n"
        "router = Router()\n"
        "\n"
        "\n"
        '@router.get("/")\n'
        "async def index(req, res):\n"
        f'    await res.json({{"app": "{name}"}})\n'
        "\n"
        "\n"
        '@router.get("/{item_id:int}")\n'
        "async def get_item(req, res):\n"
        f'    await res.json({{"app": "{name}", "item_id": req.params["item_id"]}})\n'
    )
