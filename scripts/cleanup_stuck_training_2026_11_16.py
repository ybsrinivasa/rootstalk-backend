"""One-shot cleanup for the four stuck training clients on staging
(2026-11-16).

Context: Before the per-crop expiry rewrite (migration t2q7l5n18s4j +
training_expiry.py rewrite), the hourly sweep was crashing on a
seed_orders_full FK violation, which stranded four training clients
in training_status=ACTIVE for 53-102 days past their old
training_ends_at. The schema migration drops those two columns, so
the rows are now just "forever-open" training sandboxes; this script
cascade-closes them in one go using the (now bug-fixed) helper.

Run on staging ONLY. Prod has zero training clients, so the script
no-ops there, but it's gated by client-id-list for safety.

Usage (on staging):
    cd ~/apps/rootstalk-backend && source venv/bin/activate
    python -m scripts.cleanup_stuck_training_2026_11_16
"""
import asyncio
import logging

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.modules.clients.models import Client
from app.tasks.training_expiry import _cascade_delete_training_child


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


STUCK_TRAINING_CLIENT_IDS = [
    "3e4a70a4-e052-40f0-ab4f-fd192583006e",  # TR09718075 — Kalyan RamaRajya
    "fe953b01-29fc-46fa-a361-ab8b660167aa",  # TRFE44097E — YSR Testing
    "a08be072-e4e7-45e7-9654-dde129a25c69",  # TRE863A298 — Demo 1
    "53658927-dbc8-4277-bfd4-16fb494bce0a",  # TR2B031595 — DEMO 2
]


async def _run() -> None:
    async with AsyncSessionLocal() as db:
        for cid in STUCK_TRAINING_CLIENT_IDS:
            child = (await db.execute(
                select(Client).where(Client.id == cid)
            )).scalar_one_or_none()
            if child is None:
                logger.info(f"{cid}: not present, skipping")
                continue
            if not child.is_training:
                logger.warning(
                    f"{cid}: not a training client ({child.display_name!r}), "
                    f"refusing to cascade"
                )
                continue
            logger.info(
                f"{cid}: cascading {child.display_name!r} "
                f"(parent={child.parent_client_id})"
            )
            try:
                await _cascade_delete_training_child(db, child)
                await db.commit()
                logger.info(f"{cid}: done")
            except Exception as exc:
                await db.rollback()
                logger.error(f"{cid}: cascade FAILED — {exc}")


if __name__ == "__main__":
    asyncio.run(_run())
