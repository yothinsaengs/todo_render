# Focus Board

FastAPI serves the legacy Focus Board dashboard with username/password authentication.
Tasks remain stored in Google Sheets.

## Routes

- `GET /` — authenticated dashboard
- `GET /login` — login page
- `POST /api` — authenticated dashboard API
- `GET /api/jobs/{id}` — authenticated background-write status
- `GET /ping` — unauthenticated health endpoint for uptime checks

Task creates and updates are queued and return a job ID immediately. The
dashboard polls the job endpoint until Google Sheets confirms success or failure. Job state
is kept in process memory for one hour, so deploys or process restarts discard unfinished jobs.

Removing a ticket is also a background write. It sets the Sheets status to `removed` and
hides the ticket from lists and metrics without deleting its row. **Fetch current** bypasses
the browser cache and reloads the current list and summary from Google Sheets.

Due times are stored with the `+07:00` Bangkok offset. The dashboard defaults new tasks to
24 hours from the current time. On first startup, existing v2 sheets receive a new `due_at`
column; existing date-only tasks remain valid.

## Google setup

Run this from Google Cloud Shell, replacing the project ID:

```sh
bash scripts/create_google_credentials.sh YOUR_GOOGLE_CLOUD_PROJECT_ID
```

The script enables the IAM and Sheets APIs, creates the `todo-render` service
account, and writes its JSON key to `secrets/google-service-account.json`. It does not
grant project-wide IAM roles.

Then:

1. Share the existing spreadsheet with the service account email as **Editor**.
2. Copy the spreadsheet ID from its URL.

The first authenticated API request validates the schema and creates missing `todos`,
`meta`, and `activity_log` sheets. Existing task data is used directly. Any previous
attachment sheet and Drive files are left untouched but are no longer used.

## Authentication configuration

Generate the password hash without putting the plaintext password in an environment variable:

```sh
python3 hash_password.py
```

Generate a session secret:

```sh
openssl rand -hex 32
```

Required environment variables:

- `APP_USERNAME` — login username
- `APP_PASSWORD_HASH` — complete `scrypt$...` output from `hash_password.py`
- `SESSION_SECRET` — random session-signing secret
- `SPREADSHEET_ID` — Google spreadsheet ID
- `GOOGLE_SERVICE_ACCOUNT_JSON` — complete service-account JSON, or
  `GOOGLE_SERVICE_ACCOUNT_FILE` — path to a mounted secret JSON file

Optional variables:

- `SESSION_HOURS` — session lifetime; defaults to `168`
- `COOKIE_SECURE` — use `true` on HTTPS/Render and `false` only for local HTTP
- `ACTIVITY_LOG_ENABLED` — append activity snapshots when `true`

## Run locally

Copy `.env.example` to `.env`, fill in its values, and save the downloaded Google key as
`secrets/google-service-account.json`. The application loads `.env` automatically. Both
`.env` and `secrets/` are excluded from Git and Docker build contexts.

Run directly:

```sh
python3 main.py
```

Or run with Docker, mounting the secret directory at runtime:

```sh
docker build -t focus-board .
docker run --rm --env-file .env \
  -v "$PWD/secrets:/app/secrets:ro" \
  -p 8000:8000 focus-board
```

Open `http://localhost:8000`. Keep `COOKIE_SECURE=false` locally.

## Deploy on Render

Create a Docker Web Service and add the variables above in the Render dashboard. Set
`COOKIE_SECURE=true`. For Google credentials, either paste the complete JSON into the
secret `GOOGLE_SERVICE_ACCOUNT_JSON` variable or mount it as a secret file and set
`GOOGLE_SERVICE_ACCOUNT_FILE` to that path. No custom build or start command is needed.

## Ping from Google Apps Script every 14 minutes

1. Create a project at `script.google.com`.
2. Copy `google-apps-script/Code.gs` into the Apps Script editor.
3. Add a script property named `DISCORD_WEBHOOK_URL` containing a Discord webhook URL.
4. Run `testDiscordNotification`, then run `setupPingTrigger` once.

The monitor calls the `/ping` endpoint and sends Discord notifications for network failures
or non-2xx responses. Apps Script trigger execution times are approximate.
