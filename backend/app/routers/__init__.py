"""HTTP routers package (stage 5 split).

Active route implementations still live primarily in main.py after the bulk
cut of retired handlers. New endpoints should be added under this package and
registered via include_routers().
"""
from fastapi import FastAPI


def include_routers(app: FastAPI) -> None:
    from app.routers import core_portfolio, meta, trend_portfolio
    app.include_router(meta.router)
    app.include_router(trend_portfolio.router)
    app.include_router(core_portfolio.router)
