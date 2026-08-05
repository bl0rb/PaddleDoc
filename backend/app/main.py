import logging

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router_admin as auth_admin_router
from app.api.auth import router_authenticated as auth_authenticated_router
from app.api.auth import router_public as auth_public_router
from app.api.deps import get_current_user, origin_guard
from app.api.import_routes import router as import_router
from app.api.routes import router
from app.core.config import settings
from app.schemas.jobs import HealthResponse
from app.services.storage import ensure_storage_dirs

app = FastAPI(title=settings.app_name)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


@app.on_event('startup')
def startup() -> None:
    ensure_storage_dirs()


# Public, unauthenticated router: liveness/readiness probes have no session
# to present, so /health lives outside the auth system entirely rather than
# under /auth (which is reserved for the actual public auth surface below).
public_router = APIRouter(prefix='/api/v1')


@public_router.get('/health', response_model=HealthResponse)
def healthcheck() -> HealthResponse:
    return HealthResponse(status='healthy')


app.include_router(public_router)

# Public auth surface: setup, login, provider discovery, OIDC redirect --
# these must stay reachable before a session exists. origin_guard is applied
# at the router level (see app/api/auth.py) so the CSRF check still covers
# these state-changing POSTs even though get_current_user does not.
app.include_router(auth_public_router)
app.include_router(auth_authenticated_router)
app.include_router(auth_admin_router)

# Secure-by-default: every other /api/v1 route (jobs, folders, collections,
# paddle settings, ...) now requires a valid session. Step 3 layers
# per-row visibility scoping (owner/team) on top of this; this is just the
# authentication gate itself.
app.include_router(router, dependencies=[Depends(get_current_user), Depends(origin_guard)])

# Confluence import surface (/api/v1/import/...): same session + CSRF gate as
# the main router; the module itself adds the IMPORT_ENABLED kill-switch.
app.include_router(import_router, dependencies=[Depends(get_current_user), Depends(origin_guard)])
