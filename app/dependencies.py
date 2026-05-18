from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.modules.auth.service import decode_token, get_user_by_id
from app.modules.platform.models import User, RoleType, StatusEnum

bearer = HTTPBearer()


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    payload = decode_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    user = await get_user_by_id(db, payload["sub"])
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    # Single-device enforcement: if token has a jti and it doesn't match the user's
    # current_session_id, this token was issued for a previous device. Older tokens
    # without jti are still allowed (graceful migration — they expire naturally).
    token_jti = payload.get("jti")
    if token_jti and user.current_session_id and token_jti != user.current_session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session ended — signed in on another device",
        )

    # Tenant isolation (2026-05-18): a portal-issued token carries a
    # `client_id` claim. Refuse if the path's {client_id} doesn't
    # match — this is the architectural gate that makes
    # `/client/{cid}/...` endpoints tenant-safe regardless of any
    # missing per-endpoint role checks. Tokens issued WITHOUT a
    # client_id claim (SA / CM / PWA logins) are allowed through here;
    # per-endpoint guards (_assert_cm_can_edit_client, etc.) still
    # apply downstream.
    token_client_id = payload.get("client_id")
    path_client_id = request.path_params.get("client_id")
    if token_client_id and path_client_id and token_client_id != path_client_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "cross_client_forbidden",
                "message": (
                    "This session is bound to a different company. "
                    "Sign out and sign back in to the company you need to access."
                ),
            },
        )
    # Stash for downstream introspection (e.g. /auth/me needs to
    # surface the bound client to the frontend).
    request.state.token_client_id = token_client_id
    request.state.token_client_short_name = payload.get("client_short_name")
    return user


def require_roles(*roles: RoleType):
    async def _check(current_user: User = Depends(get_current_user)) -> User:
        active_roles = {r.role_type for r in current_user.roles if r.status == StatusEnum.ACTIVE}
        if not active_roles.intersection(set(roles)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return current_user
    return _check


require_sa = require_roles(RoleType.CONTENT_MANAGER)  # placeholder — SA is checked by email in service
require_cm = require_roles(RoleType.CONTENT_MANAGER)
