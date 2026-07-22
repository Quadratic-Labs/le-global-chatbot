from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.clients.opensearch import get_opensearch_client
from app.clients.redis import get_redis_client


router = APIRouter(tags=["Health"])


@router.get("/health")
def health():
    dependencies = {
        "opensearch": "unavailable",
        "redis": "unavailable",
    }

    try:
        if get_opensearch_client().ping():
            dependencies["opensearch"] = "ok"
    except Exception:
        pass

    try:
        if get_redis_client().ping():
            dependencies["redis"] = "ok"
    except Exception:
        pass

    healthy = all(
        status == "ok"
        for status in dependencies.values()
    )

    response = {
        "status": "ok" if healthy else "degraded",
        "service": "le-global-backend",
        "dependencies": dependencies,
    }

    if not healthy:
        return JSONResponse(
            status_code=503,
            content=response,
        )

    return response
