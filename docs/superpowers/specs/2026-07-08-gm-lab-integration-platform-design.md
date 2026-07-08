# GM-LAB Integration Platform Design

Date: 2026-07-08

## Purpose

GM-LAB is an internal application integration platform for GeneMedi market team tools. It provides one login page, one application launcher, and simple permission management for a small internal user group.

The platform must keep existing applications independent. Each existing app keeps its own local project, Git remote, server deployment path, and systemd service. GM-LAB should integrate them through reverse proxy routing and shared access control, not by merging codebases.

## Current Applications

| Application | Local Repository | Git Remote | Server Service | Server Path | Internal Port | Proposed Entry |
| --- | --- | --- | --- | --- | --- | --- |
| WeChat Article / GM Blog | `D:\dev-project\wechat-article` | `https://gitlab.com/genemedi/gm-blog.git` | `gm-blog.service` | `/opt/gm-blog/current` | `8001` | `article.lab.genemedi.com` |
| Target Running | `D:\dev-project\target-running` | `https://gitlab.com/genemedi/target-running` | `target-running.service` | `/opt/target-running/current` | `5000` | `target.lab.genemedi.com` |
| PDF Generate | `D:\pdf-generate` | `https://gitlab.com/genemedi/pdf-generate.git` | `genemedi-pdf-platform.service` | `/opt/genemedi-pdf-platform/current` | `54876` | `pdf.lab.genemedi.com` |

## Recommended Architecture

Use an independent GM-LAB service plus a reverse proxy:

```text
User
  |
  v
https://lab.genemedi.com
  |
  +-- GM-LAB service: login, app launcher, users, permissions
  |
  +-- article.lab.genemedi.com -> 127.0.0.1:8001
  +-- target.lab.genemedi.com  -> 127.0.0.1:5000
  +-- pdf.lab.genemedi.com     -> 127.0.0.1:54876
```

GM-LAB should be a new lightweight repository:

- Local path: `D:\dev-project\gm-lab`
- Suggested GitLab remote: `https://gitlab.com/genemedi/gm-lab.git`
- Server path: `/opt/gm-lab/current`
- Service name: `gm-lab.service`
- Internal port: `8100`

Nginx should terminate HTTPS and route traffic. GM-LAB handles login and permission checks. Existing apps remain separate systemd services.

## Domain Plan

Initial DNS records:

| Host | Target |
| --- | --- |
| `lab.genemedi.com` | `47.102.223.198` |
| `article.lab.genemedi.com` | `47.102.223.198` |
| `target.lab.genemedi.com` | `47.102.223.198` |
| `pdf.lab.genemedi.com` | `47.102.223.198` |

When more apps are added, this can be replaced or supplemented with a wildcard record:

```text
*.lab.genemedi.com -> 47.102.223.198
```

## Access Model

The first version should stay intentionally simple:

- Internal users only.
- Expected user count: about 5 now, fewer than 10 in the near term.
- Username and password login.
- Server-side session cookie.
- No complex SSO, OAuth, organization directory, MFA, or audit workflow in v1.

Roles:

| Role | Capability |
| --- | --- |
| `admin` | Manage users, applications, and app permissions. View app status. |
| `user` | Log in and open authorized applications only. |

Permission model:

- A user can access zero or more apps.
- The launcher only shows apps the user can access.
- Direct access to an app subdomain should also require permission.

## Data Model

SQLite is sufficient for v1.

Tables:

- `users`
  - `id`
  - `username`
  - `display_name`
  - `password_hash`
  - `role`
  - `is_active`
  - `created_at`
  - `updated_at`
- `apps`
  - `id`
  - `slug`
  - `name`
  - `description`
  - `entry_host`
  - `upstream_url`
  - `systemd_service`
  - `is_active`
  - `sort_order`
- `user_app_permissions`
  - `user_id`
  - `app_id`
  - `created_at`

Seed apps:

| Slug | Name | Host | Upstream |
| --- | --- | --- | --- |
| `article` | AAV and Solidex Content | `article.lab.genemedi.com` | `http://127.0.0.1:8001` |
| `target` | Target Running | `target.lab.genemedi.com` | `http://127.0.0.1:5000` |
| `pdf` | PDF Generate | `pdf.lab.genemedi.com` | `http://127.0.0.1:54876` |

## GM-LAB Pages

V1 pages:

1. Login
   - Title: `GM-LAB`
   - Username
   - Password
   - Submit

2. App launcher
   - Shows authorized app cards only.
   - Each card opens its subdomain.
   - No long descriptions or onboarding copy.

3. Admin users
   - Add/edit/disable users.
   - Reset password.
   - Assign `admin` or `user`.

4. Admin apps
   - Add/edit/disable apps.
   - Configure display name, host, upstream URL, and systemd service name.

5. Admin permissions
   - Checkbox matrix: users by apps.
   - Save permissions.

6. App status
   - Read-only status for configured services.
   - Show `active`, `inactive`, or `unknown`.
   - Do not add restart/stop controls in v1.

## Reverse Proxy Authentication

Nginx should use `auth_request` for app subdomains:

1. User requests `https://article.lab.genemedi.com/`.
2. Nginx sends an internal auth request to GM-LAB.
3. GM-LAB checks the session cookie and whether the user has access to `article`.
4. If allowed, Nginx proxies to `http://127.0.0.1:8001`.
5. If not logged in, redirect to `https://lab.genemedi.com/login?next=...`.
6. If logged in but not authorized, show a simple 403 page.

Cookie domain should be `.lab.genemedi.com` so one login works across app subdomains.

## Existing App Changes

V1 should require no major code changes in existing apps.

Recommended minimal changes:

- Keep each app bound to localhost or an internal-only port after proxy setup is verified.
- Keep each app's current systemd service.
- Keep each app's current Git deployment flow.
- Avoid changing app base paths by using subdomains instead of path prefixes.

Do not move current apps behind GM-LAB until proxy and login are verified on a test subdomain or temporary host.

## Deployment Model

Use the same lightweight deployment style already used on the server:

- One GitLab repo per app.
- One bare repo under `/opt/<app>/repo.git`.
- One current worktree under `/opt/<app>/current`.
- One systemd service per app.
- Nginx as the public entrypoint.

GM-LAB deploy layout:

```text
/opt/gm-lab/
  repo.git
  current/
  shared/
    .env
    gm_lab.sqlite3
```

Suggested service:

```text
gm-lab.service -> 127.0.0.1:8100
```

## Future App Onboarding

To add a new internal app later:

1. Create or identify the app's local repo.
2. Create its GitLab remote.
3. Deploy it as a separate service on the server.
4. Assign an internal port.
5. Add DNS record or wildcard host.
6. Add an app record in GM-LAB.
7. Grant permissions to users.

This keeps GM-LAB as a stable launcher and access layer instead of a growing monolith.

## Non-Goals For V1

Do not include these in the first version:

- SSO or enterprise identity integration.
- Multi-tenant organization structure.
- Complex audit logs.
- Per-page permissions inside each app.
- App stop/restart controls.
- Docker or Kubernetes migration.
- Rewriting existing apps into one codebase.

## Testing Plan

Local tests:

- Login succeeds with a seeded admin.
- Login fails with wrong password.
- Logged-out users are redirected to login.
- Launcher only shows authorized apps.
- Admin can grant and revoke app permissions.
- Disabled users cannot log in.
- Disabled apps do not appear in launcher.
- Auth-check endpoint returns allow/deny for each app slug.

Server smoke checks:

- `https://lab.genemedi.com` opens GM-LAB.
- `https://article.lab.genemedi.com` requires login and then opens GM Blog.
- `https://target.lab.genemedi.com` requires login and then opens Target Running.
- `https://pdf.lab.genemedi.com` requires login and then opens PDF Generate.
- A user without permission gets 403 for direct subdomain access.
- Existing systemd services remain active after Nginx changes.

## First Implementation Milestone

Milestone 1 should deliver:

- New `gm-lab` repository.
- Flask or similarly lightweight server app.
- SQLite user/app/permission storage.
- Login/logout/session handling.
- App launcher.
- Admin user/app/permission pages.
- Auth-check endpoint for Nginx.
- Systemd deployment for `gm-lab.service`.
- Nginx config for `lab.genemedi.com` and the three app subdomains.

The first milestone is complete when the three current applications are reachable through GM-LAB-controlled entries without stopping or merging their existing services.
