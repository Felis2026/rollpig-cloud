from __future__ import annotations

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import ApiKeyIdentity, settings

security = HTTPBearer(auto_error=False)


def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> ApiKeyIdentity:
    """验证 Bearer Token，并仅把不含密钥的身份写入请求上下文。"""

    if not settings.api_keys:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="服务端未配置 Token")
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Bearer Token")
    identity = settings.api_keys.get(credentials.credentials)
    if identity is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效 Token")
    request.state.api_key_identity = identity
    return identity
