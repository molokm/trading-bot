"""HTTP routers package (stage 5 split).

Active route implementations still live primarily in main.py after the bulk
cut of retired handlers. New endpoints should be added under this package and
registered via include_routers().
"""
from fastapi import FastAPI


def include_routers(app: FastAPI) -> None:
    from app.routers import meta
    app.include_router(meta.router)
