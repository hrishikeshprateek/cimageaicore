"""Content layer: opportunities queue, retriever, blog agent (mock provider), drafts. Needs Postgres (skips otherwise)."""
import pytest

from apps.api.jobs import JobState, embed_job_blocks
from services.ai_gateway.embeddings import MockEmbedder
from services.ai_gateway.mock import MockProvider
from services.block_engine.engine import BlockEngine
from services.block_engine.sources import from_upload
from tests.test_pg_store import store, test_db_url  # noqa: F401 - fixtures


@pytest.fixture
def content(store):  # noqa: F811
    from apps.api.content_store import ContentStore

    return ContentStore(store.pool)


def _indexed_job(store, tiny_video):  # noqa: F811
    job = store.create(from_upload(tiny_video).info, "mock", "mock-v1")
    engine = BlockEngine(MockProvider(delay_seconds=0))
    result = engine.analyze(job.id, from_upload(tiny_video), lambda s, d: store.transition(job.id, s, d))
    store.complete(job.id, result)
    store.transition(job.id, JobState.BLOCKS_COMPLETE)
    embed_job_blocks(store, MockEmbedder(768), job.id)
    store.transition(job.id, JobState.INDEXED)
    return job


def test_opportunities_are_queued_from_blocks(store, content, tiny_video):  # noqa: F811
    job = _indexed_job(store, tiny_video)
    assert content.create_opportunities_from_job(job.id) == 1
    assert content.create_opportunities_from_job(job.id) == 0  # idempotent
    opps = content.list_opportunities("new")
    assert len(opps) == 1 and opps[0].source == "ai" and opps[0].job_id == job.id and opps[0].source_name
    assert content.set_opportunity_status(opps[0].id, "accepted").status == "accepted"
    manual = content.create_manual_opportunity("Write about robotics", None, ["blog"], "parents", job.id)
    assert manual.source == "manual" and manual.status == "accepted" and manual.media_id == job.media_id


def test_retriever_builds_bounded_evidence_pack(store, tiny_video):  # noqa: F811
    from services.retrieval.retriever import Retriever

    job = _indexed_job(store, tiny_video)
    r = Retriever(store, MockEmbedder(768))
    pack = r.evidence_for(["placeholder quote"], job_id=job.id)
    types = [b.block_type for b in pack.blocks]
    assert "content_opportunity" not in types and types[0] == "video" and "quote" in types
    assert pack.sources and pack.sources[0]["blocks"] == len(pack.blocks)
    assert "[id=" in pack.render()
    small = r.evidence_for(["placeholder"], job_id=job.id, max_blocks=2)
    assert len(small.blocks) == 2 and small.truncated


def test_blog_agent_grounds_and_flags(store, tiny_video):  # noqa: F811
    from agents.blog_agent.agent import BlogAgent
    from services.retrieval.retriever import Retriever

    job = _indexed_job(store, tiny_video)
    agent = BlogAgent(MockProvider(delay_seconds=0), Retriever(store, MockEmbedder(768)), institution_context="Test College")
    res = agent.draft("Industry interaction at the college", job_id=job.id, target_words=1000)
    ids = res.evidence.ids()
    assert res.draft.title.startswith("[MOCK]") and all(c.block_id in ids for c in res.draft.citations)
    assert any("pointed outside the evidence" in w for w in res.warnings)   # the mock's bogus citation was dropped
    assert any("length" in w for w in res.warnings)                         # mock body is far shorter than 1000 words
    assert res.draft.hero_block_id in ids


def test_blog_agent_refuses_without_evidence(store):  # noqa: F811
    from agents.blog_agent.agent import BlogAgent, DraftValidationError
    from services.retrieval.retriever import Retriever

    agent = BlogAgent(MockProvider(delay_seconds=0), Retriever(store, MockEmbedder(768)))
    with pytest.raises(DraftValidationError, match="no evidence"):
        agent.draft("zzz nothing indexed about this")


def test_draft_lifecycle_versions_and_review(store, content, tiny_video):  # noqa: F811
    job = _indexed_job(store, tiny_video)
    content.create_opportunities_from_job(job.id)
    opp = content.list_opportunities()[0]
    d = content.create_draft(opp.title, opp.id)
    assert d.status == "generating"
    d = content.finish_draft(d.id, title="T", slug="t", body_markdown="## A\n\nbody", seo={}, social={}, citations=[], evidence={},
                             hero_block_id=None, model="m", prompt_version="blog_v1", usage={}, warnings=["w"])
    assert d.status == "new" and content.get_opportunity(opp.id).status == "drafted"
    e = content.update_draft_text(d.id, title="Edited", body_markdown=None)
    assert e.version == 2 and e.title == "Edited" and e.body_markdown == "## A\n\nbody" and e.status == "in_review"
    versions = content.draft_versions(d.id)
    assert [v["edited_by"] for v in versions] == ["agent", "previous", "editor"]
    assert content.set_draft_status(d.id, "approved").status == "approved"
    content.record_run("blog", draft_id=d.id, opportunity_id=opp.id, model="m", prompt_version="blog_v1", status="ok", usage={"total_tokens": 1}, seconds=1.5)
    assert content.list_runs()[0]["draft_id"] == d.id
    listing = content.list_drafts("approved")
    assert listing and listing[0].id == d.id and listing[0].body_markdown == ""  # listing omits bodies


def test_citation_markers_are_harvested_and_stripped():
    from agents.blog_agent.agent import inline_citation_ids, strip_citation_markers

    md = "We grew [id=j:quote:0]. Labs [id=j:summary:0, id=j:topic:1].\n\n* item [id=j:transcript:2]  \n"
    assert inline_citation_ids(md) == ["j:quote:0", "j:summary:0", "j:topic:1", "j:transcript:2"]
    assert strip_citation_markers(md) == "We grew. Labs.\n\n* item\n"


@pytest.fixture
def pg_app(test_db_url, tmp_path, monkeypatch):  # noqa: F811
    """API on the throw-away Postgres DB with mock AI + mock embeddings."""
    from apps.api import config

    monkeypatch.setenv("AI_PROVIDER", "mock")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "mock")
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NAS_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    config.get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from apps.api.main import create_app

    with TestClient(create_app()) as client:
        yield client
    config.get_settings.cache_clear()
    import psycopg
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        conn.execute("TRUNCATE audit_log, knowledge_blocks, processing_jobs, media, content_opportunities, drafts, draft_versions, agent_runs CASCADE")


def test_end_to_end_video_to_reviewed_draft(pg_app, tiny_video):
    import time

    client = pg_app
    assert client.get("/api/v1/system").json()["store"] == "postgres"
    with tiny_video.open("rb") as f:
        job_id = client.post("/api/v1/analyze", files={"file": (tiny_video.name, f, "video/mp4")}).json()["job_id"]
    for _ in range(100):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("CONTENT_CANDIDATE", "FAILED"):
            break
        time.sleep(0.1)
    assert job["state"] == "CONTENT_CANDIDATE", job.get("error")
    states = [s["state"] for s in job["stages"]]
    assert states[-4:] == ["BLOCKS_COMPLETE", "EMBEDDING", "INDEXED", "CONTENT_CANDIDATE"]

    opps = client.get("/api/v1/opportunities", params={"status": "new"}).json()
    assert len(opps) == 1 and opps[0]["job_id"] == job_id
    oid = opps[0]["id"]
    assert client.post(f"/api/v1/opportunities/{oid}/status", json={"status": "accepted"}).json()["status"] == "accepted"

    d = client.post("/api/v1/drafts", json={"opportunity_id": oid, "target_words": 600}).json()
    assert d["status"] == "generating"
    for _ in range(100):
        d = client.get(f"/api/v1/drafts/{d['id']}").json()
        if d["status"] in ("new", "failed"):
            break
        time.sleep(0.1)
    assert d["status"] == "new", d.get("error")
    assert d["title"].startswith("[MOCK]") and d["citations"] and "[id=" not in d["body_markdown_clean"]
    assert client.get("/api/v1/opportunities", params={"status": "drafted"}).json()[0]["id"] == oid

    e = client.put(f"/api/v1/drafts/{d['id']}", json={"title": "Edited title"}).json()
    assert e["version"] == 2 and e["status"] == "in_review"
    a = client.post(f"/api/v1/drafts/{d['id']}/status", json={"status": "approved"}).json()
    assert a["status"] == "approved"
    versions = client.get(f"/api/v1/drafts/{d['id']}/versions").json()
    assert [v["edited_by"] for v in versions] == ["agent", "previous", "editor"]
    runs = client.get("/api/v1/agent-runs").json()
    assert runs and runs[0]["status"] == "ok" and runs[0]["draft_id"] == d["id"]
    # a brief with nothing relevant indexed fails cleanly
    bad = client.post("/api/v1/drafts", json={"brief": "zzzz qqqq nothing"}).json()
    for _ in range(50):
        bad = client.get(f"/api/v1/drafts/{bad['id']}").json()
        if bad["status"] in ("new", "failed"):
            break
        time.sleep(0.1)
    # the mock embedder still finds *something* by hash collision or none - either a draft or a clean failure is acceptable
    assert bad["status"] in ("new", "failed")
