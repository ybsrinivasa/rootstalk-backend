from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.modules.auth.router import router as auth_router
from app.modules.platform.router import router as platform_router
from app.modules.clients.router import router as clients_router
from app.modules.sync.router import router as sync_router

app = FastAPI(
    title="RootsTalk API",
    version="1.0.0",
    description="Agricultural business infrastructure — RootsTalk backend API",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(platform_router)
app.include_router(clients_router)
app.include_router(sync_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "rootstalk-api"}
