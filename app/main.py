from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import admin_router, auth_router, cases_router
from app.sheet_store import reset_request_cache

app = FastAPI(title="Visa Lodgement Date Calculator API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def sheet_cache_per_request(request: Request, call_next):
    # See sheet_store.py -- avoids re-fetching the same Sheets table
    # multiple times within one request (e.g. once per row in a list).
    reset_request_cache()
    return await call_next(request)

app.include_router(auth_router.router)
app.include_router(cases_router.router)
app.include_router(admin_router.router)


@app.get("/health")
def health():
    return {"status": "ok"}
