"""CIMAGE AI Media Platform - API (V0.1: Video -> Knowledge Blocks)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from apps.api.admin_routes import router as admin_router
from apps.api.composer_routes import router as composer_router
from apps.api.config import REPO_ROOT, get_settings
from apps.api.content_routes import auto_draft_job, router as content_router
from apps.api.publish_routes import router as publish_router
from apps.api.ingest import submit_source
from apps.api.jobs import JobRunner, JsonJobStore, embed_job_blocks
from apps.api.routes import router
from services.block_engine import sources
from services.ingestion.watcher import FolderWatcher
from services.ai_gateway import build_provider
from services.ai_gateway.embeddings import build_embedder
from services.block_engine.engine import BlockEngine
from services.block_engine.proxy import ProxyPolicy
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
    engine = BlockEngine(provider, prompt_version=settings.prompt_version, institution_context=settings.institution_context, known_people_file=settings.known_people_file, people_pass_version=settings.people_pass_version or None,
                         proxy=ProxyPolicy(enabled=settings.proxy_enabled, min_mb=settings.proxy_min_mb, max_height=settings.proxy_max_height, max_bitrate_kbps=settings.proxy_max_bitrate_kbps,
                                           crf=settings.proxy_crf, keep=settings.proxy_keep, timeout_seconds=settings.proxy_timeout_seconds), proxies_dir=settings.proxies_dir)
    store = _build_store(settings)
    loaded = store.load()
    embedder = build_embedder(settings)
    content = None
    images = None
    if getattr(store, "supports_vectors", False):
        from apps.api.content_store import ContentStore
        from services.media_library.store import ImageStore

        content = ContentStore(store.pool)
        images = ImageStore(store.pool, settings.data_dir / "images")
    runner = JobRunner(store, settings.worker_threads, embedder=embedder, content_store=content,
                       on_content_candidate=lambda job_id, n: auto_draft_job(app.state, job_id))
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
    app.state.images = images
    app.state.retriever = retriever
    app.state.blog_agent = blog_agent
    app.state.watcher = _build_watcher(app, settings)
    app.state.watcher.start()
    log.info("provider=%s model=%s embedder=%s/%s store=%s jobs_loaded=%d data_dir=%s watcher=%s auto_draft=%s", provider.name, provider.model,
             embedder.name, embedder.model, store.kind, loaded, settings.data_dir, "on" if settings.watcher_enabled else "off", settings.auto_draft)
    try:
        yield
    finally:
        app.state.watcher.stop()
        runner.shutdown()
        store.close()


def _build_watcher(app: FastAPI, settings) -> FolderWatcher:
    """The folder watcher submits through the same path as /analyze and sweeps embeddings between scans."""
    state = app.state

    def submit(path: Path) -> tuple[str, bool]:
        src = sources.from_path(str(path), settings.allowed_roots, settings.stable_seconds)
        sub = submit_source(state, src, actor="watcher")
        return sub.job.id, sub.deduplicated

    def active_jobs() -> int:
        return sum(1 for j in state.store.list() if j.is_active)

    sweep = (lambda: embed_job_blocks(state.store, state.embedder, None)) if getattr(state.store, "supports_vectors", False) else None
    return FolderWatcher(settings.watch_roots_resolved, submit=submit, active_jobs=active_jobs, sweep_embeddings=sweep,
                         interval_seconds=settings.watcher_interval_seconds, stable_seconds=settings.watcher_stable_seconds,
                         max_active_jobs=settings.watcher_max_active_jobs, state_file=settings.watcher_state_file, enabled=settings.watcher_enabled)


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
    app.include_router(composer_router)  # Video Composer (COMPOSER_ENABLED gates it)
    app.include_router(content_router)
    app.include_router(publish_router)
    app.include_router(admin_router)

    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok"}

    # One UI: everything lives in /admin (web/admin.html + web/admin/*.js modules). The old stand-alone pages redirect
    # into their sections so bookmarks keep working; the files themselves are kept under web/_legacy and are not served.
    @app.get("/admin", include_in_schema=False)
    def admin_page() -> FileResponse:
        return FileResponse(WEB_DIR / "admin.html")

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/admin#overview", status_code=302)

    @app.get("/content", include_in_schema=False)
    def content_page() -> RedirectResponse:
        return RedirectResponse("/admin#drafts", status_code=302)

    app.mount("/static", StaticFiles(directory=WEB_DIR / "admin"), name="static")
    return app


app = create_app()
