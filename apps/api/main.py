"""CIMAGE AI Media Platform - API (V0.1: Video -> Knowledge Blocks)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from apps.api.config import REPO_ROOT, get_settings
from apps.api.jobs import JobRunner, JsonJobStore
from apps.api.content_routes import router as content_router
from apps.api.routes import router
from services.ai_gateway import build_provider
from services.ai_gateway.embeddings import build_embedder
from services.block_engine.engine import BlockEngine
from services.retrieval.retriever import Retriever
from agents.blog_agent.agent import BlogAgent

WEB_DIR = REPO_ROOT / "web"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cimage.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    provider = build_provider(settings)
    engine = BlockEngine(provider, prompt_version=settings.prompt_version, institution_context=settings.institution_context, known_people_file=settings.known_people_file, people_pass_version=settings.people_pass_version or None)
    store = _build_store(settings)
    loaded = store.load()
    embedder = build_embedder(settings)
    content = None
    if getattr(store, "supports_vectors", False):
        from apps.api.content_store import ContentStore

        content = ContentStore(store.pool)
    runner = JobRunner(store, settings.worker_threads, embedder=embedder, content_store=content)
    retriever = Retriever(store, embedder)
    blog_agent = BlogAgent(
        provider, retriever, prompt_version=settings.blog_prompt_version, institution_context=settings.institution_context,
        model=settings.blog_model or None, thinking_level=settings.blog_thinking_level or None,
    )

    app.state.settings = settings
    app.state.engine = engine
    app.state.store = store
    app.state.runner = runner
    app.state.embedder = embedder
    app.state.content = content
    app.state.retriever = retriever
    app.state.blog_agent = blog_agent
    log.info("provider=%s model=%s embedder=%s/%s store=%s jobs_loaded=%d data_dir=%s", provider.name, provider.model, embedder.name, embedder.model, store.kind, loaded, settings.data_dir)
    try:
        yield
    finally:
        runner.shutdown()
        store.close()


def _build_store(settings):
    """PostgreSQL when DATABASE_URL is set and reachable, otherwise JSON files (with a warning)."""
    if settings.database_url:
        from apps.api.db import make_pool, ping, run_migrations
        from apps.api.pg_store import PostgresJobStore

        if ping(settings.database_url):
            pool = make_pool(settings.database_url)
            if settings.auto_migrate:
                applied = run_migrations(pool)
                if applied:
                    log.info("applied migrations: %s", applied)
            return PostgresJobStore(pool)
        log.warning("DATABASE_URL set but database unreachable - falling back to JSON-file store")
    return JsonJobStore(settings.jobs_dir, settings.analyses_dir)


def create_app() -> FastAPI:
    app = FastAPI(title="CIMAGE AI Media Platform", version=get_settings().app_version, lifespan=lifespan)
    app.include_router(router)
    app.include_router(content_router)

    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/content", include_in_schema=False)
    def content_page() -> FileResponse:
        return FileResponse(WEB_DIR / "content.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


app = create_app()
