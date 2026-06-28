"""Subscription soft-delete read-path filter (2026-06-28).

A session-level SQLAlchemy event listener that automatically appends
`Subscription.deleted_at IS NULL` to every SELECT involving the
Subscription mapper, so call sites don't need to remember the filter.

Opt-out: queries that need to see soft-deleted rows (the admin
cleanup endpoint's list view) pass:

    db.execute(stmt.execution_options(include_deleted=True))

What this catches:
- `select(Subscription).where(...)` and similar ORM patterns.
- Joins through Subscription (because `with_loader_criteria` applies
  to aliases as well).

What this does NOT catch:
- Raw `text(...)` queries — soft-delete leak; rewrite or filter manually.
- Cascade-table queries that hit `Order.subscription_id == ...`
  without joining Subscription — the cascade row is still returned;
  the read site must add an explicit join + filter where the soft
  delete must propagate. Hot read paths (farmer dashboard, dealer
  order list, pundit query feed) have been swept; future sites need
  to mirror the same pattern.
"""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.subscriptions.models import Subscription


def _add_subscription_soft_delete_filter(execute_state) -> None:
    """Append `deleted_at IS NULL` to every SELECT touching Subscription
    unless the caller explicitly opts in to including deleted rows."""
    if not execute_state.is_select:
        return
    if execute_state.is_column_load or execute_state.is_relationship_load:
        return
    if execute_state.execution_options.get("include_deleted", False):
        return
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            Subscription,
            Subscription.deleted_at.is_(None),
            include_aliases=True,
        )
    )


def install_subscription_soft_delete_listener() -> None:
    """Register the filter on the Session class. Idempotent — safe to
    call multiple times; SQLAlchemy de-duplicates listener
    registrations on the same target."""
    event.listen(Session, "do_orm_execute", _add_subscription_soft_delete_filter)
    event.listen(AsyncSession.sync_session_class, "do_orm_execute",
                 _add_subscription_soft_delete_filter)
