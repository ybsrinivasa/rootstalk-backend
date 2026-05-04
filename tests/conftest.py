"""Test infrastructure — Postgres container + async session fixtures.

Provides DB-backed integration tests for any test that asks for the `db`
fixture. A single Postgres container is reused for the whole pytest session;
each test gets a fresh transaction that is rolled back at teardown so tests
stay isolated without paying full schema-create cost per test.

Skip rule: if Docker is not running on the host, DB-backed tests are skipped
gracefully so pure-function tests still run in CI environments without
container support.

See: per_subscription_versioning.md (Phase 3 deliverables).
"""
from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio


# ── Docker availability check ────────────────────────────────────────────────

def _docker_available() -> bool:
    """Detect a usable Docker daemon. We check this once at collect time so
    the rest of the module can decide whether to skip DB fixtures."""
    try:
        import docker  # type: ignore
    except ImportError:
        return False
    try:
        docker.from_env().ping()
        return True
    except Exception:
        return False


DOCKER_AVAILABLE = _docker_available()

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE,
    reason="Docker daemon unavailable — DB-backed integration tests skipped",
)


# ── Postgres container (session-scoped) ──────────────────────────────────────

@pytest.fixture(scope="session")
def _postgres_container():
    """Start one Postgres container for the whole test session and tear it
    down at the end. Skipped automatically when Docker isn't available."""
    if not DOCKER_AVAILABLE:
        pytest.skip("Docker daemon unavailable")

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def _async_db_url(_postgres_container) -> str:
    """testcontainers gives a sync psycopg URL; rewrite to asyncpg."""
    sync_url = _postgres_container.get_connection_url()
    # Examples we may receive:
    #   postgresql+psycopg2://test:test@localhost:32768/test
    #   postgresql://test:test@localhost:32768/test
    return (
        sync_url
        .replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        .replace("postgresql://", "postgresql+asyncpg://")
    )


# ── Schema setup (session-scoped) ────────────────────────────────────────────

@pytest_asyncio.fixture(scope="session")
async def _engine(_async_db_url):
    """Create a session-scoped async engine and run `Base.metadata.create_all`
    so every model registered via app.* is materialised in the test DB.
    """
    # Point the global settings at the test DB before any app module reads
    # `settings.database_url`. We import here, after the container is up.
    os.environ["DATABASE_URL"] = _async_db_url
    os.environ["DATABASE_URL_SYNC"] = _async_db_url.replace("+asyncpg", "")

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    from app.database import Base

    # Import every model module so SQLAlchemy registers them on Base.metadata.
    # Mirrors the imports in alembic/env.py — kept in lockstep.
    import app.modules.platform.models  # noqa: F401
    import app.modules.auth.models  # noqa: F401
    import app.modules.clients.models  # noqa: F401
    import app.modules.sync.models  # noqa: F401
    import app.modules.orders.models  # noqa: F401
    import app.modules.subscriptions.models  # noqa: F401
    import app.modules.subscriptions.snapshot_models  # noqa: F401
    import app.modules.subscriptions.config_error_models  # noqa: F401
    import app.modules.advisory.models  # noqa: F401
    import app.modules.qr.models  # noqa: F401
    import app.modules.farmpundit.models  # noqa: F401

    # NullPool: every connection is freshly opened. Avoids cross-event-loop
    # bleed between session-scoped engine and function-scoped sessions when
    # pytest-asyncio rotates loops.
    engine = create_async_engine(_async_db_url, future=True, poolclass=NullPool)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


# ── Per-test session (TRUNCATE-at-teardown isolation) ───────────────────────
#
# We tried the SAVEPOINT-restart pattern first; asyncpg's single-active-op
# constraint on a connection makes it brittle when production code calls
# session.commit() inside the route. The TRUNCATE-at-teardown approach lets
# production code commit freely and is clean enough at the table sizes we
# use in tests.

@pytest_asyncio.fixture
async def db(_engine):
    """Yield an AsyncSession bound to the session-scoped test DB. Production
    code is free to call `await db.commit()`; at teardown we TRUNCATE every
    table so the next test sees an empty DB.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.database import Base

    Session = async_sessionmaker(_engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        await session.close()
        # Truncate every mapped table. Order doesn't matter under CASCADE.
        table_names = ", ".join(
            f'"{t.name}"' for t in Base.metadata.sorted_tables
        )
        async with _engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))
