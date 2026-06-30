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
