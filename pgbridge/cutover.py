"""
Repointing a Django project at the migrated database.

The last step of a migration is the one nobody writes down: the application is
still talking to PostgreSQL. This rewrites `DATABASES` in a project's
settings.py to the SQL Server it was just migrated to, either inline or through
a .env file read with python-decouple.

No Tk here either, so the whole thing is testable and scriptable.
"""

from __future__ import annotations

import ast
import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile
import stat

# The keys written to .env, in the order they appear in the file.
ENV_KEYS = ("DB_ENGINE", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST",
            "DB_PORT", "DB_DRIVER", "DB_TRUSTED_CONNECTION", "DB_EXTRA_PARAMS")

MARKER = "pgbridge"


def atomic_write(path: str, text: str, mode: int = 0o600) -> None:
    """Private temporary file in the destination directory, then atomic replace."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".pgbridge-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.chmod(tmp, mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def validate_env_values(values: dict[str, str]) -> dict[str, str]:
    """Serialize for python-decouple's line-based RepositoryEnv parser."""
    result = {}
    for key, value in values.items():
        if any(c in value for c in "\r\n\x00"):
            raise ValueError(f"{key} contains a line break or NUL; use a deployment secret store.")
        result[key] = value if re.fullmatch(r"[A-Za-z0-9_./:@,;={} -]*", value) and value == value.strip() else '"' + value + '"'
    return result


# ---------------------------------------------------------------------------
# Locating the block to replace
# ---------------------------------------------------------------------------
def find_databases(source: str) -> tuple[int, int] | None:
    """The 1-based line span of the top-level ``DATABASES = {...}``.

    Parsed rather than pattern-matched: the assignment is routinely spread over
    twenty lines with nested dicts and comments, and a regex that gets it wrong
    corrupts somebody's settings file.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "DATABASES":
                end = getattr(node, "end_lineno", None) or node.lineno
                return node.lineno, end
    return None


def already_patched(source: str) -> bool:
    return f"# {MARKER}:" in source


def has_decouple_import(source: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "from decouple import" in source
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "decouple":
            return any(a.name == "config" for a in node.names)
    return False


def _import_line(source: str) -> int:
    """Where to put an added import: after the last top-level import."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    last = 0
    for index, node in enumerate(tree.body):
        if isinstance(node, (ast.Import, ast.ImportFrom)) or (index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            last = getattr(node, "end_lineno", None) or node.lineno
        else:
            break
    return last


# ---------------------------------------------------------------------------
# Rendering the new configuration
# ---------------------------------------------------------------------------
def _extra_params(ms) -> str:
    return (f"Encrypt={'yes' if ms.encrypt else 'no'};"
            f"TrustServerCertificate={'yes' if ms.trust_cert else 'no'}")


def env_values(ms) -> dict[str, str]:
    """What goes in .env. Windows auth carries no user or password."""
    windows = ms.auth == "windows"
    return {
        "DB_ENGINE": "mssql",
        "DB_NAME": ms.database,
        "DB_USER": "" if windows else ms.user,
        "DB_PASSWORD": "" if windows else ms.password,
        "DB_HOST": ms.server,
        "DB_PORT": "",
        "DB_DRIVER": ms.driver,
        "DB_TRUSTED_CONNECTION": "yes" if windows else "no",
        "DB_EXTRA_PARAMS": _extra_params(ms),
    }


def render_databases(ms, use_env: bool, env_path: str = "") -> str:
    """The replacement DATABASES block, as source."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    head = (f"# {MARKER}: written {stamp} — points at SQL Server "
            f"{ms.database} on {ms.server}.\n"
            f"# The previous configuration is commented out directly above.\n")
    windows = ms.auth == "windows"

    if use_env:
        body = [
            "DATABASES = {",
            '    "default": {',
            '        "ENGINE": config("DB_ENGINE", default="mssql"),',
            '        "NAME": config("DB_NAME"),',
            '        "USER": config("DB_USER", default=""),',
            '        "PASSWORD": config("DB_PASSWORD", default=""),',
            '        "HOST": config("DB_HOST"),',
            '        "PORT": config("DB_PORT", default=""),',
            '        "OPTIONS": {',
            '            "driver": config("DB_DRIVER",',
            '                             default="ODBC Driver 18 for SQL Server"),',
            '            "extra_params": config("DB_EXTRA_PARAMS", default=""),',
            '            "trusted_connection": config("DB_TRUSTED_CONNECTION",',
            '                                         default="no"),',
            "        },",
            "    }",
            "}",
        ]
    else:
        # repr() rather than quoting by hand: passwords contain backslashes and
        # quotes often enough that anything else eventually writes a broken file.
        user = "" if windows else ms.user
        password = "" if windows else ms.password
        trusted = "yes" if windows else "no"
        body = [
            "DATABASES = {",
            '    "default": {',
            '        "ENGINE": "mssql",',
            f'        "NAME": {ms.database!r},',
            f'        "USER": {user!r},',
            f'        "PASSWORD": {password!r},',
            f'        "HOST": {ms.server!r},',
            '        "PORT": "",',
            '        "OPTIONS": {',
            f'            "driver": {ms.driver!r},',
            f'            "extra_params": {_extra_params(ms)!r},',
            f'            "trusted_connection": {trusted!r},',
            "        },",
            "    }",
            "}",
        ]
    if use_env and env_path:
        head += ("from decouple import Config as _PgBridgeConfig, RepositoryEnv as _PgBridgeEnv\n"
                 f"_pgbridge_config = _PgBridgeConfig(_PgBridgeEnv({os.path.abspath(env_path)!r}))\n")
        body = [line.replace("config(", "_pgbridge_config(") for line in body]
    return head + "\n".join(body) + "\n"


# ---------------------------------------------------------------------------
# Patching
# ---------------------------------------------------------------------------
def patch_settings(path: str, ms, use_env: bool, backup: bool = True, env_path: str = "") -> dict:
    """Comment out the current DATABASES and write the new one below it.

    The old block is kept, commented, rather than deleted: rolling back is then
    a matter of reading the file, and the credentials that used to work stay
    visible when the new ones do not.
    """
    if not ms.database.strip():
        raise ValueError("A target database is required.")
    if use_env:
        validate_env_values(env_values(ms))
    with open(path, encoding="utf-8") as fh:
        source = fh.read()

    span = find_databases(source)
    if span is None:
        raise ValueError(
            "No top-level DATABASES assignment in this file. Point at the "
            "settings module that actually defines it — in a split settings "
            "package that is usually settings/base.py or settings/production.py.")

    lines = source.splitlines(keepends=True)
    start, end = span
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    backup_path = ""
    if backup:
        backup_path = f"{path}.pgbridge-{datetime.datetime.now():%Y%m%d-%H%M%S-%f}.bak"
        shutil.copy2(path, backup_path)

    old_block = "".join(lines[start - 1:end])
    commented = [f"# {MARKER}: replaced {stamp}. Previous configuration:\n"]
    for line in old_block.splitlines():
        commented.append(f"# {line}\n" if line.strip() else "#\n")

    new_block = render_databases(ms, use_env, env_path) + "\n"
    patched = lines[:start - 1] + commented + ["\n", new_block] + lines[end:]

    added_import = False
    if use_env and not env_path and not has_decouple_import(source):
        at = _import_line(source)
        # Placed with the other imports, not above the block that uses it, so
        # the file still reads like a settings module.
        patched.insert(at, "from decouple import config"
                           f"  # {MARKER}: for the database settings below\n")
        added_import = True

    text = "".join(patched)
    # Never leave a file that will not import.
    try:
        ast.parse(text)
    except SyntaxError as exc:
        if backup_path:
            shutil.copy2(backup_path, path)
        raise ValueError(f"The patched file would not parse ({exc}); "
                         "nothing was changed.") from exc

    atomic_write(path, text, stat.S_IMODE(os.stat(path).st_mode))

    return {"path": path, "backup": backup_path,
            "commented_lines": end - start + 1,
            "added_import": added_import,
            "old_block": old_block.rstrip("\n"),
            "new_block": new_block.rstrip("\n")}


def update_env_file(path: str, values: dict[str, str]) -> dict:
    """Create or update .env, keeping every key the project already had."""
    values = validate_env_values(values)
    existing: list[str] = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            existing = fh.read().splitlines()

    seen, out, updated = set(), [], []
    for line in existing:
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        key = match.group(1) if match else None
        if key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
            updated.append(key)
        else:
            out.append(line)

    added = [k for k in ENV_KEYS if k in values and k not in seen]
    if added:
        if out and out[-1].strip():
            out.append("")
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        out.append(f"# {MARKER}: database settings written {stamp}")
        out.extend(f"{k}={values[k]}" for k in added)

    atomic_write(path, "\n".join(out).rstrip("\n") + "\n")
    return {"path": path, "updated": updated, "added": added,
            "created": not existing}


# ---------------------------------------------------------------------------
# The project's environment
# ---------------------------------------------------------------------------
def venv_python(folder: str) -> str | None:
    """The interpreter inside a virtualenv, on either platform."""
    if not folder:
        return None
    for relative in ("bin/python", "bin/python3",
                     "Scripts/python.exe", "Scripts/python3.exe"):
        candidate = os.path.join(folder, *relative.split("/"))
        if os.path.isfile(candidate):
            return candidate
    # The folder may already be the bin/Scripts directory, or the project root.
    for relative in ("python", "python3", "python.exe",
                     "venv/bin/python", "env/bin/python", ".venv/bin/python",
                     "venv/Scripts/python.exe", "env/Scripts/python.exe",
                     ".venv/Scripts/python.exe"):
        candidate = os.path.join(folder, *relative.split("/"))
        if os.path.isfile(candidate):
            return candidate
    return None


def check_packages(python: str, names: tuple[str, ...]) -> dict[str, bool]:
    """Which of these import inside that interpreter."""
    found = {}
    for name in names:
        module = {"python-decouple": "decouple",
                  "mssql-django": "mssql"}.get(name, name.replace("-", "_"))
        try:
            done = subprocess.run([python, "-c", f"import {module}"],
                                  capture_output=True, timeout=60)
            found[name] = done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            found[name] = False
    return found


def install_packages(python: str, names: list[str]) -> tuple[bool, str]:
    try:
        done = subprocess.run([python, "-m", "pip", "install", *names],
                              capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = (done.stdout or "") + (done.stderr or "")
    return done.returncode == 0, output.strip()[-4000:]


def describe_python(python: str) -> str:
    try:
        done = subprocess.run([python, "-c",
                               "import sys; print(sys.version.split()[0])"],
                              capture_output=True, text=True, timeout=60)
        return done.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def find_manage_py(settings_path: str) -> str | None:
    """Locate manage.py by walking up from settings.py."""
    if not settings_path:
        return None
    curr = os.path.dirname(os.path.abspath(settings_path))
    for _ in range(4):
        candidate = os.path.join(curr, "manage.py")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(curr)
        if parent == curr:
            break
        curr = parent
    return None


def guess_project_dir(settings_path: str) -> str:
    manage = find_manage_py(settings_path)
    if manage:
        return os.path.dirname(manage)
    folder = os.path.dirname(os.path.abspath(settings_path))
    parent = os.path.dirname(folder)
    return parent or folder


def guess_env_path(settings_path: str) -> str:
    """Where a project's .env normally lives: beside manage.py.

    settings.py is usually <root>/<project>/settings.py, so the root is two
    levels up; fall back to one level up when it is not.
    """
    folder = os.path.dirname(os.path.abspath(settings_path))
    parent = os.path.dirname(folder)
    for candidate in (parent, folder):
        if os.path.exists(os.path.join(candidate, "manage.py")):
            return os.path.join(candidate, ".env")
    return os.path.join(parent or folder, ".env")


def test_app_connection(
    settings_path: str,
    venv_folder: str = "",
    env_path: str = "",
    timeout: int = 25,
    expected_database: str = "",
) -> tuple[bool, str, dict]:
    """Test whether the Django application connects to the database.

    Runs a subprocess in the project's virtual environment (or fallback)
    that initializes Django with the specified settings and tests connection
    via connection.ensure_connection() and a probe query.
    """
    if not settings_path or not os.path.isfile(settings_path):
        return False, f"Settings file not found: {settings_path}", {}

    python = venv_python(venv_folder) if venv_folder else None
    if not python:
        project_dir = guess_project_dir(settings_path)
        python = (venv_python(project_dir) or
                  venv_python(os.path.dirname(os.path.abspath(settings_path))))
    if not python:
        python = sys.executable

    project_dir = guess_project_dir(settings_path)
    if not env_path:
        env_path = guess_env_path(settings_path)

    runner = (
        "import os, sys\n"
        "proj = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "settings_file = sys.argv[2] if len(sys.argv) > 2 else ''\n"
        "custom_env = sys.argv[3] if len(sys.argv) > 3 else ''\n"
        "if custom_env and os.path.isfile(custom_env):\n"
        "    os.environ['DECOUPLE_ENV_FILE'] = custom_env\n"
        "if proj and proj not in sys.path:\n"
        "    sys.path.insert(0, proj)\n"
        "settings_dir = os.path.dirname(settings_file) if settings_file else ''\n"
        "if settings_dir and settings_dir not in sys.path:\n"
        "    sys.path.insert(0, settings_dir)\n"
        "mod = ''\n"
        "if proj and settings_file:\n"
        "    try:\n"
        "        rel = os.path.relpath(settings_file, proj)\n"
        "        if not rel.startswith('..'):\n"
        "            mod = os.path.splitext(rel)[0].replace(os.sep, '.').replace('/', '.')\n"
        "    except Exception:\n"
        "        pass\n"
        "if not mod and settings_file:\n"
        "    mod = os.path.splitext(os.path.basename(settings_file))[0]\n"
        "if mod:\n"
        "    os.environ['DJANGO_SETTINGS_MODULE'] = mod\n"
        "try:\n"
        "    import django\n"
        "    django.setup()\n"
        "except Exception as e:\n"
        "    sys.stderr.write(f'DJANGO_SETUP_FAILED: {e}\\n')\n"
        "    sys.exit(101)\n"
        "try:\n"
        "    from django.db import connection\n"
        "    connection.ensure_connection()\n"
        "    with connection.cursor() as cursor:\n"
        "        cursor.execute('SELECT DB_NAME(), @@VERSION')\n"
        "        row = cursor.fetchone()\n"
        "        db = row[0] if row else 'unknown'\n"
        "        ver = row[1].splitlines()[0].strip() if (row and row[1]) else ''\n"
        "        vendor = getattr(connection, 'vendor', 'unknown')\n"
        "        print(f'PGBRIDGE_OK|{db}|{vendor}|{ver}')\n"
        "except Exception as e:\n"
        "    sys.stderr.write(f'DB_CONNECTION_FAILED: {e}\\n')\n"
        "    sys.exit(102)\n"
    )

    env = os.environ.copy()
    if env_path and os.path.isfile(env_path):
        env["DECOUPLE_ENV_FILE"] = env_path

    try:
        proc = subprocess.run(
            [python, "-c", runner, project_dir, settings_path, env_path or ""],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"Connection test timed out after {timeout} seconds.", {}
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Failed to execute Python test runner: {exc}", {}

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    for line in stdout.splitlines():
        if proc.returncode == 0 and line.startswith("PGBRIDGE_OK|"):
            parts = line.split("|", 3)
            db_name = parts[1] if len(parts) > 1 else "unknown"
            vendor = parts[2] if len(parts) > 2 else "unknown"
            version = parts[3] if len(parts) > 3 else ""

            if vendor == "postgresql":
                return False, (
                    f"Connected successfully, but the app is still pointing at PostgreSQL "
                    f"(Database: '{db_name}'). Click 'Patch settings.py' to repoint to SQL Server."
                ), {"database": db_name, "vendor": vendor, "version": version}

            if vendor not in ("microsoft", "mssql"):
                return False, f"Unexpected database vendor: {vendor}", {"database": db_name, "vendor": vendor}
            if expected_database and db_name != expected_database:
                return False, f"App connected to {db_name!r}, expected {expected_database!r}.", {"database": db_name, "vendor": vendor}
            return True, (
                f"App successfully connected to SQL Server database '{db_name}' via Django!"
            ), {"database": db_name, "vendor": vendor, "version": version}

    err_msg = stderr.strip()
    if "DJANGO_SETUP_FAILED:" in err_msg:
        reason = err_msg.split("DJANGO_SETUP_FAILED:", 1)[1].strip()
        return False, f"Django initialization failed: {reason}", {}
    if "DB_CONNECTION_FAILED:" in err_msg:
        reason = err_msg.split("DB_CONNECTION_FAILED:", 1)[1].strip()
        return False, f"Database connection failed: {reason}", {}

    clean_err = (stderr or stdout).strip()[-500:] or f"Process exited with code {proc.returncode}"
    return False, f"Connection check failed: {clean_err}", {}


def demo() -> None:
    """Self-check: patch a throwaway settings module both ways."""
    import tempfile
    from dataclasses import dataclass

    @dataclass
    class _Ms:
        server: str = "sql01"
        database: str = "shop_app"
        driver: str = "ODBC Driver 18 for SQL Server"
        auth: str = "sql"
        user: str = "svc"
        password: str = "pw"
        encrypt: bool = True
        trust_cert: bool = True

    original = (
        "import os\n"
        "SECRET_KEY = 'x'\n"
        "DATABASES = {\n"
        "    'default': {\n"
        "        'ENGINE': 'django.db.backends.postgresql',\n"
        "        'NAME': 'shop_app',\n"
        "    }\n"
        "}\n"
        "DEBUG = True\n")
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "settings.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(original)

    assert find_databases(original) == (3, 8)
    result = patch_settings(path, _Ms(), use_env=True)
    patched = open(path, encoding="utf-8").read()
    assert "# DATABASES = {" in patched, "the old block must be kept, commented"
    active = [ln for ln in patched.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("postgresql" in ln for ln in active), \
        "the old engine must no longer be live"
    assert any("DATABASES = {" in ln for ln in active), "a live block must exist"
    assert "from decouple import config" in patched
    assert 'config("DB_NAME")' in patched
    assert "DEBUG = True" in patched, "the rest of the file must survive"
    ast.parse(patched)
    assert os.path.exists(result["backup"])

    env = os.path.join(folder, ".env")
    with open(env, "w", encoding="utf-8") as fh:
        fh.write("SECRET_KEY=keep-me\nDB_NAME=old_name\n")
    report = update_env_file(env, env_values(_Ms()))
    body = open(env, encoding="utf-8").read()
    assert "SECRET_KEY=keep-me" in body, "other keys must survive"
    assert "DB_NAME=shop_app" in body and "old_name" not in body
    assert "DB_HOST=sql01" in body
    assert report["updated"] == ["DB_NAME"]

    path2 = os.path.join(folder, "hard.py")
    with open(path2, "w", encoding="utf-8") as fh:
        fh.write(original)
    patch_settings(path2, _Ms(auth="windows"), use_env=False)
    hard = open(path2, encoding="utf-8").read()
    assert '"trusted_connection": \'yes\'' in hard, hard
    assert '"USER": \'\'' in hard, "windows auth carries no user"
    assert "config(" not in hard
    ast.parse(hard)

    # A password with quotes and backslashes must survive the round trip.
    path3 = os.path.join(folder, "quoted.py")
    with open(path3, "w", encoding="utf-8") as fh:
        fh.write(original)
    nasty = "p'a\\s\"w"
    patch_settings(path3, _Ms(password=nasty), use_env=False)
    scope: dict = {}
    exec(compile(open(path3, encoding="utf-8").read(), path3, "exec"), scope)
    assert scope["DATABASES"]["default"]["PASSWORD"] == nasty
    assert scope["DATABASES"]["default"]["ENGINE"] == "mssql"
    print("cutover self-check ok")


if __name__ == "__main__":
    demo()
