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
