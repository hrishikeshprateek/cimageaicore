"""Folder picker: browse mounted locations, pick watched folders from the UI, applied to the running watcher."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from fastapi.testclient import TestClient

from services.ingestion.config import WatcherConfig, folder_status, is_browsable, list_dir, load_config, place_label, places, save_config


def test_config_roundtrip_and_browse_helpers(tmp_path: Path, tiny_video: Path):
    cfg_file = tmp_path / "data" / "watcher_config.json"
    assert load_config(cfg_file).roots == []
    save_config(cfg_file, WatcherConfig(roots=[str(tmp_path / "nas")]))
    assert load_config(cfg_file).roots == [str(tmp_path / "nas")]
    cfg_file.write_text("{broken", encoding="utf-8")
    assert load_config(cfg_file).roots == []

    nas = tmp_path / "nas"
    (nas / "Cimage AI Agent" / "sub").mkdir(parents=True)
    (nas / ".hidden").mkdir()
    shutil.copy(tiny_video, nas / "Cimage AI Agent" / "clip.mp4")
    (nas / "Cimage AI Agent" / "notes.txt").write_text("x")
    assert nas in places([nas]) and tmp_path / "missing" not in places([tmp_path / "missing"])
    assert is_browsable(nas / "Cimage AI Agent", [nas]) and not is_browsable(tmp_path, [nas]) and not is_browsable(Path("/etc"), [nas])
    st = folder_status(nas / "Cimage AI Agent")
    assert st["exists"] and st["readable"] and st["videos"] == 1 and st["folders"] == 1
    assert folder_status(tmp_path / "nope")["exists"] is False
    d = list_dir(nas)
    assert [f["name"] for f in d["folders"]] == ["Cimage AI Agent"] and d["folders"][0]["videos"] == 1 and d["parent"] == str(tmp_path)
    inner = list_dir(nas / "Cimage AI Agent")
    assert [v["name"] for v in inner["videos"]] == ["clip.mp4"] and [f["name"] for f in inner["folders"]] == ["sub"]


def test_pick_folder_from_the_ui_and_watcher_picks_up_a_video(app_env, tiny_video: Path, monkeypatch):
    monkeypatch.setenv("WATCHER_ENABLED", "true")
    monkeypatch.setenv("WATCHER_INTERVAL_SECONDS", "3600")   # we trigger scans by hand
    monkeypatch.setenv("WATCHER_STABLE_SECONDS", "0")
    monkeypatch.setenv("NAS_BROWSE_ANYWHERE", "false")       # restricted mode: only NAS mounts + configured roots
    from apps.api import config

    config.get_settings.cache_clear()
    from apps.api.main import create_app

    drop = app_env["nas"] / "Cimage AI Agent"
    drop.mkdir()
    with TestClient(create_app()) as client:
        r = client.get("/api/v1/nas/roots").json()
        assert r["enabled"] and all(x["source"] == "env" for x in r["roots"]) and any(p["path"] == str(app_env["nas"]) for p in r["places"])
        top = client.get("/api/v1/nas/browse").json()
        assert top["places"] and any(f["path"] == str(app_env["nas"]) for f in top["folders"])
        lst = client.get("/api/v1/nas/browse", params={"path": str(app_env["nas"])}).json()
        assert [f["name"] for f in lst["folders"]] == ["Cimage AI Agent"] and lst["parent"] is None   # cannot climb above the place
        assert client.get("/api/v1/nas/browse", params={"path": "/etc"}).status_code == 400
        assert client.put("/api/v1/nas/roots", json={"roots": ["relative/path"]}).status_code == 400
        assert client.put("/api/v1/nas/roots", json={"roots": ["/etc"]}).status_code == 400

        r = client.put("/api/v1/nas/roots", json={"roots": [str(drop)]}).json()
        ui = [x for x in r["roots"] if x["source"] == "ui"]
        assert ui and ui[0]["path"] == str(drop) and ui[0]["exists"]
        assert str(drop) in client.get("/api/v1/watcher").json()["roots"]
        assert load_config(config.get_settings().watcher_config_file).roots == [str(drop)]

        # a video dropped into the picked folder is submitted by the next scan
        shutil.copy(tiny_video, drop / "review.mp4")
        first = client.post("/api/v1/watcher/scan?sync=true").json()
        second = client.post("/api/v1/watcher/scan?sync=true").json()   # stability needs two sightings
        assert first["seen"] >= 1 and (first["queued"] + second["queued"]) == 1
        idx = client.get("/api/v1/watcher/index").json()
        assert any(i["path"] == str(drop / "review.mp4") and i["action"] == "queued" for i in idx)
        for _ in range(100):
            jobs = client.get("/api/v1/jobs").json()
            if jobs and jobs[0]["state"] in ("BLOCKS_COMPLETE", "FAILED", "INDEXED", "CONTENT_CANDIDATE"):
                break
            time.sleep(0.1)
        assert jobs[0]["source"]["kind"] == "nas_file" and jobs[0]["state"] != "FAILED"

        # removing the folder from the UI keeps the .env roots
        r = client.put("/api/v1/nas/roots", json={"roots": []}).json()
        assert all(x["source"] == "env" for x in r["roots"]) and str(drop) not in client.get("/api/v1/watcher").json()["roots"]

    # the choice survives a restart
    with TestClient(create_app()) as client:
        client.put("/api/v1/nas/roots", json={"roots": [str(drop)]})
    with TestClient(create_app()) as client:
        assert str(drop) in client.get("/api/v1/watcher").json()["roots"]


def test_picker_browses_the_whole_filesystem_by_default(app_env, tmp_path: Path, monkeypatch):
    """NAS_BROWSE_ANYWHERE (default true): Home / Desktop / mounted drives / the whole system are starting points, any folder
    can be opened and chosen, / is the top, and OS internals are hidden when listing /."""
    monkeypatch.setenv("WATCHER_ENABLED", "true")
    monkeypatch.setenv("WATCHER_INTERVAL_SECONDS", "3600")
    from apps.api import config

    config.get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from apps.api.main import create_app

    footage = tmp_path / "somewhere else" / "Camera Cards"; footage.mkdir(parents=True)
    with TestClient(create_app()) as client:
        r = client.get("/api/v1/nas/roots").json()
        assert r["browse_anywhere"] is True and any(p["label"].startswith("Home") for p in r["places"]) and all(p["path"] != "/" for p in r["places"])
        top = client.get("/api/v1/nas/browse").json()
        names = [f["name"] for f in top["folders"]]
        assert any(n.startswith("Home") for n in names) and names[-1].startswith("Whole system") and top["folders"][-1]["path"] == "/"
        root = client.get("/api/v1/nas/browse", params={"path": "/"}).json()
        assert root["parent"] is None and not ({"etc", "usr", "bin", "proc", "System"} & {f["name"] for f in root["folders"]})
        lst = client.get("/api/v1/nas/browse", params={"path": str(tmp_path / "somewhere else")}).json()
        assert [f["name"] for f in lst["folders"]] == ["Camera Cards"] and lst["parent"] == str(tmp_path)   # can climb, all the way up
        assert client.get("/api/v1/nas/browse", params={"path": str(tmp_path / "missing")}).status_code == 404
        r = client.put("/api/v1/nas/roots", json={"roots": [str(footage)]}).json()
        assert any(x["path"] == str(footage) and x["source"] == "ui" for x in r["roots"])
        assert client.put("/api/v1/nas/roots", json={"roots": ["relative/path"]}).status_code == 400



def test_places_inside_docker_are_the_mounted_host_folders(tmp_path: Path):
    """In the api container the filesystem is the image's: the container's home is nobody's Desktop, the image's empty /home
    and /srv are noise, /data is the app's own volume - the places are /nas + the host's /mnt and /media (mounted by compose,
    listed even while empty so it is obvious where the share must go), configured roots and the container root."""
    home = Path.home()
    ps = places([tmp_path], anywhere=True, in_docker=True)
    assert home not in ps and home / "Desktop" not in ps and home / "Downloads" not in ps
    assert Path("/data") not in ps and tmp_path in ps and ps[-1] == Path("/")
    for raw in ("/home", "/srv"):
        p = Path(raw)
        if p.is_dir() and next(p.iterdir(), None) is None:   # empty on this machine -> hidden in docker mode, shown otherwise
            assert p not in ps and p in places([], anywhere=True, in_docker=False)
    assert place_label(Path("/mnt"), in_docker=True) == "Host mounts  /mnt" and place_label(Path("/mnt"), in_docker=False) == "Mounts  /mnt"
    assert place_label(Path("/"), in_docker=True).startswith("Container filesystem") and place_label(Path("/"), in_docker=False).startswith("Whole system")
    assert place_label(Path("/nas"), in_docker=True).startswith("NAS")
    # outside docker nothing changes: home and the whole system are starting points
    outside = places([], anywhere=True, in_docker=False)
    assert home in outside and outside[-1] == Path("/")
