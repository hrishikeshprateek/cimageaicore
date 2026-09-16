"""Prompt registry + editor API: bundled versions are read-only, edits become new overlay versions, activation applies live."""
from __future__ import annotations

from pathlib import Path

import pytest

from services.prompts.registry import KINDS, PromptRegistry


def test_registry_lists_reads_saves_and_activates(tmp_path: Path):
    reg = PromptRegistry(tmp_path / "prompts", tmp_path / "prompt_config.json", defaults={"video-analysis": "v2"}, institution_context="Test College")
    kinds = {k["kind"]: k for k in reg.describe()["kinds"]}
    assert set(kinds) == set(KINDS) and kinds["video-analysis"]["active"] == "v2"
    assert [v["version"] for v in kinds["video-analysis"]["versions"]] == ["v2", "v1"]          # default first, then natural order
    assert [v["version"] for v in kinds["people-pass"]["versions"]] == ["people_v1"]             # people_* never leaks into video-analysis
    assert all(v["source"] == "bundled" for v in kinds["blog"]["versions"])
    system, user = reg.prompt("video-analysis")
    assert "{institution_context}" in system and "{known_people}" in system
    assert reg.known_people().startswith("- ") and "Neeraj" in reg.known_people()
    # bundled files are never edited in place
    with pytest.raises(FileExistsError):
        reg.save("video-analysis", "v2", reg.read("video-analysis", "v2"))
    # a new version must keep the sections and the kind's prefix; placeholders missing are warnings, not errors
    with pytest.raises(ValueError):
        reg.save("video-analysis", "v3", "no sections here")
    with pytest.raises(ValueError):
        reg.save("video-analysis", "custom3", "## system\nx\n## user\ny")
    text = reg.read("video-analysis", "v2").replace("Be faithful.", "Be faithful and brief.")
    path = reg.save("video-analysis", "v3", text)
    assert path == tmp_path / "prompts" / "video-analysis" / "v3.md" and reg.path("video-analysis", "v3") == path
    assert {v["version"]: v["source"] for v in reg.versions("video-analysis")} == {"v2": "bundled", "v1": "bundled", "v3": "custom"}
    assert reg.suggest_version("video-analysis") == "v4" and reg.suggest_version("blog").startswith("blog_v") and reg.suggest_version("cuts") == "cuts_v2"
    with pytest.raises(FileExistsError):
        reg.save("video-analysis", "v3", text)                     # exists -> needs overwrite=True
    reg.save("video-analysis", "v3", text + "\n", overwrite=True)
    # activation persists and notifies listeners; the default is not stored
    seen = []
    reg.on_change(lambda r: seen.append(r.active("video-analysis")))
    reg.set_active("video-analysis", "v3")
    assert reg.active("video-analysis") == "v3" and seen == ["v3"] and "and brief" in reg.prompt("video-analysis")[0]
    assert PromptRegistry(tmp_path / "prompts", tmp_path / "prompt_config.json", defaults={"video-analysis": "v2"}).active("video-analysis") == "v3"
    with pytest.raises(ValueError):
        reg.delete("video-analysis", "v3")                          # active
    reg.set_active("video-analysis", "v2"); reg.delete("video-analysis", "v3")
    assert reg.config.active == {} and reg.path("video-analysis", "v3") is None
    # context + roster overlay
    reg.set_institution_context("  CIMAGE, Patna  ")
    assert reg.institution_context == "CIMAGE, Patna"
    reg.set_institution_context("Test College"); assert reg.config.institution_context is None   # same as env -> not stored
    reg.save("known-people", "known_people", "# roster\nAsha Verma\n")
    assert reg.known_people() == "- Asha Verma"


def test_editor_api_and_live_reload(app_env, monkeypatch):
    from fastapi.testclient import TestClient
    from apps.api import config

    config.get_settings.cache_clear()
    from apps.api.main import create_app

    with TestClient(create_app()) as client:
        d = client.get("/api/v1/prompts").json()
        va = next(k for k in d["kinds"] if k["kind"] == "video-analysis")
        assert va["active"] == "v2" and any(v["version"] == "v1" for v in va["versions"]) and d["institution_context"]
        r = client.get("/api/v1/prompts/video-analysis/v2").json()
        assert r["source"] == "bundled" and r["active"] and r["suggested_version"] == "v3" and "## system" in r["text"]
        assert client.get("/api/v1/prompts/video-analysis/nope").status_code == 404
        assert client.get("/api/v1/prompts/what/v1").status_code == 404
        assert client.post("/api/v1/prompts/video-analysis", json={"version": "v2", "text": r["text"]}).status_code == 409
        assert client.post("/api/v1/prompts/video-analysis", json={"version": "v3", "text": "broken"}).status_code == 400
        new = r["text"].replace("institutional media analyst", "institutional media analyst (v3 test)")
        s = client.post("/api/v1/prompts/video-analysis", json={"version": "v3", "text": new, "activate": True}).json()
        assert s["active"] == "v3" and s["saved"].endswith("prompts/video-analysis/v3.md")
        engine = client.app.state.engine
        assert engine.prompt_version == "v3" and "(v3 test)" in engine.system_template        # reloaded live, no restart
        # institution context flows into the engine and the writer
        client.put("/api/v1/prompts/context", json={"institution_context": "Some Other College"})
        assert engine.institution_context == "Some Other College" and client.app.state.blog_agent.institution_context == "Some Other College"
        client.put("/api/v1/prompts/context", json={"institution_context": None})
        assert engine.institution_context == client.app.state.settings.institution_context
        # switch back and delete the custom version
        assert client.put("/api/v1/prompts/video-analysis/active", json={"version": "v2"}).status_code == 200
        assert engine.prompt_version == "v2" and "(v3 test)" not in engine.system_template
        assert client.delete("/api/v1/prompts/video-analysis/v3").status_code == 200
        assert client.delete("/api/v1/prompts/video-analysis/v2").status_code == 404                # bundled: not deletable
        # blog writer follows its own kind
        b = client.get("/api/v1/prompts/blog/blog_v2").json()
        client.post("/api/v1/prompts/blog", json={"version": "blog_v3", "text": b["text"] + "\nAlways end with a call to action.", "activate": True})
        assert client.app.state.blog_agent.prompt_version == "blog_v3" and "call to action" in client.app.state.blog_agent.user_template + client.app.state.blog_agent.system_template
    assert (app_env["data"] / "prompts" / "content-generation" / "blog_v3.md").exists() and (app_env["data"] / "prompt_config.json").exists()
