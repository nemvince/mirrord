import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import load_config
from app.plugins.registry import register_builtins
from app.sync_engine import SyncEngine
from app.web.routes import router, set_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mirrord")


def datetime_filter(ts: float, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.fromtimestamp(ts).strftime(fmt)


def create_app() -> FastAPI:
    config_path = os.environ.get("MIRRORD_CONFIG", "config.yaml")

    register_builtins()

    config = load_config(config_path)
    engine = SyncEngine()
    set_engine(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine.start()
        yield
        engine.stop()

    # Resolve display version: release tag > git commit > fallback
    app_version = os.environ.get("MIRRORD_VERSION", "dev")
    git_commit = os.environ.get("MIRRORD_GIT_COMMIT", "")
    if app_version and app_version != "dev":
        display_version = app_version
    elif git_commit:
        display_version = git_commit[:7]
    else:
        display_version = "dev"

    app = FastAPI(title="mirrord", version=display_version, lifespan=lifespan)

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    templates_dir = os.path.join(os.path.dirname(__file__), "templates")
    templates = Jinja2Templates(directory=templates_dir)
    templates.env.filters["datetime"] = datetime_filter
    templates.env.globals["version"] = display_version
    app.state.templates = templates

    app.include_router(router)

    # Stash for __main__ access
    app.state.config = config
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=app.state.config.server.host,
        port=app.state.config.server.port,
        reload=False,
    )
