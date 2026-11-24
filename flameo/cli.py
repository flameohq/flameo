"""Command-line interface for Flameo."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import ipaddress
import keyword
import os
import re
import subprocess
import sys
import tempfile
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from . import scaffold

_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "build",
    "dist",
    "site-packages",
    "temp",
}
_PREFERRED_APP_FILES = ("app.py", "main.py", "server.py", "asgi.py", "wsgi.py")
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_DB_LABELS = {
    "sqlite": "SQLite",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "oracle": "Oracle",
    "mongodb": "MongoDB",
}
_CACHE_LABELS = {
    "redis": "Redis",
    "valkey": "Valkey",
    "dragonfly": "Dragonfly",
}


class CLIError(Exception):
    """A user-facing CLI error."""


@dataclass(frozen=True)
class AppCandidate:
    path: Path
    var: str
    line: int


def _is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, OSError):
        return False


def _prompt_yes_no(question: str, default: bool = False) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        answer = input(question + suffix).strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def _prompt_choice(label: str, options: Sequence[str], labels: dict, default: str) -> str:
    print(label)
    for index, option in enumerate(options, 1):
        print(f"  {index}) {labels[option]}")
    while True:
        answer = input(f"Select [{default}]: ").strip().lower()
        if not answer:
            return default
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(options):
                return options[index - 1]
            continue
        for option in options:
            if answer == option or answer == labels[option].lower():
                return option


def _validate_project_name(name: str) -> None:
    if (
        not name
        or name in (".", "..")
        or os.path.isabs(name)
        or "/" in name
        or "\\" in name
        or not _PROJECT_NAME_RE.fullmatch(name)
    ):
        raise CLIError(
            f"[flameo] invalid project name {name!r}. Use letters, digits, "
            "underscores, or hyphens."
        )


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flameo",
        description="Flameo project scaffolding and Granian RSGI runner.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    project = sub.add_parser(
        "startproject",
        help="Create a new Flameo project.",
    )
    project.add_argument("name", help="Project directory name.")
    project.add_argument(
        "--directory",
        default=".",
        help="Parent directory where the project is created.",
    )
    project.add_argument(
        "--force",
        action="store_true",
        help="Allow writing into a non-empty project directory.",
    )
    project.add_argument(
        "--no-input",
        action="store_true",
        help="Do not run the interactive wizard.",
    )
    project.add_argument(
        "--database",
        choices=("none",) + scaffold.DB_CHOICES,
        default="none",
        help="Database backend to configure.",
    )
    project.add_argument(
        "--cache",
        choices=("none",) + scaffold.CACHE_CHOICES,
        default="none",
        help="Cache backend to configure.",
    )
    project.add_argument(
        "--static",
        action="store_true",
        help="Generate and configure static file serving.",
    )
    project.add_argument(
        "--templates",
        action="store_true",
        help="Generate and configure Jinja templates.",
    )

    app = sub.add_parser(
        "startapp",
        help="Create a router and mount it in the Flameo app.",
    )
    app.add_argument("name", help="Router/app name. Must be a Python identifier.")
    app.add_argument(
        "--directory",
        default=".",
        help="Project root directory.",
    )
    app.add_argument(
        "--routers-dir",
        default="routers",
        help="Router package directory relative to the project root.",
    )
    app.add_argument(
        "--app-file",
        default=None,
        help="Explicit Python file containing the Flameo instance.",
    )
    app.add_argument(
        "--app-var",
        default=None,
        help="Explicit Flameo instance variable name.",
    )
    app.add_argument(
        "--prefix",
        default=None,
        help="Router mount prefix. Defaults to /<name>.",
    )
    app.add_argument(
        "--no-wire",
        action="store_true",
        help="Create the router without modifying app source.",
    )
    app.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing router file.",
    )

    run = sub.add_parser(
        "run",
        help="Run the app with Granian using RSGI.",
        description=(
            "Alias for `granian --interface rsgi`. All remaining arguments are "
            "forwarded to Granian."
        ),
    )
    run.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def _startproject(args: argparse.Namespace) -> int:
    _validate_project_name(args.name)

    base = Path(args.directory).expanduser().resolve()
    target = base / args.name
    if target.exists():
        if not target.is_dir():
            raise CLIError(f"[flameo] {target} exists and is not a directory")
        if any(target.iterdir()) and not args.force:
            raise CLIError(
                f"[flameo] {target} already exists and is not empty. "
                "Use --force to write generated files anyway."
            )

    database = args.database
    cache = args.cache
    static = bool(args.static)
    templates = bool(args.templates)

    if not args.no_input:
        if not _is_tty():
            raise CLIError(
                "[flameo] non-interactive input detected; use --no-input with "
                "--database/--cache/--static/--templates."
            )
        database = (
            _prompt_choice(
                "Select database:",
                scaffold.DB_CHOICES,
                _DB_LABELS,
                "sqlite",
            )
            if _prompt_yes_no("Do you need a database?")
            else "none"
        )
        cache = (
            _prompt_choice(
                "Select cache:",
                scaffold.CACHE_CHOICES,
                _CACHE_LABELS,
                "redis",
            )
            if _prompt_yes_no("Do you need a cache?")
            else "none"
        )
        static = _prompt_yes_no("Serve static files?")
        templates = _prompt_yes_no("Use templates?")

    target.mkdir(parents=True, exist_ok=True)
    _write_text(
        target / "app.py",
        scaffold.build_app_source(
            args.name,
            database=database,
            cache=cache,
            static=static,
            templates=templates,
        ),
    )
    _write_text(
        target / "routers" / "__init__.py",
        '"""Application routers."""\n',
    )
    _write_text(
        target / "README.md",
        scaffold.build_readme(
            args.name,
            database=database,
            cache=cache,
            static=static,
            templates=templates,
        ),
    )
    _write_text(target / ".gitignore", scaffold.build_gitignore())
    _write_text(target / ".env", scaffold.build_env_source(database, cache))

    if static:
        _write_bytes(target / "static" / ".gitkeep", b"")
    if templates:
        _write_text(
            target / "templates" / "base.html",
            scaffold.build_base_html(args.name),
        )
        _write_text(
            target / "templates" / "index.html",
            scaffold.build_index_html(args.name),
        )

    print(f"Created Flameo project at {target}")
    print("  database: none" if database == "none" else f"  database: {database}")
    print("  cache:    none" if cache == "none" else f"  cache:    {cache}")
    print(f"  static:   {'yes' if static else 'no'}")
    print(f"  templates:{' yes' if templates else ' no'}")
    print("")
    print(f"Configuration was written to {target / '.env'}.")
    print("Run it with: flameo run app:app --reload")
    return 0


def _read_source(path: Path) -> Tuple[str, str]:
    with tokenize.open(path) as handle:
        return handle.read(), handle.encoding


def _is_flameo_call(node: ast.AST, flameo_names: set, module_names: set) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in flameo_names
    if isinstance(func, ast.Attribute) and func.attr == "Flameo":
        return isinstance(func.value, ast.Name) and func.value.id in module_names
    return False


def _find_candidates_in_source(source: str, path: Path) -> List[AppCandidate]:
    tree = ast.parse(source)
    flameo_names: set = set()
    module_names: set = set()
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.module == "flameo":
            for alias in statement.names:
                if alias.name == "Flameo":
                    flameo_names.add(alias.asname or alias.name)
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.name == "flameo":
                    module_names.add(alias.asname or "flameo")

    candidates: List[AppCandidate] = []
    for statement in tree.body:
        targets: List[ast.expr] = []
        value = None
        if isinstance(statement, ast.Assign):
            targets = list(statement.targets)
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        if value is None or not _is_flameo_call(value, flameo_names, module_names):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                candidates.append(AppCandidate(path, target.id, statement.lineno))
    return candidates


def _iter_python_files(root: Path) -> Iterable[Path]:
    paths: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in _EXCLUDED_DIRS and not name.startswith(".")
        ]
        for filename in filenames:
            if filename.endswith(".py"):
                paths.append(Path(dirpath) / filename)

    def score(path: Path):
        if path.name in _PREFERRED_APP_FILES:
            return (0, _PREFERRED_APP_FILES.index(path.name), len(path.parts), str(path))
        return (1, 0, len(path.parts), str(path))

    return sorted(paths, key=score)


def _discover_candidates(
    root: Path,
    app_file: Optional[str],
    app_var: Optional[str],
) -> List[AppCandidate]:
    candidates: List[AppCandidate] = []
    if app_file:
        path = Path(app_file)
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        if not path.exists():
            raise CLIError(f"[flameo] app file not found: {path}")
        try:
            source, _ = _read_source(path)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            raise CLIError(f"[flameo] cannot read {path}: {exc}") from exc
        try:
            candidates = _find_candidates_in_source(source, path)
        except SyntaxError as exc:
            raise CLIError(f"[flameo] cannot parse {path}: {exc}") from exc
    else:
        for path in _iter_python_files(root):
            try:
                source, _ = _read_source(path)
                candidates.extend(_find_candidates_in_source(source, path))
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue

    if app_var:
        candidates = [candidate for candidate in candidates if candidate.var == app_var]
    return candidates


def _module_path(root: Path, routers_dir: Path, name: str) -> Optional[str]:
    try:
        relative = routers_dir.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return ".".join(tuple(relative.parts) + (name,))


def _router_import_line(
    app_path: Path,
    root: Path,
    routers_dir: Path,
    name: str,
) -> Optional[str]:
    app_parent = app_path.parent.resolve()
    routers_resolved = routers_dir.resolve()

    if app_parent == root.resolve():
        module = _module_path(root, routers_dir, name)
        return f"from {module} import router" if module else None

    if (app_parent / "__init__.py").exists():
        try:
            relative = routers_resolved.relative_to(app_parent)
        except ValueError:
            return None
        if relative.parts:
            return f"from .{'.'.join(tuple(relative.parts) + (name,))} import router"
    return None


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    compare = node.test
    if len(compare.ops) != 1 or not isinstance(compare.ops[0], ast.Eq):
        return False
    left, right = compare.left, compare.comparators[0]
    if isinstance(left, ast.Name) and left.id == "__name__":
        return isinstance(right, ast.Constant) and right.value == "__main__"
    if isinstance(right, ast.Name) and right.id == "__name__":
        return isinstance(left, ast.Constant) and left.value == "__main__"
    return False


def _wire_app(path: Path, import_line: str, mount_line: str) -> bool:
    source, encoding = _read_source(path)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise CLIError(f"[flameo] cannot parse {path}: {exc}") from exc

    existing = {line.strip() for line in source.splitlines()}
    need_import = import_line not in existing
    need_mount = mount_line not in existing
    if not need_import and not need_mount:
        return False

    newline = "\r\n" if "\r\n" in source else "\n"
    lines = source.splitlines(keepends=True)

    import_index = 0
    if tree.body and isinstance(tree.body[0], ast.Expr):
        value = getattr(tree.body[0], "value", None)
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            import_index = tree.body[0].end_lineno or 0
    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            import_index = max(import_index, statement.end_lineno or statement.lineno)

    mount_index = len(lines)
    for statement in tree.body:
        if _is_main_guard(statement):
            mount_index = max(0, statement.lineno - 1)
            break

    def line_with_newline(text: str) -> str:
        return text if text.endswith(("\n", "\r")) else text + newline

    def insert_at(index: int, text: str) -> None:
        nonlocal lines
        if index > 0 and not lines[index - 1].endswith(("\n", "\r")):
            lines[index - 1] = lines[index - 1] + newline
        if index >= len(lines) and lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] = lines[-1] + newline
        lines.insert(index, line_with_newline(text))

    if need_import and need_mount:
        if import_index <= mount_index:
            insert_at(mount_index, mount_line)
            insert_at(import_index, import_line)
        else:
            insert_at(import_index, import_line)
            insert_at(mount_index, mount_line)
    elif need_import:
        insert_at(import_index, import_line)
    else:
        insert_at(mount_index, mount_line)

    new_source = "".join(lines)
    try:
        ast.parse(new_source)
    except SyntaxError as exc:
        raise CLIError(
            f"[flameo] refusing to modify {path}: generated source is invalid ({exc})"
        ) from exc

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(new_source)
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise
    return True


def _manual_wire_text(module: Optional[str], name: str, prefix: str) -> str:
    import_line = (
        f"from {module} import router"
        if module
        else f"from routers.{name} import router"
    )
    return (
        "Mount the router manually:\n\n"
        f"{import_line}\n"
        f'app.use_router("{prefix}", router)\n'
    )


def _prompt_candidate(candidates: Sequence[AppCandidate]) -> AppCandidate:
    print("Multiple Flameo instances found:")
    for index, candidate in enumerate(candidates, 1):
        print(f"  {index}) {candidate.path}:{candidate.line} -> {candidate.var}")
    while True:
        answer = input(f"Select [1-{len(candidates)}]: ").strip()
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(candidates):
                return candidates[index - 1]


def _startapp(args: argparse.Namespace) -> int:
    name = args.name
    if not name.isidentifier() or keyword.iskeyword(name):
        raise CLIError(
            f"[flameo] invalid app name {name!r}. Use a valid Python identifier."
        )

    root = Path(args.directory).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise CLIError(f"[flameo] project directory not found: {root}")

    routers_dir = Path(args.routers_dir)
    if not routers_dir.is_absolute():
        routers_dir = root / routers_dir
    routers_dir = routers_dir.resolve()

    router_path = routers_dir / f"{name}.py"
    if router_path.exists() and not args.force:
        raise CLIError(
            f"[flameo] {router_path} already exists. Use --force to overwrite it."
        )

    _write_text(router_path, scaffold.build_router_source(name))
    init_path = routers_dir / "__init__.py"
    if not init_path.exists():
        _write_text(init_path, '"""Application routers."""\n')

    prefix = args.prefix or f"/{name}"
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    module = _module_path(root, routers_dir, name)
    print(f"Created {router_path}")

    if args.no_wire:
        print(_manual_wire_text(module, name, prefix))
        return 0

    candidates = _discover_candidates(root, args.app_file, args.app_var)
    if not candidates:
        print(
            "No Flameo instance was found/matched, so app.py was not modified.",
            file=sys.stderr,
        )
        print(_manual_wire_text(module, name, prefix))
        return 0

    if len(candidates) > 1:
        if not _is_tty():
            print("Multiple Flameo instances found:", file=sys.stderr)
            for candidate in candidates:
                print(
                    f"  {candidate.path}:{candidate.line} -> {candidate.var}",
                    file=sys.stderr,
                )
            print(
                "Re-run with --app-file and/or --app-var to choose one.",
                file=sys.stderr,
            )
            print(_manual_wire_text(module, name, prefix))
            return 0
        candidate = _prompt_candidate(candidates)
    else:
        candidate = candidates[0]

    import_line = _router_import_line(candidate.path, root, routers_dir, name)
    if import_line is None:
        print(
            f"[flameo] could not determine a safe import path for {candidate.path}.",
            file=sys.stderr,
        )
        print(_manual_wire_text(module, name, prefix))
        return 0

    mount_line = f'{candidate.var}.use_router("{prefix}", router)'
    changed = _wire_app(candidate.path, import_line, mount_line)
    if changed:
        print(f"Wired {candidate.path} using variable {candidate.var!r}:")
        print(f"  {import_line}")
        print(f"  {mount_line}")
    else:
        print(f"{candidate.path} already contains this router wiring.")
    return 0


def _collect_option_values(args: Sequence[str], option: str) -> Tuple[List[str], bool]:
    values: List[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == option:
            if index + 1 >= len(args) or args[index + 1].startswith("--"):
                return values, True
            values.append(args[index + 1])
            index += 2
            continue
        if token.startswith(option + "="):
            values.append(token[len(option) + 1 :])
        index += 1
    return values, False


def _validate_host(value: str) -> None:
    if value.startswith("[") and value.endswith("]"):
        raise CLIError(
            f"[flameo] invalid host {value!r}: expected an IPv4 or IPv6 address "
            f"(use {value[1:-1]!r} without brackets)"
        )
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise CLIError(
            f"[flameo] invalid host {value!r}: expected an IPv4 or IPv6 address"
        ) from exc


def _validate_port(value: str) -> None:
    if not value or not value.isascii() or not value.isdigit():
        raise CLIError(
            f"[flameo] invalid port {value!r}: expected an integer between 1 and 65535"
        )
    port = int(value)
    if not 1 <= port <= 65535:
        raise CLIError(
            f"[flameo] invalid port {value!r}: expected an integer between 1 and 65535"
        )


def _granian_available() -> bool:
    return importlib.util.find_spec("granian") is not None


def _launch_granian(forwarded: Sequence[str]) -> int:
    if not _granian_available():
        raise CLIError(
            "[flameo] Granian is a required Flameo dependency but could not be imported.\n"
            "Reinstall Flameo:\n"
            "pip install --force-reinstall flameo"
        )
    argv = [sys.executable, "-m", "granian", *forwarded]
    if os.name != "nt":
        os.execv(sys.executable, argv)
        return 0
    try:
        return subprocess.run(argv, check=False).returncode
    except FileNotFoundError as exc:
        raise CLIError("[flameo] could not start Granian") from exc


def _run_command(forwarded: Sequence[str]) -> int:
    args = list(forwarded)
    if args and args[0] == "--":
        args = args[1:]

    for token in args:
        if token == "--interface" or token.startswith("--interface="):
            raise CLIError(
                "[flameo] `flameo run` always uses RSGI. Run "
                "`granian --interface asgi ...` directly for ASGI."
            )

    if any(token in ("--help", "-h", "--version") for token in args):
        return _launch_granian(["--interface", "rsgi", *args])

    host_values, host_missing = _collect_option_values(args, "--host")
    if host_missing:
        raise CLIError("[flameo] missing value for --host")
    port_values, port_missing = _collect_option_values(args, "--port")
    if port_missing:
        raise CLIError("[flameo] missing value for --port")

    for value in host_values:
        _validate_host(value)
    for value in port_values:
        _validate_port(value)

    has_uds = any(token == "--uds" or token.startswith("--uds=") for token in args)

    default_args: List[str] = []
    if not has_uds:
        if not host_values:
            if "GRANIAN_HOST" in os.environ:
                _validate_host(os.environ["GRANIAN_HOST"])
            else:
                default_args.extend(["--host", "127.0.0.1"])
        if not port_values:
            if "GRANIAN_PORT" in os.environ:
                _validate_port(os.environ["GRANIAN_PORT"])
            else:
                default_args.extend(["--port", "9000"])

    return _launch_granian(["--interface", "rsgi", *default_args, *args])


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    if raw and raw[0] == "run":
        try:
            return _run_command(raw[1:])
        except CLIError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130

    parser = build_parser()
    try:
        namespace = parser.parse_args(raw)
    except SystemExit as exc:
        return int(exc.code or 0)

    try:
        if namespace.command == "startproject":
            return _startproject(namespace)
        if namespace.command == "startapp":
            return _startapp(namespace)
        if namespace.command == "run":
            return _run_command(namespace.args)
    except CLIError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
