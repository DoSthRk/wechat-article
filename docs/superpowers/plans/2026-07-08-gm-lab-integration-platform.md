# GM-LAB Integration Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build GM-LAB as a lightweight internal portal that provides login, app launcher, simple user/app permissions, and reverse-proxy authorization for the three existing internal applications.

**Architecture:** Create a new independent Flask + SQLite service in `D:\dev-project\gm-lab`. Existing applications remain separate repositories and systemd services; GM-LAB stores users, apps, permissions, and exposes an auth-check endpoint for Nginx `auth_request`.

**Tech Stack:** Python 3.11+, Flask, SQLite via standard `sqlite3`, Werkzeug password hashing, pytest, gunicorn, Nginx, systemd.

---

## Scope Check

This plan implements the first GM-LAB milestone only:

- New `gm-lab` repository and application.
- Local login/logout/session handling.
- App launcher filtered by permissions.
- Admin pages for users, apps, and permissions.
- Read-only app status page.
- Auth-check endpoint for Nginx.
- Deployment files for `gm-lab.service` and Nginx subdomain proxy.

It does not merge or rewrite `wechat-article`, `target-running`, or `pdf-generate`.

## File Structure

Create these files under `D:\dev-project\gm-lab`:

```text
D:\dev-project\gm-lab\
  app.py
  README.md
  requirements.txt
  pytest.ini
  .gitignore
  gmlab\
    __init__.py
    auth.py
    config.py
    db.py
    routes.py
    status.py
    static\
      styles.css
    templates\
      admin_apps.html
      admin_permissions.html
      admin_users.html
      base.html
      forbidden.html
      launcher.html
      login.html
  scripts\
    create_admin.py
  tests\
    conftest.py
    test_admin.py
    test_app_factory.py
    test_auth.py
    test_auth_check.py
    test_status.py
  deploy\
    gm-lab.service
    nginx\
      gm-lab.conf
```

Responsibilities:

- `app.py`: WSGI entrypoint.
- `gmlab/__init__.py`: Flask app factory and CLI registration.
- `gmlab/config.py`: Environment-backed configuration.
- `gmlab/db.py`: SQLite schema, seed data, user/app/permission queries.
- `gmlab/auth.py`: Session helpers, password verification, decorators.
- `gmlab/routes.py`: HTTP routes and form handlers.
- `gmlab/status.py`: Read-only systemd status adapter.
- `scripts/create_admin.py`: Local/server helper to create or reset an admin user.
- `deploy/gm-lab.service`: systemd unit template.
- `deploy/nginx/gm-lab.conf`: Nginx config for `lab.genemedi.com` and app subdomains.

## Task 1: Bootstrap Repository And Health Route

**Files:**
- Create: `D:\dev-project\gm-lab\.gitignore`
- Create: `D:\dev-project\gm-lab\requirements.txt`
- Create: `D:\dev-project\gm-lab\pytest.ini`
- Create: `D:\dev-project\gm-lab\app.py`
- Create: `D:\dev-project\gm-lab\gmlab\__init__.py`
- Create: `D:\dev-project\gm-lab\gmlab\config.py`
- Create: `D:\dev-project\gm-lab\tests\conftest.py`
- Create: `D:\dev-project\gm-lab\tests\test_app_factory.py`

- [ ] **Step 1: Create the repository shell**

Run:

```powershell
New-Item -ItemType Directory -Force -Path 'D:\dev-project\gm-lab' | Out-Null
Set-Location 'D:\dev-project\gm-lab'
git init
New-Item -ItemType Directory -Force -Path 'gmlab','tests','deploy\nginx','scripts','gmlab\templates','gmlab\static' | Out-Null
```

Expected:

```text
Initialized empty Git repository in D:/dev-project/gm-lab/.git/
```

- [ ] **Step 2: Add base project files**

Write `D:\dev-project\gm-lab\.gitignore`:

```gitignore
.venv/
__pycache__/
*.pyc
.pytest_cache/
.env
*.sqlite3
instance/
```

Write `D:\dev-project\gm-lab\requirements.txt`:

```text
Flask==3.0.3
gunicorn==22.0.0
pytest==8.2.2
python-dotenv==1.0.1
Werkzeug==3.0.3
```

Write `D:\dev-project\gm-lab\pytest.ini`:

```ini
[pytest]
testpaths = tests
```

- [ ] **Step 3: Write the failing app factory test**

Write `D:\dev-project\gm-lab\tests\conftest.py`:

```python
from pathlib import Path

import pytest

from gmlab import create_app


@pytest.fixture
def app(tmp_path: Path):
    db_path = tmp_path / "gm_lab_test.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_PATH": str(db_path),
            "SECRET_KEY": "test-secret",
            "SESSION_COOKIE_DOMAIN": None,
        }
    )
    return app


@pytest.fixture
def client(app):
    return app.test_client()
```

Write `D:\dev-project\gm-lab\tests\test_app_factory.py`:

```python
def test_health_endpoint(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
```

- [ ] **Step 4: Run test to verify it fails**

Run:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m pytest tests\test_app_factory.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'gmlab'
```

- [ ] **Step 5: Implement the minimal Flask app**

Write `D:\dev-project\gm-lab\gmlab\config.py`:

```python
from __future__ import annotations

import os
from pathlib import Path


class Config:
    SECRET_KEY = os.environ.get("GM_LAB_SECRET_KEY", "dev-change-me")
    DATABASE_PATH = os.environ.get(
        "GM_LAB_DATABASE_PATH",
        str(Path.cwd() / "instance" / "gm_lab.sqlite3"),
    )
    SESSION_COOKIE_NAME = "gm_lab_session"
    SESSION_COOKIE_DOMAIN = os.environ.get("GM_LAB_COOKIE_DOMAIN")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
```

Write `D:\dev-project\gm-lab\gmlab\__init__.py`:

```python
from __future__ import annotations

from flask import Flask, jsonify

from .config import Config


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)

    if test_config:
        app.config.update(test_config)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    return app
```

Write `D:\dev-project\gm-lab\app.py`:

```python
from gmlab import create_app

app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8100, debug=False)
```

- [ ] **Step 6: Run test to verify it passes**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests\test_app_factory.py -q
```

Expected:

```text
1 passed
```

- [ ] **Step 7: Commit**

Run:

```powershell
git add .gitignore requirements.txt pytest.ini app.py gmlab tests
git commit -m "feat: bootstrap gm lab flask app"
```

Expected: commit succeeds and the output includes `feat: bootstrap gm lab flask app`.

## Task 2: SQLite Schema, Seed Apps, And Admin Creation

**Files:**
- Create: `D:\dev-project\gm-lab\gmlab\db.py`
- Create: `D:\dev-project\gm-lab\scripts\create_admin.py`
- Modify: `D:\dev-project\gm-lab\gmlab\__init__.py`
- Modify: `D:\dev-project\gm-lab\tests\conftest.py`
- Create: `D:\dev-project\gm-lab\tests\test_auth.py`

- [ ] **Step 1: Write failing tests for schema and seeded apps**

Replace `D:\dev-project\gm-lab\tests\conftest.py` with:

```python
from pathlib import Path

import pytest

from gmlab import create_app
from gmlab.db import init_db


@pytest.fixture
def app(tmp_path: Path):
    db_path = tmp_path / "gm_lab_test.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_PATH": str(db_path),
            "SECRET_KEY": "test-secret",
            "SESSION_COOKIE_DOMAIN": None,
        }
    )
    with app.app_context():
        init_db()
    return app


@pytest.fixture
def client(app):
    return app.test_client()
```

Write `D:\dev-project\gm-lab\tests\test_auth.py`:

```python
from gmlab.db import create_or_update_user, get_app_by_slug, get_user_by_username, list_apps


def test_init_db_seeds_current_apps(app):
    with app.app_context():
        apps = list_apps()
        article = get_app_by_slug("article")
        target = get_app_by_slug("target")
        pdf = get_app_by_slug("pdf")

    assert [item["slug"] for item in apps] == ["article", "target", "pdf"]
    assert article["upstream_url"] == "http://127.0.0.1:8001"
    assert target["systemd_service"] == "target-running.service"
    assert pdf["entry_host"] == "pdf.lab.genemedi.com"


def test_create_or_update_user_hashes_password(app):
    with app.app_context():
        create_or_update_user(
            username="admin",
            display_name="GM Admin",
            password="secret123",
            role="admin",
            is_active=True,
        )
        user = get_user_by_username("admin")

    assert user["username"] == "admin"
    assert user["role"] == "admin"
    assert user["password_hash"] != "secret123"
    assert user["is_active"] == 1
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests\test_auth.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'gmlab.db'
```

- [ ] **Step 3: Implement database module**

Write `D:\dev-project\gm-lab\gmlab\db.py`:

```python
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from flask import current_app, g
from werkzeug.security import generate_password_hash


SEED_APPS = [
    {
        "slug": "article",
        "name": "AAV and Solidex Content",
        "description": "WeChat article drafting and publishing workflow.",
        "entry_host": "article.lab.genemedi.com",
        "upstream_url": "http://127.0.0.1:8001",
        "systemd_service": "gm-blog.service",
        "sort_order": 10,
    },
    {
        "slug": "target",
        "name": "Target Running",
        "description": "Target research and processing dashboard.",
        "entry_host": "target.lab.genemedi.com",
        "upstream_url": "http://127.0.0.1:5000",
        "systemd_service": "target-running.service",
        "sort_order": 20,
    },
    {
        "slug": "pdf",
        "name": "PDF Generate",
        "description": "GeneMedi PDF document generation tool.",
        "entry_host": "pdf.lab.genemedi.com",
        "upstream_url": "http://127.0.0.1:54876",
        "systemd_service": "genemedi-pdf-platform.service",
        "sort_order": 30,
    },
]


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path = Path(current_app.config["DATABASE_PATH"])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


def close_db(error: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)


def init_db() -> None:
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS apps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            entry_host TEXT NOT NULL UNIQUE,
            upstream_url TEXT NOT NULL,
            systemd_service TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 100,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_app_permissions (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, app_id)
        );
        """
    )
    seed_apps()
    db.commit()


def seed_apps() -> None:
    for app in SEED_APPS:
        upsert_app(**app, is_active=True)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def list_apps(active_only: bool = False) -> list[dict[str, Any]]:
    db = get_db()
    sql = "SELECT * FROM apps"
    params: tuple[Any, ...] = ()
    if active_only:
        sql += " WHERE is_active = ?"
        params = (1,)
    sql += " ORDER BY sort_order ASC, name ASC"
    return [dict(row) for row in db.execute(sql, params).fetchall()]


def get_app_by_slug(slug: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM apps WHERE slug = ?", (slug,)).fetchone()
    return row_to_dict(row)


def get_app_by_host(host: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM apps WHERE entry_host = ?", (host,)).fetchone()
    return row_to_dict(row)


def upsert_app(
    slug: str,
    name: str,
    description: str,
    entry_host: str,
    upstream_url: str,
    systemd_service: str,
    sort_order: int,
    is_active: bool,
) -> None:
    get_db().execute(
        """
        INSERT INTO apps
            (slug, name, description, entry_host, upstream_url, systemd_service, sort_order, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            name = excluded.name,
            description = excluded.description,
            entry_host = excluded.entry_host,
            upstream_url = excluded.upstream_url,
            systemd_service = excluded.systemd_service,
            sort_order = excluded.sort_order,
            is_active = excluded.is_active,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            slug,
            name,
            description,
            entry_host,
            upstream_url,
            systemd_service,
            sort_order,
            1 if is_active else 0,
        ),
    )


def create_or_update_user(
    username: str,
    display_name: str,
    password: str | None,
    role: str,
    is_active: bool,
) -> None:
    existing = get_user_by_username(username)
    db = get_db()
    if existing and password:
        password_hash = generate_password_hash(password)
        db.execute(
            """
            UPDATE users
            SET display_name = ?, password_hash = ?, role = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP
            WHERE username = ?
            """,
            (display_name, password_hash, role, 1 if is_active else 0, username),
        )
    elif existing:
        db.execute(
            """
            UPDATE users
            SET display_name = ?, role = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP
            WHERE username = ?
            """,
            (display_name, role, 1 if is_active else 0, username),
        )
    else:
        if not password:
            raise ValueError("password is required for new users")
        db.execute(
            """
            INSERT INTO users (username, display_name, password_hash, role, is_active)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, display_name, generate_password_hash(password), role, 1 if is_active else 0),
        )
    db.commit()


def get_user_by_username(username: str) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return row_to_dict(row)


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return row_to_dict(row)


def list_users() -> list[dict[str, Any]]:
    rows = get_db().execute("SELECT * FROM users ORDER BY username ASC").fetchall()
    return [dict(row) for row in rows]


def set_user_app_permissions(user_id: int, app_ids: list[int]) -> None:
    db = get_db()
    db.execute("DELETE FROM user_app_permissions WHERE user_id = ?", (user_id,))
    db.executemany(
        "INSERT INTO user_app_permissions (user_id, app_id) VALUES (?, ?)",
        [(user_id, app_id) for app_id in app_ids],
    )
    db.commit()


def get_user_app_ids(user_id: int) -> set[int]:
    rows = get_db().execute(
        "SELECT app_id FROM user_app_permissions WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    return {int(row["app_id"]) for row in rows}


def list_apps_for_user(user_id: int) -> list[dict[str, Any]]:
    rows = get_db().execute(
        """
        SELECT apps.*
        FROM apps
        JOIN user_app_permissions ON user_app_permissions.app_id = apps.id
        WHERE user_app_permissions.user_id = ? AND apps.is_active = 1
        ORDER BY apps.sort_order ASC, apps.name ASC
        """,
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def user_can_access_app(user_id: int, app_id: int) -> bool:
    row = get_db().execute(
        """
        SELECT 1
        FROM user_app_permissions
        WHERE user_id = ? AND app_id = ?
        """,
        (user_id, app_id),
    ).fetchone()
    return row is not None
```

Modify `D:\dev-project\gm-lab\gmlab\__init__.py`:

```python
from __future__ import annotations

import click
from flask import Flask, jsonify

from . import db
from .config import Config


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)

    if test_config:
        app.config.update(test_config)

    db.init_app(app)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    return app


@click.command("init-db")
def init_db_command() -> None:
    db.init_db()
    click.echo("Initialized GM-LAB database.")
```

Write `D:\dev-project\gm-lab\scripts\create_admin.py`:

```python
from __future__ import annotations

import argparse

from gmlab import create_app
from gmlab.db import create_or_update_user, init_db


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or reset a GM-LAB admin user.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--display-name", default="GM Admin")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        init_db()
        create_or_update_user(
            username=args.username,
            display_name=args.display_name,
            password=args.password,
            role="admin",
            is_active=True,
        )

    print(f"Admin user ready: {args.username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests\test_app_factory.py tests\test_auth.py -q
```

Expected:

```text
3 passed
```

- [ ] **Step 5: Commit**

Run:

```powershell
git add gmlab tests scripts
git commit -m "feat: add sqlite schema and seed data"
```

Expected: commit succeeds and the output includes `feat: add sqlite schema and seed data`.

## Task 3: Login, Logout, And Session Helpers

**Files:**
- Create: `D:\dev-project\gm-lab\gmlab\auth.py`
- Create: `D:\dev-project\gm-lab\gmlab\routes.py`
- Create: `D:\dev-project\gm-lab\gmlab\templates\base.html`
- Create: `D:\dev-project\gm-lab\gmlab\templates\login.html`
- Create: `D:\dev-project\gm-lab\gmlab\templates\forbidden.html`
- Create: `D:\dev-project\gm-lab\gmlab\static\styles.css`
- Modify: `D:\dev-project\gm-lab\gmlab\__init__.py`
- Modify: `D:\dev-project\gm-lab\tests\test_auth.py`

- [ ] **Step 1: Add failing login/logout tests**

Append to `D:\dev-project\gm-lab\tests\test_auth.py`:

```python
from gmlab.db import set_user_app_permissions


def test_login_logout_flow(client, app):
    with app.app_context():
        create_or_update_user("admin", "GM Admin", "secret123", "admin", True)

    response = client.post(
        "/login",
        data={"username": "admin", "password": "secret123"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")

    response = client.get("/")
    assert response.status_code == 200
    assert b"GM-LAB" in response.data

    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_login_rejects_wrong_password(client, app):
    with app.app_context():
        create_or_update_user("admin", "GM Admin", "secret123", "admin", True)

    response = client.post(
        "/login",
        data={"username": "admin", "password": "wrong"},
    )

    assert response.status_code == 401
    assert b"Invalid username or password" in response.data


def test_disabled_user_cannot_login(client, app):
    with app.app_context():
        create_or_update_user("disabled", "Disabled", "secret123", "user", False)

    response = client.post(
        "/login",
        data={"username": "disabled", "password": "secret123"},
    )

    assert response.status_code == 401
    assert b"Invalid username or password" in response.data
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests\test_auth.py -q
```

Expected:

```text
AssertionError: assert 404 == 302
```

- [ ] **Step 3: Implement auth helpers and routes**

Write `D:\dev-project\gm-lab\gmlab\auth.py`:

```python
from __future__ import annotations

from functools import wraps
from typing import Callable

from flask import abort, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash

from .db import get_user_by_id, get_user_by_username


def load_logged_in_user() -> None:
    user_id = session.get("user_id")
    g.user = get_user_by_id(user_id) if user_id else None


def verify_login(username: str, password: str) -> dict | None:
    user = get_user_by_username(username.strip())
    if not user or not user["is_active"]:
        return None
    if not check_password_hash(user["password_hash"], password):
        return None
    return user


def login_user(user: dict) -> None:
    session.clear()
    session["user_id"] = user["id"]


def logout_user() -> None:
    session.clear()


def login_required(view: Callable):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.get("user") is None:
            next_url = request.url
            return redirect(url_for("routes.login", next=next_url))
        return view(**kwargs)

    return wrapped_view


def admin_required(view: Callable):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.get("user") is None:
            next_url = request.url
            return redirect(url_for("routes.login", next=next_url))
        if g.user["role"] != "admin":
            abort(403)
        return view(**kwargs)

    return wrapped_view
```

Write `D:\dev-project\gm-lab\gmlab\routes.py`:

```python
from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for

from .auth import login_required, login_user, logout_user, verify_login
from .db import list_apps_for_user

bp = Blueprint("routes", __name__)


@bp.get("/")
@login_required
def launcher():
    apps = list_apps_for_user(request.environ["gmlab.user_id"])
    return render_template("launcher.html", apps=apps)


@bp.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "GET":
        return render_template("login.html", error=None)

    user = verify_login(request.form.get("username", ""), request.form.get("password", ""))
    if not user:
        return render_template("login.html", error="Invalid username or password"), 401

    login_user(user)
    return redirect(request.args.get("next") or url_for("routes.launcher"))


@bp.post("/logout")
def logout():
    logout_user()
    return redirect(url_for("routes.login"))
```

Before registering routes, `launcher()` needs a reliable user id. Modify it in the same file:

```python
from flask import Blueprint, g, redirect, render_template, request, url_for
```

Then replace the `launcher()` body with:

```python
@bp.get("/")
@login_required
def launcher():
    apps = list_apps_for_user(g.user["id"])
    return render_template("launcher.html", apps=apps)
```

Write `D:\dev-project\gm-lab\gmlab\templates\base.html`:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ title or "GM-LAB" }}</title>
    <link rel="stylesheet" href="{{ url_for('static', filename='styles.css') }}">
  </head>
  <body>
    <header class="topbar">
      <a class="brand" href="{{ url_for('routes.launcher') }}">GM-LAB</a>
      {% if g.user %}
        <nav>
          {% if g.user.role == "admin" %}
            <a href="{{ url_for('routes.admin_users') }}">Users</a>
            <a href="{{ url_for('routes.admin_apps') }}">Apps</a>
            <a href="{{ url_for('routes.admin_permissions') }}">Permissions</a>
            <a href="{{ url_for('routes.status_page') }}">Status</a>
          {% endif %}
          <form method="post" action="{{ url_for('routes.logout') }}">
            <button type="submit">Logout</button>
          </form>
        </nav>
      {% endif %}
    </header>
    <main class="page">
      {% block body %}{% endblock %}
    </main>
  </body>
</html>
```

Write `D:\dev-project\gm-lab\gmlab\templates\login.html`:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>GM-LAB</title>
    <link rel="stylesheet" href="{{ url_for('static', filename='styles.css') }}">
  </head>
  <body class="login-page">
    <form class="login-box" method="post">
      <h1>GM-LAB</h1>
      {% if error %}<p class="error">{{ error }}</p>{% endif %}
      <label>
        Username
        <input name="username" autocomplete="username" required>
      </label>
      <label>
        Password
        <input name="password" type="password" autocomplete="current-password" required>
      </label>
      <button type="submit">Login</button>
    </form>
  </body>
</html>
```

Write `D:\dev-project\gm-lab\gmlab\templates\launcher.html`:

```html
{% extends "base.html" %}
{% block body %}
  <section class="launcher">
    {% for app in apps %}
      <a class="app-card" href="https://{{ app.entry_host }}/">
        <strong>{{ app.name }}</strong>
        <span>{{ app.description }}</span>
      </a>
    {% else %}
      <p>No applications are assigned to your account.</p>
    {% endfor %}
  </section>
{% endblock %}
```

Write `D:\dev-project\gm-lab\gmlab\templates\forbidden.html`:

```html
{% extends "base.html" %}
{% block body %}
  <h1>Access denied</h1>
  <p>Your account does not have access to this application.</p>
{% endblock %}
```

Write `D:\dev-project\gm-lab\gmlab\static\styles.css`:

```css
:root {
  color-scheme: light;
  font-family: Arial, Helvetica, sans-serif;
  color: #172033;
  background: #f6f7f9;
}

body {
  margin: 0;
}

a {
  color: inherit;
}

.topbar {
  align-items: center;
  background: #ffffff;
  border-bottom: 1px solid #e3e6eb;
  display: flex;
  justify-content: space-between;
  min-height: 56px;
  padding: 0 24px;
}

.brand {
  font-size: 20px;
  font-weight: 700;
  text-decoration: none;
}

.topbar nav {
  align-items: center;
  display: flex;
  gap: 16px;
}

.topbar form {
  margin: 0;
}

.page {
  margin: 0 auto;
  max-width: 1100px;
  padding: 32px 24px;
}

.login-page {
  align-items: center;
  display: flex;
  min-height: 100vh;
  justify-content: center;
}

.login-box {
  background: #ffffff;
  border: 1px solid #e3e6eb;
  border-radius: 8px;
  display: grid;
  gap: 16px;
  padding: 28px;
  width: min(360px, calc(100vw - 40px));
}

.login-box h1 {
  margin: 0 0 8px;
}

label {
  display: grid;
  gap: 6px;
  font-weight: 600;
}

input,
select,
button,
textarea {
  font: inherit;
}

input,
select,
textarea {
  border: 1px solid #cfd5df;
  border-radius: 6px;
  padding: 9px 10px;
}

button,
.button {
  background: #1c5fd4;
  border: 0;
  border-radius: 6px;
  color: #ffffff;
  cursor: pointer;
  padding: 9px 14px;
  text-decoration: none;
}

.error {
  background: #fff0f0;
  border: 1px solid #ffd0d0;
  border-radius: 6px;
  color: #9b1c1c;
  margin: 0;
  padding: 10px;
}

.launcher {
  display: grid;
  gap: 16px;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}

.app-card {
  background: #ffffff;
  border: 1px solid #e3e6eb;
  border-radius: 8px;
  display: grid;
  gap: 8px;
  padding: 18px;
  text-decoration: none;
}

.app-card strong {
  font-size: 18px;
}

.app-card span {
  color: #5a6578;
}
```

Modify `D:\dev-project\gm-lab\gmlab\__init__.py`:

```python
from __future__ import annotations

import click
from flask import Flask, jsonify, render_template

from . import db
from .auth import load_logged_in_user
from .config import Config
from .routes import bp


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)

    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    app.before_request(load_logged_in_user)
    app.register_blueprint(bp)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.errorhandler(403)
    def forbidden(error):
        return render_template("forbidden.html"), 403

    return app


@click.command("init-db")
def init_db_command() -> None:
    db.init_db()
    click.echo("Initialized GM-LAB database.")
```

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests\test_app_factory.py tests\test_auth.py -q
```

Expected:

```text
6 passed
```

- [ ] **Step 5: Commit**

Run:

```powershell
git add gmlab tests
git commit -m "feat: add login and session handling"
```

Expected: commit succeeds and the output includes `feat: add login and session handling`.

## Task 4: Admin Users, Apps, And Permissions

**Files:**
- Modify: `D:\dev-project\gm-lab\gmlab\db.py`
- Modify: `D:\dev-project\gm-lab\gmlab\routes.py`
- Create: `D:\dev-project\gm-lab\gmlab\templates\admin_users.html`
- Create: `D:\dev-project\gm-lab\gmlab\templates\admin_apps.html`
- Create: `D:\dev-project\gm-lab\gmlab\templates\admin_permissions.html`
- Create: `D:\dev-project\gm-lab\tests\test_admin.py`

- [ ] **Step 1: Write failing admin tests**

Write `D:\dev-project\gm-lab\tests\test_admin.py`:

```python
from gmlab.db import (
    create_or_update_user,
    get_app_by_slug,
    get_user_app_ids,
    get_user_by_username,
    upsert_app,
)


def login(client, app):
    with app.app_context():
        create_or_update_user("admin", "GM Admin", "secret123", "admin", True)
    return client.post("/login", data={"username": "admin", "password": "secret123"})


def test_admin_can_create_user(client, app):
    login(client, app)

    response = client.post(
        "/admin/users",
        data={
            "username": "alice",
            "display_name": "Alice",
            "password": "alice-pass",
            "role": "user",
            "is_active": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    with app.app_context():
        user = get_user_by_username("alice")
    assert user["display_name"] == "Alice"
    assert user["role"] == "user"


def test_non_admin_cannot_open_admin(client, app):
    with app.app_context():
        create_or_update_user("bob", "Bob", "secret123", "user", True)

    client.post("/login", data={"username": "bob", "password": "secret123"})
    response = client.get("/admin/users")

    assert response.status_code == 403


def test_admin_can_update_app(client, app):
    login(client, app)

    response = client.post(
        "/admin/apps",
        data={
            "slug": "article",
            "name": "Article Tool",
            "description": "Drafts",
            "entry_host": "article.lab.genemedi.com",
            "upstream_url": "http://127.0.0.1:8001",
            "systemd_service": "gm-blog.service",
            "sort_order": "10",
            "is_active": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    with app.app_context():
        app_record = get_app_by_slug("article")
    assert app_record["name"] == "Article Tool"


def test_admin_can_assign_permissions(client, app):
    login(client, app)
    with app.app_context():
        create_or_update_user("alice", "Alice", "alice-pass", "user", True)
        user = get_user_by_username("alice")
        article = get_app_by_slug("article")
        pdf = get_app_by_slug("pdf")

    response = client.post(
        "/admin/permissions",
        data={
            "user_id": str(user["id"]),
            "app_ids": [str(article["id"]), str(pdf["id"])],
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    with app.app_context():
        assert get_user_app_ids(user["id"]) == {article["id"], pdf["id"]}
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests\test_admin.py -q
```

Expected:

```text
AssertionError: assert 404 == 302
```

- [ ] **Step 3: Add admin routes**

Append to `D:\dev-project\gm-lab\gmlab\routes.py`:

```python
from .auth import admin_required
from .db import (
    create_or_update_user,
    get_user_app_ids,
    list_apps,
    list_users,
    set_user_app_permissions,
    upsert_app,
)


@bp.route("/admin/users", methods=("GET", "POST"))
@admin_required
def admin_users():
    if request.method == "POST":
        create_or_update_user(
            username=request.form["username"].strip(),
            display_name=request.form["display_name"].strip(),
            password=request.form.get("password") or None,
            role=request.form["role"],
            is_active=request.form.get("is_active") == "1",
        )
        return redirect(url_for("routes.admin_users"))
    return render_template("admin_users.html", users=list_users())


@bp.route("/admin/apps", methods=("GET", "POST"))
@admin_required
def admin_apps():
    if request.method == "POST":
        upsert_app(
            slug=request.form["slug"].strip(),
            name=request.form["name"].strip(),
            description=request.form["description"].strip(),
            entry_host=request.form["entry_host"].strip(),
            upstream_url=request.form["upstream_url"].strip(),
            systemd_service=request.form["systemd_service"].strip(),
            sort_order=int(request.form["sort_order"]),
            is_active=request.form.get("is_active") == "1",
        )
        get_db = __import__("gmlab.db", fromlist=["get_db"]).get_db
        get_db().commit()
        return redirect(url_for("routes.admin_apps"))
    return render_template("admin_apps.html", apps=list_apps())


@bp.route("/admin/permissions", methods=("GET", "POST"))
@admin_required
def admin_permissions():
    users = list_users()
    apps = list_apps()
    selected_user_id = int(request.form.get("user_id") or request.args.get("user_id") or (users[0]["id"] if users else 0))

    if request.method == "POST":
        app_ids = [int(value) for value in request.form.getlist("app_ids")]
        set_user_app_permissions(selected_user_id, app_ids)
        return redirect(url_for("routes.admin_permissions", user_id=selected_user_id))

    selected_app_ids = get_user_app_ids(selected_user_id) if selected_user_id else set()
    return render_template(
        "admin_permissions.html",
        users=users,
        apps=apps,
        selected_user_id=selected_user_id,
        selected_app_ids=selected_app_ids,
    )
```

Immediately clean the dynamic import by modifying the import block at the top of `routes.py` to include `get_db`, and replace the two dynamic-import lines:

```python
from .db import (
    create_or_update_user,
    get_db,
    get_user_app_ids,
    list_apps,
    list_apps_for_user,
    list_users,
    set_user_app_permissions,
    upsert_app,
)
```

Then replace:

```python
        get_db = __import__("gmlab.db", fromlist=["get_db"]).get_db
        get_db().commit()
```

with:

```python
        get_db().commit()
```

Write `D:\dev-project\gm-lab\gmlab\templates\admin_users.html`:

```html
{% extends "base.html" %}
{% block body %}
  <h1>Users</h1>
  <form class="panel" method="post">
    <label>Username <input name="username" required></label>
    <label>Display name <input name="display_name" required></label>
    <label>Password <input name="password" type="password" required></label>
    <label>Role
      <select name="role">
        <option value="user">user</option>
        <option value="admin">admin</option>
      </select>
    </label>
    <label><input type="checkbox" name="is_active" value="1" checked> Active</label>
    <button type="submit">Save user</button>
  </form>
  <table>
    <thead><tr><th>Username</th><th>Name</th><th>Role</th><th>Active</th></tr></thead>
    <tbody>
      {% for user in users %}
        <tr>
          <td>{{ user.username }}</td>
          <td>{{ user.display_name }}</td>
          <td>{{ user.role }}</td>
          <td>{{ "yes" if user.is_active else "no" }}</td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
{% endblock %}
```

Write `D:\dev-project\gm-lab\gmlab\templates\admin_apps.html`:

```html
{% extends "base.html" %}
{% block body %}
  <h1>Apps</h1>
  <form class="panel" method="post">
    <label>Slug <input name="slug" required></label>
    <label>Name <input name="name" required></label>
    <label>Description <input name="description"></label>
    <label>Host <input name="entry_host" required></label>
    <label>Upstream URL <input name="upstream_url" required></label>
    <label>systemd service <input name="systemd_service"></label>
    <label>Sort order <input name="sort_order" type="number" value="100" required></label>
    <label><input type="checkbox" name="is_active" value="1" checked> Active</label>
    <button type="submit">Save app</button>
  </form>
  <table>
    <thead><tr><th>Slug</th><th>Name</th><th>Host</th><th>Upstream</th><th>Active</th></tr></thead>
    <tbody>
      {% for app in apps %}
        <tr>
          <td>{{ app.slug }}</td>
          <td>{{ app.name }}</td>
          <td>{{ app.entry_host }}</td>
          <td>{{ app.upstream_url }}</td>
          <td>{{ "yes" if app.is_active else "no" }}</td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
{% endblock %}
```

Write `D:\dev-project\gm-lab\gmlab\templates\admin_permissions.html`:

```html
{% extends "base.html" %}
{% block body %}
  <h1>Permissions</h1>
  <form class="panel" method="post">
    <label>User
      <select name="user_id" onchange="window.location='{{ url_for('routes.admin_permissions') }}?user_id=' + this.value">
        {% for user in users %}
          <option value="{{ user.id }}" {% if user.id == selected_user_id %}selected{% endif %}>{{ user.username }}</option>
        {% endfor %}
      </select>
    </label>
    <div class="checkbox-list">
      {% for app in apps %}
        <label>
          <input type="checkbox" name="app_ids" value="{{ app.id }}" {% if app.id in selected_app_ids %}checked{% endif %}>
          {{ app.name }}
        </label>
      {% endfor %}
    </div>
    <button type="submit">Save permissions</button>
  </form>
{% endblock %}
```

Append to `D:\dev-project\gm-lab\gmlab\static\styles.css`:

```css
.panel {
  background: #ffffff;
  border: 1px solid #e3e6eb;
  border-radius: 8px;
  display: grid;
  gap: 14px;
  margin-bottom: 24px;
  max-width: 680px;
  padding: 18px;
}

table {
  background: #ffffff;
  border-collapse: collapse;
  width: 100%;
}

th,
td {
  border-bottom: 1px solid #e3e6eb;
  padding: 10px;
  text-align: left;
}

.checkbox-list {
  display: grid;
  gap: 10px;
}
```

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests\test_admin.py tests\test_auth.py -q
```

Expected:

```text
10 passed
```

- [ ] **Step 5: Commit**

Run:

```powershell
git add gmlab tests
git commit -m "feat: add admin users apps and permissions"
```

Expected: commit succeeds and the output includes `feat: add admin users apps and permissions`.

## Task 5: Nginx Auth-Check Endpoint

**Files:**
- Modify: `D:\dev-project\gm-lab\gmlab\routes.py`
- Create: `D:\dev-project\gm-lab\tests\test_auth_check.py`

- [ ] **Step 1: Write failing auth-check tests**

Write `D:\dev-project\gm-lab\tests\test_auth_check.py`:

```python
from gmlab.db import create_or_update_user, get_app_by_slug, get_user_by_username, set_user_app_permissions


def login_as(client, app, username: str, password: str = "secret123", role: str = "user"):
    with app.app_context():
        create_or_update_user(username, username.title(), password, role, True)
    return client.post("/login", data={"username": username, "password": password})


def grant(app, username: str, slug: str):
    with app.app_context():
        user = get_user_by_username(username)
        app_record = get_app_by_slug(slug)
        set_user_app_permissions(user["id"], [app_record["id"]])


def test_auth_check_redirects_logged_out_user(client):
    response = client.get("/auth/check", headers={"X-Original-Host": "article.lab.genemedi.com"})

    assert response.status_code == 401
    assert response.headers["X-Login-URL"].startswith("https://lab.genemedi.com/login")


def test_auth_check_allows_authorized_user(client, app):
    login_as(client, app, "alice")
    grant(app, "alice", "article")

    response = client.get("/auth/check", headers={"X-Original-Host": "article.lab.genemedi.com"})

    assert response.status_code == 204
    assert response.data == b""


def test_auth_check_blocks_user_without_app_permission(client, app):
    login_as(client, app, "alice")

    response = client.get("/auth/check", headers={"X-Original-Host": "pdf.lab.genemedi.com"})

    assert response.status_code == 403


def test_auth_check_blocks_unknown_host(client, app):
    login_as(client, app, "alice")

    response = client.get("/auth/check", headers={"X-Original-Host": "unknown.lab.genemedi.com"})

    assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests\test_auth_check.py -q
```

Expected:

```text
AssertionError: assert 404 == 401
```

- [ ] **Step 3: Implement auth-check route**

Modify the import block in `D:\dev-project\gm-lab\gmlab\routes.py` to include these names:

```python
from flask import Blueprint, Response, g, redirect, render_template, request, url_for
from .db import get_app_by_host, user_can_access_app
```

Append:

```python
@bp.get("/auth/check")
def auth_check():
    host = request.headers.get("X-Original-Host") or request.headers.get("Host", "")
    host = host.split(":", 1)[0].strip().lower()
    app_record = get_app_by_host(host)

    if not app_record or not app_record["is_active"]:
        return Response(status=404)

    if g.get("user") is None:
        login_url = f"https://lab.genemedi.com/login?next=https://{host}/"
        response = Response(status=401)
        response.headers["X-Login-URL"] = login_url
        return response

    if not user_can_access_app(g.user["id"], app_record["id"]):
        return Response(status=403)

    return Response(status=204)
```

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests\test_auth_check.py tests\test_admin.py tests\test_auth.py -q
```

Expected:

```text
14 passed
```

- [ ] **Step 5: Commit**

Run:

```powershell
git add gmlab tests
git commit -m "feat: add reverse proxy auth check"
```

Expected: commit succeeds and the output includes `feat: add reverse proxy auth check`.

## Task 6: Read-Only Service Status Page

**Files:**
- Create: `D:\dev-project\gm-lab\gmlab\status.py`
- Modify: `D:\dev-project\gm-lab\gmlab\routes.py`
- Create: `D:\dev-project\gm-lab\gmlab\templates\status.html`
- Create: `D:\dev-project\gm-lab\tests\test_status.py`

- [ ] **Step 1: Write failing status tests**

Write `D:\dev-project\gm-lab\tests\test_status.py`:

```python
from gmlab.status import parse_systemctl_result


def test_parse_systemctl_active():
    assert parse_systemctl_result(0, "active\n", "") == "active"


def test_parse_systemctl_inactive():
    assert parse_systemctl_result(3, "inactive\n", "") == "inactive"


def test_parse_systemctl_unknown():
    assert parse_systemctl_result(4, "", "not-found\n") == "unknown"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests\test_status.py -q
```

Expected:

```text
ModuleNotFoundError: No module named 'gmlab.status'
```

- [ ] **Step 3: Implement status adapter**

Write `D:\dev-project\gm-lab\gmlab\status.py`:

```python
from __future__ import annotations

import subprocess


def parse_systemctl_result(returncode: int, stdout: str, stderr: str) -> str:
    value = stdout.strip()
    if returncode == 0 and value == "active":
        return "active"
    if value in {"inactive", "failed", "activating", "deactivating"}:
        return value
    return "unknown"


def get_service_status(service_name: str) -> str:
    if not service_name:
        return "unknown"
    result = subprocess.run(
        ["systemctl", "is-active", service_name],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    return parse_systemctl_result(result.returncode, result.stdout, result.stderr)
```

Append to `D:\dev-project\gm-lab\gmlab\routes.py`:

```python
from .status import get_service_status


@bp.get("/admin/status")
@admin_required
def status_page():
    apps = list_apps()
    rows = []
    for app_record in apps:
        rows.append(
            {
                "name": app_record["name"],
                "slug": app_record["slug"],
                "entry_host": app_record["entry_host"],
                "systemd_service": app_record["systemd_service"],
                "status": get_service_status(app_record["systemd_service"]),
            }
        )
    return render_template("status.html", rows=rows)
```

Write `D:\dev-project\gm-lab\gmlab\templates\status.html`:

```html
{% extends "base.html" %}
{% block body %}
  <h1>Status</h1>
  <table>
    <thead><tr><th>App</th><th>Host</th><th>Service</th><th>Status</th></tr></thead>
    <tbody>
      {% for row in rows %}
        <tr>
          <td>{{ row.name }}</td>
          <td>{{ row.entry_host }}</td>
          <td>{{ row.systemd_service }}</td>
          <td>{{ row.status }}</td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
{% endblock %}
```

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python -m pytest -q
```

Expected:

```text
17 passed
```

- [ ] **Step 5: Commit**

Run:

```powershell
git add gmlab tests
git commit -m "feat: add read only app status"
```

Expected: commit succeeds and the output includes `feat: add read only app status`.

## Task 7: Deployment Files

**Files:**
- Create: `D:\dev-project\gm-lab\deploy\gm-lab.service`
- Create: `D:\dev-project\gm-lab\deploy\nginx\gm-lab.conf`
- Create: `D:\dev-project\gm-lab\README.md`
- Create: `D:\dev-project\gm-lab\tests\test_deploy_files.py`

- [ ] **Step 1: Write failing deployment file tests**

Write `D:\dev-project\gm-lab\tests\test_deploy_files.py`:

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_systemd_service_uses_gunicorn_on_8100():
    text = (ROOT / "deploy" / "gm-lab.service").read_text(encoding="utf-8")

    assert "WorkingDirectory=/opt/gm-lab/current" in text
    assert "--bind 127.0.0.1:8100" in text
    assert "app:app" in text


def test_nginx_config_contains_required_hosts():
    text = (ROOT / "deploy" / "nginx" / "gm-lab.conf").read_text(encoding="utf-8")

    assert "server_name lab.genemedi.com;" in text
    assert "server_name article.lab.genemedi.com;" in text
    assert "server_name target.lab.genemedi.com;" in text
    assert "server_name pdf.lab.genemedi.com;" in text
    assert "proxy_pass http://127.0.0.1:8100/auth/check;" in text
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests\test_deploy_files.py -q
```

Expected:

```text
FileNotFoundError
```

- [ ] **Step 3: Add systemd service**

Write `D:\dev-project\gm-lab\deploy\gm-lab.service`:

```ini
[Unit]
Description=GM-LAB internal application platform
After=network.target

[Service]
Type=simple
User=admin
Group=admin
WorkingDirectory=/opt/gm-lab/current
EnvironmentFile=/opt/gm-lab/shared/.env
ExecStart=/opt/gm-lab/current/.venv/bin/gunicorn --workers 2 --bind 127.0.0.1:8100 --access-logfile - --error-logfile - app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Add Nginx config**

Write `D:\dev-project\gm-lab\deploy\nginx\gm-lab.conf`:

```nginx
server {
    listen 80;
    server_name lab.genemedi.com;

    location / {
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_pass http://127.0.0.1:8100;
    }
}

server {
    listen 80;
    server_name article.lab.genemedi.com;

    location = /_gm_lab_auth {
        internal;
        proxy_pass http://127.0.0.1:8100/auth/check;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header Cookie $http_cookie;
        proxy_set_header X-Original-Host $host;
    }

    error_page 401 =302 https://lab.genemedi.com/login?next=$scheme://$host$request_uri;
    error_page 403 = @gm_lab_forbidden;

    location @gm_lab_forbidden {
        return 403 "Access denied\n";
    }

    location / {
        auth_request /_gm_lab_auth;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_pass http://127.0.0.1:8001;
    }
}

server {
    listen 80;
    server_name target.lab.genemedi.com;

    location = /_gm_lab_auth {
        internal;
        proxy_pass http://127.0.0.1:8100/auth/check;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header Cookie $http_cookie;
        proxy_set_header X-Original-Host $host;
    }

    error_page 401 =302 https://lab.genemedi.com/login?next=$scheme://$host$request_uri;
    error_page 403 = @gm_lab_forbidden;

    location @gm_lab_forbidden {
        return 403 "Access denied\n";
    }

    location / {
        auth_request /_gm_lab_auth;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_pass http://127.0.0.1:5000;
    }
}

server {
    listen 80;
    server_name pdf.lab.genemedi.com;

    location = /_gm_lab_auth {
        internal;
        proxy_pass http://127.0.0.1:8100/auth/check;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header Cookie $http_cookie;
        proxy_set_header X-Original-Host $host;
    }

    error_page 401 =302 https://lab.genemedi.com/login?next=$scheme://$host$request_uri;
    error_page 403 = @gm_lab_forbidden;

    location @gm_lab_forbidden {
        return 403 "Access denied\n";
    }

    location / {
        auth_request /_gm_lab_auth;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_pass http://127.0.0.1:54876;
    }
}
```

- [ ] **Step 5: Add README**

Write `D:\dev-project\gm-lab\README.md`:

````markdown
# GM-LAB

GM-LAB is GeneMedi's internal app launcher and lightweight permission layer.

## Local Development

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m flask --app app init-db
.\.venv\Scripts\python scripts\create_admin.py --username admin --password change-me-now --display-name "GM Admin"
.\.venv\Scripts\python app.py
```

Open `http://127.0.0.1:8100`.

## Seed Applications

- `article.lab.genemedi.com` -> `http://127.0.0.1:8001`
- `target.lab.genemedi.com` -> `http://127.0.0.1:5000`
- `pdf.lab.genemedi.com` -> `http://127.0.0.1:54876`
````

- [ ] **Step 6: Run tests**

Run:

```powershell
.\.venv\Scripts\python -m pytest -q
```

Expected:

```text
19 passed
```

- [ ] **Step 7: Commit**

Run:

```powershell
git add README.md deploy tests
git commit -m "chore: add gm lab deployment files"
```

Expected: commit succeeds and the output includes `chore: add gm lab deployment files`.

## Task 8: Local Manual Smoke Test

**Files:**
- No new files.

- [ ] **Step 1: Initialize local database and admin**

Run:

```powershell
Set-Location 'D:\dev-project\gm-lab'
.\.venv\Scripts\python -m flask --app app init-db
.\.venv\Scripts\python scripts\create_admin.py --username admin --password change-me-now --display-name "GM Admin"
```

Expected:

```text
Initialized GM-LAB database.
Admin user ready: admin
```

- [ ] **Step 2: Start local server**

Run:

```powershell
.\.venv\Scripts\python app.py
```

Expected:

```text
Running on http://127.0.0.1:8100
```

- [ ] **Step 3: Verify local health from a second terminal**

Run:

```powershell
curl.exe -sS http://127.0.0.1:8100/health
```

Expected:

```json
{"status":"ok"}
```

- [ ] **Step 4: Verify login page**

Run:

```powershell
curl.exe -i http://127.0.0.1:8100/login
```

Expected:

```text
HTTP/1.1 200 OK
```

The response body contains:

```text
GM-LAB
```

- [ ] **Step 5: Commit no-op check**

Run:

```powershell
git status --short
```

Expected:

```text

```

## Task 9: GitLab Remote And Server Deployment

**Files:**
- No local file changes expected.

- [ ] **Step 1: Create GitLab repository**

Create this GitLab project before running commands:

```text
https://gitlab.com/genemedi/gm-lab
```

- [ ] **Step 2: Push local repository**

Run:

```powershell
Set-Location 'D:\dev-project\gm-lab'
git branch -M main
git remote add origin https://gitlab.com/genemedi/gm-lab.git
git push -u origin main
```

Expected:

```text
branch 'main' set up to track 'origin/main'
```

- [ ] **Step 3: Create server directories**

Run:

```powershell
ssh -i "$HOME\.ssh\id_ed25519" root@47.102.223.198 "mkdir -p /opt/gm-lab/repo.git /opt/gm-lab/current /opt/gm-lab/shared && chown -R admin:admin /opt/gm-lab"
```

Expected:

```text

```

- [ ] **Step 4: Initialize bare repo and post-receive hook**

Run:

```powershell
$script = @'
set -euo pipefail
cd /opt/gm-lab/repo.git
git init --bare
cat > hooks/post-receive <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail
GIT_WORK_TREE=/opt/gm-lab/current git checkout -f main
cd /opt/gm-lab/current
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p /opt/gm-lab/shared
chown -R admin:admin /opt/gm-lab/current /opt/gm-lab/shared
systemctl restart gm-lab.service || true
HOOK
chmod +x hooks/post-receive
chown -R admin:admin /opt/gm-lab
'@
$script | ssh -i "$HOME\.ssh\id_ed25519" root@47.102.223.198 'bash -s'
```

Expected:

```text
Initialized empty Git repository in /opt/gm-lab/repo.git/
```

- [ ] **Step 5: Add production git remote and push**

Run:

```powershell
Set-Location 'D:\dev-project\gm-lab'
git remote add prod ssh://root@47.102.223.198/opt/gm-lab/repo.git
git push prod main
```

Expected:

```text
To ssh://47.102.223.198/opt/gm-lab/repo.git
 * [new branch]      main -> main
```

- [ ] **Step 6: Create server environment file**

Run:

```powershell
$script = @'
set -euo pipefail
python3 - <<'PY' > /opt/gm-lab/shared/.env
import secrets
print("GM_LAB_SECRET_KEY=" + secrets.token_urlsafe(48))
print("GM_LAB_DATABASE_PATH=/opt/gm-lab/shared/gm_lab.sqlite3")
print("GM_LAB_COOKIE_DOMAIN=.lab.genemedi.com")
PY
chown admin:admin /opt/gm-lab/shared/.env
chmod 600 /opt/gm-lab/shared/.env
'@
$script | ssh -i "$HOME\.ssh\id_ed25519" root@47.102.223.198 'bash -s'
```

Expected:

```text

```

Verify the generated secret file is present without printing the secret:

```powershell
ssh -i "$HOME\.ssh\id_ed25519" root@47.102.223.198 "test -s /opt/gm-lab/shared/.env && ls -l /opt/gm-lab/shared/.env"
```

- [ ] **Step 7: Install systemd service**

Run:

```powershell
ssh -i "$HOME\.ssh\id_ed25519" root@47.102.223.198 "cp /opt/gm-lab/current/deploy/gm-lab.service /etc/systemd/system/gm-lab.service && systemctl daemon-reload && systemctl enable gm-lab.service && systemctl restart gm-lab.service && systemctl status gm-lab.service --no-pager"
```

Expected:

```text
Active: active (running)
```

- [ ] **Step 8: Initialize production database and admin**

Run:

```powershell
ssh -i "$HOME\.ssh\id_ed25519" root@47.102.223.198 "cd /opt/gm-lab/current && /opt/gm-lab/current/.venv/bin/flask --app app init-db && /opt/gm-lab/current/.venv/bin/python scripts/create_admin.py --username admin --password 'change-me-now' --display-name 'GM Admin'"
```

Expected:

```text
Initialized GM-LAB database.
Admin user ready: admin
```

- [ ] **Step 9: Verify local server endpoint on production host**

Run:

```powershell
ssh -i "$HOME\.ssh\id_ed25519" root@47.102.223.198 "curl -sS http://127.0.0.1:8100/health"
```

Expected:

```json
{"status":"ok"}
```

## Task 10: DNS, Nginx, And Final Smoke Checks

**Files:**
- No repository file changes unless the Nginx config needs local adjustment after `nginx -t`.

- [ ] **Step 1: Configure DNS records**

Create these DNS records for `genemedi.com`:

```text
lab.genemedi.com          A 47.102.223.198
article.lab.genemedi.com  A 47.102.223.198
target.lab.genemedi.com   A 47.102.223.198
pdf.lab.genemedi.com      A 47.102.223.198
```

- [ ] **Step 2: Install Nginx config**

Run:

```powershell
ssh -i "$HOME\.ssh\id_ed25519" root@47.102.223.198 "cp /opt/gm-lab/current/deploy/nginx/gm-lab.conf /etc/nginx/conf.d/gm-lab.conf && nginx -t"
```

Expected:

```text
syntax is ok
test is successful
```

- [ ] **Step 3: Reload Nginx without touching existing app services**

Run:

```powershell
ssh -i "$HOME\.ssh\id_ed25519" root@47.102.223.198 "systemctl reload nginx && systemctl is-active target-running.service gm-blog.service genemedi-pdf-platform.service gm-lab.service"
```

Expected:

```text
active
active
active
active
```

- [ ] **Step 4: Verify GM-LAB public health**

Run:

```powershell
curl.exe -i http://lab.genemedi.com/health
```

Expected:

```text
HTTP/1.1 200 OK
```

Response body:

```json
{"status":"ok"}
```

- [ ] **Step 5: Verify app subdomains require login**

Run:

```powershell
curl.exe -I http://article.lab.genemedi.com/
curl.exe -I http://target.lab.genemedi.com/
curl.exe -I http://pdf.lab.genemedi.com/
```

Expected for each:

```text
HTTP/1.1 302
Location: https://lab.genemedi.com/login
```

- [ ] **Step 6: Browser smoke test**

Open:

```text
http://lab.genemedi.com/login
```

Log in:

```text
username: admin
password: change-me-now
```

Verify:

- The launcher opens.
- The three seeded apps are visible after permissions are granted to admin.
- `article.lab.genemedi.com` opens the GM Blog app.
- `target.lab.genemedi.com` opens Target Running.
- `pdf.lab.genemedi.com` opens PDF Generate.

- [ ] **Step 7: Change admin password**

Use the admin users page to set a non-temporary admin password. Then verify the old password no longer logs in.

Run:

```powershell
curl.exe -i http://lab.genemedi.com/login
```

Expected:

```text
HTTP/1.1 200 OK
```

## Final Verification

Run locally:

```powershell
Set-Location 'D:\dev-project\gm-lab'
.\.venv\Scripts\python -m pytest -q
git status --short --branch
```

Expected:

```text
19 passed
## main...origin/main
```

Run on server:

```powershell
ssh -i "$HOME\.ssh\id_ed25519" root@47.102.223.198 "systemctl is-active gm-lab.service target-running.service gm-blog.service genemedi-pdf-platform.service mysql.service && curl -sS http://127.0.0.1:8100/health"
```

Expected:

```text
active
active
active
active
active
{"status":"ok"}
```
