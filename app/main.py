__version__ = '0.0.0'  # replaced during build

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from acme.exceptions import ACMEException
import db
import db.migrations
import acme
import ca
import web
from config import settings

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    await db.migrations.run()
    if settings.ca.enabled:
        await ca.init()
    await acme.start_cronjobs()
    yield
    await db.disconnect()


app = FastAPI(lifespan=lifespan, version=__version__, redoc_url=None, docs_url=None,
    title=settings.web.app_title, description=settings.web.app_description)
app.add_middleware(web.middleware.SecurityHeadersMiddleware, content_security_policy={
    '/acme/': "base-uri 'self'; default-src 'none';",
    '/endpoints': "base-uri 'self'; default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-src 'none'; img-src 'self' data:;",
    '/': "base-uri 'self'; default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-src 'none'; img-src 'self' data:;"
})

if settings.web.enabled:
    @app.get("/endpoints", tags=['web'])
    async def swagger_ui_html():
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title=app.title,
            swagger_favicon_url="favicon.png",
            swagger_css_url='libs/swagger-ui.css',
            swagger_js_url='libs/swagger-ui-bundle.js'
        )

# custom exception handler for acme specific response format
@app.exception_handler(RequestValidationError)
async def acme_validation_exception_handler(request: Request, exc: RequestValidationError):
    if request.url.path.startswith('/acme/'):
        exc = ACMEException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, type='malformed', detail=exc.json())
    return await http_exception_handler(request, exc)

@app.exception_handler(HTTPException)
async def acme_http_exception_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith('/acme/'):
        exc = ACMEException(status_code=exc.status_code, type='serverInternal', detail=exc.detail)
    return await http_exception_handler(request, exc)


app.include_router(acme.router)
app.include_router(ca.router)

if settings.web.enabled:
    app.include_router(web.router)

    if Path('/app/web/www').exists():
        app.mount('/', StaticFiles(directory='/app/web/www'), name='static')