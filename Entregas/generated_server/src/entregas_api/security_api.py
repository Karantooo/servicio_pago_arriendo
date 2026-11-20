# coding: utf-8

import os

from fastapi import HTTPException, Request, Security, status
from fastapi.security.api_key import APIKeyHeader

from entregas_api.models.extra_models import TokenModel

api_key_header = APIKeyHeader(name="API-Key", auto_error=False)


def get_token_APIKeyHeader(
    request: Request,
    token_api_key_header: str | None = Security(api_key_header),
) -> TokenModel:
    admin_key = os.getenv("ADMIN_API_KEY")
    readonly_key = os.getenv("CLIENT_API_KEY")
    if not token_api_key_header or token_api_key_header not in {
        admin_key,
        readonly_key,
    }:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "UNAUTHORIZED",
                "message": "API Key inválida o faltante.",
            },
        )

    is_write = request.method in {"POST", "PUT", "PATCH", "DELETE"}
    if is_write and token_api_key_header != admin_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": "Se requiere una API Key con rol admin.",
            },
        )
    return TokenModel(sub="admin" if is_write else "readonly")
