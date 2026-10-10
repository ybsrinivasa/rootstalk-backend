"""Celery tasks for Custom Parameter / Variable auto-translation.

Fired by:
  * `create_parameter` + `create_variable` CA handlers on first save.
  * The CA-portal "Regenerate" button via the regenerate endpoints
    in `advisory/router.py`.

Not fired on rename — see Option-2 policy note in
`services/pv_translation.py` module docstring. The rename handler
downgrades existing rows to PENDING instead; SE reviews + clicks
Regenerate per row.
"""
import asyncio
import logging

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.services.pv_translation import (
    translate_parameter, translate_variable,
)

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.translate_pv.translate_parameter_name")
def translate_parameter_name_task(parameter_id: str) -> dict:
    return asyncio.run(_run_parameter(parameter_id))


@celery_app.task(name="app.tasks.translate_pv.translate_variable_name")
def translate_variable_name_task(variable_id: str) -> dict:
    return asyncio.run(_run_variable(variable_id))


async def _run_parameter(parameter_id: str) -> dict:
    async with AsyncSessionLocal() as db:
        try:
            written = await translate_parameter(db, parameter_id)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "translate_parameter_name_task failed for %s: %s",
                parameter_id, e,
            )
            return {"error": str(e)}
        if written is None:
            return {"skipped": True}
        return {"written": written}


async def _run_variable(variable_id: str) -> dict:
    async with AsyncSessionLocal() as db:
        try:
            written = await translate_variable(db, variable_id)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "translate_variable_name_task failed for %s: %s",
                variable_id, e,
            )
            return {"error": str(e)}
        if written is None:
            return {"skipped": True}
        return {"written": written}
