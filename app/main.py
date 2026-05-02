from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.modules.auth.router import router as auth_router
from app.modules.platform.router import router as platform_router
from app.modules.clients.router import router as clients_router
from app.modules.sync.router import router as sync_router
from app.modules.advisory.router import router as advisory_router
from app.modules.subscriptions.router import router as subscriptions_router
from app.modules.orders.router import router as orders_router
from app.modules.farmpundit.router import router as farmpundit_router
from app.modules.farmpundit.diagnosis_router import router as diagnosis_router
from app.modules.qr.router import router as qr_router
from app.modules.reports.router import router as reports_router
from app.modules.seed_mgmt.router import router as seed_mgmt_router
from app.modules.media.router import router as media_router

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
app.include_router(advisory_router)
app.include_router(subscriptions_router)
app.include_router(orders_router)
app.include_router(farmpundit_router)
app.include_router(diagnosis_router)
app.include_router(qr_router)
app.include_router(reports_router)
app.include_router(seed_mgmt_router)
app.include_router(media_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "rootstalk-api"}
