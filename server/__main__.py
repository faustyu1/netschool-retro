import os

import uvicorn

uvicorn.run(
    "server.main:app",
    host=os.environ.get("HOST", "127.0.0.1"),
    port=int(os.environ.get("PORT", "8000")),
    proxy_headers=True,
)
