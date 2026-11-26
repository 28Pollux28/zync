# Zync CTFd Plugin

This plugin for CTFd integrates with the [Galvanize Instancer](https://github.com/28Pollux28/galvanize) to allow competing teams/users to deploy challenge instances. It adds a challenge type "zync" that works with the Galvanize Instancer API for on-demand challenge deployments.

## Features

### Player Features
* Deploy challenge instances on-demand via Galvanize Instancer
* Time-limited instances with countdown display
* Extend instance duration (when supported by the Instancer)
* Terminate instances manually
* Support for both unique (per-team) and shared challenge instances

### Admin Features
* **Configuration Management**: Configure Instancer URL and JWT secret via web UI or environment variables
* **Admin Dashboard** (`/admin/zync_dashboard`): Real-time monitoring and management interface
  - Error deployments monitoring with auto-refresh (10s)
  - Team deployments overview with auto-refresh (60s)
  - Retry, terminate, and delete actions for failed deployments
  - Team filtering functionality
  - Visual status badges and deployment duration tracking
  - Expandable/collapsible error messages
  - Challenge index reload functionality
* **Challenge Manager** (`/admin/zync_challenges`): replaces the `challenges.sh` script of ctfd-management
  - Repositories: clone (from the sources Galvanize allows, branch or tag), update (fetch + reset), remove
  - Rebuild Docker images (single image or compose stack), for one challenge, a selection or a whole repository
  - Ingest new challenges into CTFd, sync existing ones, delete them, remove their images
  - Dry run, background jobs with live logs, history of the last jobs
* **Environment Variable Support**: Load configuration from env vars at startup (`ZYNC_DEPLOYER_URL`, `ZYNC_JWT_SECRET`)
* Full deployment lifecycle management via Galvanize Instancer

## Installation

1. Rename or copy the plugin folder to `zync` in your CTFd plugins directory:
   ```
   CTFd/CTFd/plugins/zync/
   ```
2. Replace CTFd standard `docker-entrypoint.sh` with the one provided ([ctfd-docker-entrypoint.sh](ctfd-docker-entrypoint.sh)) or add the following lines (enables plugin dependencies installation) before the app start:
   ```
    for d in CTFd/plugins/*; do \
        if [ -f "$d/requirements.txt" ]; then
            pip install --no-cache-dir -r "$d/requirements.txt";
        fi;
    done;
    ```
   
3. Restart CTFd.

4. **Configure the plugin** (choose one method):

   **Option A: Web UI Configuration**
   - Navigate to **Plugins** → **Zync Config** (`/admin/zync_config`)
   - Configure:
     - **Instancer URL**: The full URL to your Galvanize Instancer (e.g., `https://instancer.example.com`)
     - **JWT Secret**: The secret used to sign JWT tokens for the Instancer

   **Option B: Environment Variables** (recommended for production)
   - Set the following environment variables before starting CTFd:
     ```bash
     export ZYNC_DEPLOYER_URL="https://instancer.example.com"
     export ZYNC_JWT_SECRET="your-secret-key-here"
     ```
   - The plugin will automatically load these values into the database at startup
   - Environment variables take precedence and will overwrite existing database configuration

5. Create challenges with the **zync** challenge type.

6. (Optional) Access the **Admin Dashboard** at `/admin/zync_dashboard` to monitor deployments.

## Challenge Manager

The **Zync Challenges** page (`/admin/zync_challenges`) replaces the `challenges.sh` script of ctfd-management. It works on the challenge repositories of Galvanize, which owns them; Zync itself needs no access to the server, Docker or git, only the Galvanize URL and secret of Zync Config.

| Action | Runs in | What it does |
|---|---|---|
| Clone / Update / Remove repository | Galvanize | Clone from an allowed source (`repositories.sources` of Galvanize's config), fetch and reset to the followed branch or tag (images are not rebuilt: use Rebuild), delete the folder (refused while instances of its challenges run). Remove can delete the repository's challenges from CTFd first |
| Rebuild | Galvanize | Build the images with Docker on every Ansible host: `docker build` of the Dockerfile for single-image challenges, `docker compose build` of the services with a `build` section for compose challenges. Only the challenges Galvanize reports something to build for (`build.buildable`) are sent, and the confirmation counts them. *Force* builds without cache |
| Check images / Remove images | Galvanize | Which images exist on each host; remove the built ones. Hosts that did not answer, or where the check failed, are shown apart |
| Ingest | Zync | Create the challenges missing from CTFd, after the challenges they require. A challenge stays hidden until its flags, files, hints, tags, topics and requirements are attached, and is removed again if one fails |
| Sync | Zync | Update the challenges in place: their ID, solves and unlocked hints are kept, unchanged files are not downloaded again, the state is never changed. Requirements are set in a second pass; a change that would form a cycle with the requirements already in CTFd is refused, and the challenge keeps its current ones |
| Rebuild + ingest | both | `challenges.sh`'s default action: every deployable challenge is sent to Galvanize, which builds what there is to build; the challenges whose build failed, or was refused by Galvanize's image policy, are not ingested |
| Delete from CTFd | Zync | Delete the challenges with their solves, flags, hints and files. A challenge still required by a challenge that is not deleted with it is refused, unless *Force* |

Each action applies to one challenge (row buttons), the selected ones, the ones shown by the filters, or the whole repository. *Dry run* shows what ingest, sync and delete would do. Zync's actions run as background jobs, one at a time across CTFd's workers; Galvanize's run as Galvanize operations. Both logs show in the same panel, with the recent history.

A CTFd challenge is linked to the definition file it was ingested from (table `zync_challenge_sources`): two challenges may share a name in different categories, a renamed challenge stays the same challenge, and challenges whose definition left Galvanize (removed repository) can still be deleted. A definition moved inside its repository (folder or file renamed) keeps its challenge when its name and category did not change at the same time. Challenges created by hand or by the old script are adopted when their name and category match.

Safeguards:

- a `zync` challenge Galvanize cannot deploy (a duplicate of another definition's category and name, invalid deployment settings) is neither ingested nor synced;
- `state` must be `visible`, `hidden` or `locked`;
- a requirement names a challenge of the same repository when one has that name (several: write `Category/Name`; not ingested yet: an error), and only otherwise a CTFd challenge, which must be the only one of that name;
- an install interrupted by a worker restart leaves its challenge hidden and marked incomplete: the next ingest deletes and installs it again, sync refuses it;
- a job works on the definitions Galvanize listed when it started, warns when the repository moved to another commit while it ran (e.g. a Galvanize update during the job: sync again), and stops between two challenges when cancelled (or if it stalled long enough for another job to take over). A Galvanize build it waits for is cancelled with it.

Zync calls Galvanize's repository, build, image and file endpoints from its server, with short-lived tokens carrying the `manage` scope; the tokens it hands to the browser (dashboard) never have it.

`challenge.yml` supports the fields of `challenges.sh`: `name`, `category`, `description`, `value`, `type` (`standard`, `dynamic` with `minimum`/`decay`, `zync`...), `state`, `attempts`, `connection_info`, `attribution`, `flags` (strings or `{type, content, data}`), `files` (Galvanize looks for them in the challenge folder, then its `files/` subfolder, and reports missing files, paths leaving the folder and Git LFS pointers), `hints` (strings or `{title, content, cost, key, requirements}`), `tags`, `topics`, `requirements` (list, or `{prerequisites, anonymize}`, by name or ID) and `extra` (merged into the CTFd payload, except `id`, `type`, `name`, `category`, `state`, `requirements` and `next_id`). Differences with the script: files must stay inside the challenge folder; a field removed from `challenge.yml` (`attempts`, `connection_info`, `attribution`) is cleared by sync; static and regex flags are stripped, as CTFd's API does.

Requires a Galvanize version with the repository API (`/admin/repos`, `/admin/challenges` with `files`, `commit` and `build.buildable`, `/admin/build` taking `{path}` references).

### Tests

```bash
tests/run.sh                              # unit and integration tests, in the CTFd image (Docker), on SQLite
ZYNC_TEST_DB=mariadb tests/run.sh         # the same against a throwaway MariaDB
CTFD_IMAGE=ctfd/ctfd:3.8.9 tests/run.sh -k sync
E2E_CTFD=https://ctf.example E2E_PASSWORD=... python3 tests/e2e/e2e_polypwn.py   # end to end, against a running stack
```

## API

The plugin exposes the following endpoints:

### User Endpoints
- `GET /api/v1/deploy/url` - Get Instancer URL
- `POST /api/v1/deploy/token` - Get JWT token for a challenge deployment

### Admin Endpoints
- `GET /admin/zync_config` - Configuration page for Instancer URL and JWT secret
- `GET /admin/zync_dashboard` - Admin dashboard for monitoring and managing deployments
- `GET /admin/zync_token` - Generate admin JWT token for API access
- `POST /admin/zync_status_token` - Generate status token for specific challenge
- `GET /admin/zync_challenges` - Challenge manager
- `GET /admin/zync_challenges/api/overview` - Environment and repositories
- `GET /admin/zync_challenges/api/challenges?repo=<repo>` - Challenges of a repository, with their CTFd and image state
- `GET|POST /admin/zync_challenges/api/jobs` - List jobs / start one (`action`, `repo`, `keys`, `categories`, `force`, `dry_run`...)
- `GET /admin/zync_challenges/api/jobs/<id>?offset=<n>` - Job status and log from character `n`
- `POST /admin/zync_challenges/api/jobs/<id>/cancel` - Stop a job at its next step

The frontend communicates directly with the Galvanize Instancer at the configured URL.

## Configuration

### Environment Variables

- `ZYNC_DEPLOYER_URL` - URL to the Galvanize Instancer API (e.g., `https://instancer.example.com`)
- `ZYNC_JWT_SECRET` - Shared secret for JWT token signing/verification

These variables are loaded once at plugin startup and stored in the database. If both environment variables and database values exist, environment variables take precedence.

## Admin Dashboard

Access the dashboard at `/admin/zync_dashboard` to:
- Monitor error deployments with real-time status updates
- View all team deployments with filtering capabilities
- Retry failed deployments
- Terminate or delete problematic instances
- Reload challenge index from Galvanize Instancer
- Track deployment durations and statuses

The dashboard auto-refreshes error deployments every 10 seconds and team deployments every 60 seconds.

## Version

Current version: **0.4.0**

See [CHANGELOG.md](CHANGELOG.md) for detailed version history.
