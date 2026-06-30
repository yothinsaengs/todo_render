import os

import uvicorn
from fastapi import FastAPI

app = FastAPI(title="Hello World Service")


@app.get("/")
async def hello_world() -> dict[str, str]:
    return {"message": "Hello World"}


@app.get("/ping")
async def ping() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
    )
