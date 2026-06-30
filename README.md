# FastAPI Hello World service

Endpoints:

- `GET /` returns `{"message":"Hello World"}`
- `GET /ping` returns `{"status":"ok"}` for cron or uptime checks

## Run with Docker

```sh
docker build -t fastapi-hello .
docker run --rm -p 8000:8000 fastapi-hello
```

Then open `http://localhost:8000/` or ping:

```sh
curl http://localhost:8000/ping
```

## Deploy on Render

Create a **Web Service**, connect this repository, and select the **Docker** runtime.
No custom build or start command is required. The service reads Render's `PORT`
environment variable automatically.

## Ping from Google Apps Script every 14 minutes

1. Create a project at `script.google.com`.
2. Copy `google-apps-script/Code.gs` into the Apps Script editor.
3. Select `setupPingTrigger` and click **Run** once.
4. Approve the requested URL Fetch and trigger permissions.

The script calls `https://todo-render-rxto.onrender.com/ping` and schedules its
next run after 14 minutes. Apps Script trigger execution times are approximate.
Run `stopPingTrigger` manually to stop it.
