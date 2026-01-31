# Zync CTFd Plugin

This plugin for CTFd integrates with the [Galvanize Instancer](https://github.com/28Pollux28/galvanize) to allow competing teams/users to deploy challenge instances. It adds a challenge type "zync" that works with the Galvanize Instancer API for on-demand challenge deployments.

## Features

* Deploy challenge instances via Galvanize Instancer
* Time-limited instances with countdown display
* Extend instance duration (when supported by the Instancer)
* Terminate instances
* Admin config for Instancer URL and JWT secret
* Deployment lifecycle fully managed by Galvanize Instancer (admin panel to be designed)

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

4. Navigate to **Plugins** → **Zync Config** (`/admin/zync_config`). Configure:
   - **Instancer URL**: The full URL to your Galvanize Instancer (e.g., `https://instancer.example.com/`)
   - **JWT Secret**: The secret used to sign JWT tokens for the Instancer

5. Create challenges with the **zync** challenge type.

## API

The plugin exposes the following endpoints:
- `GET /api/v1/deploy/url` - Get Instancer URL
- `POST /api/v1/deploy/token` - Get JWT token for a challenge

The frontend communicates directly with the Galvanize Instancer at the configured URL.
