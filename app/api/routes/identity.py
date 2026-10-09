"""SlideForge service: api/routes/identity. Darwin's `/api/userinfo` (an OIDC userinfo shim, C8).

Same path, methods, request and response as `netlify/functions/userinfo.ts` (contract:
`tests/contract/data/api-userinfo.json`); the decoding and its quirks are in
app/core/darwin/userinfo.py. It does NOT use `require_user` and does not verify the token: Supabase
Auth's `custom:auxi` provider calls it with the token the identity provider just issued. Every method
is answered the same way. Kept only while the identity flow needs it (plan D25).
"""

from __future__ import annotations

import json

from fastapi import Request
from fastapi.responses import Response

from app.api.legacy import darwin_route, json_response, legacy_router
from app.core.darwin.userinfo import answer

router = legacy_router(prefix="/api", tags=["darwin-identity"])

#: What `new Response(JSON.stringify(...))` sends when no content-type is set (the Fetch default).
ERROR_CONTENT_TYPE = "text/plain;charset=UTF-8"


@darwin_route(router, "/userinfo", methods=("GET",), summary="OIDC userinfo for the custom:auxi provider (unverified)")
async def userinfo(request: Request) -> Response:
    result = answer(request.headers.get("authorization"))
    if result.status == 200:
        return json_response(result.body)
    content = json.dumps(result.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(content=content, status_code=result.status, media_type=ERROR_CONTENT_TYPE)
