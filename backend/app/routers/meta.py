"""Meta routes for the AI-only product."""
from fastapi import APIRouter

from app.services.route_policy import PUBLIC, policy

router = APIRouter(tags=["meta"])


@router.get("/api/meta/product")
@policy(PUBLIC)
async def product_meta():
    return {
        "product": "COPIX",
        "mode": "AI_ONLY",
        "bots": ["AI Discretionary 1H"],
        "legacy_bots": "retired",
        "stage": 5,
    }
