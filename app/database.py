from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
from app.config import settings

# NullPool: every checkout opens a fresh connection, every check-in
# closes it. No long-lived pooled connections, so no asyncpg event-loop
# mismatch in celery workers — where each task wraps its body in
# `asyncio.run(...)` and the loop a connection was bound to is gone by
# the next task.
#
# For the API server (uvicorn) this means a fresh DB handshake per
# request (~1-2 ms). At V1 load that's acceptable; can revisit
# post-launch if profile shows the connection cost as a bottleneck.
engine = create_async_engine(
    settings.database_url, echo=False, poolclass=NullPool,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
