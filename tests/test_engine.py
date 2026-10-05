import json
from io import BytesIO
from pathlib import Path
import uuid
import numpy as np
import pytest
from fastapi.testclient import TestClient
from media_search.indexer import synchronize
from media_search.indexer import read_preview
from media_search.photos_bridge import local_previews
from media_search.server import create_app
from media_search.store import Store
from PIL import Image


def make_row(tmp_path, name, date="2026-01-01", person="Ada", media="image"):
    path = tmp_path / (name + ".jpg")
    Image.new("RGB", (16, 16), "red").save(path)
    return dict(uuid=str(uuid.uuid4()).upper(), filename=name+".jpg", date=date, persons=[person],
                place="London", media_type=media, path=str(path), modified=None)


def embed(store, row, vector):
    with store.connect() as db:
        db.execute("UPDATE assets SET embedding=? WHERE uuid=?", (np.array(vector, dtype=np.float32).tobytes(), row["uuid"]))


def test_incremental_change_and_removed_assets_do_not_reuse_vectors(tmp_path):
    store = Store(tmp_path / "runtime")
    a, b = make_row(tmp_path,"a"), make_row(tmp_path,"b")
    assert len(synchronize(store,[a,b],"first")) == 2
    embed(store,a,[1,0]); embed(store,b,[0,1])
    # Unchanged file retains its embedding while person metadata changes immediately.
    a["persons"] = ["Grace"]
    assert synchronize(store,[a,b],"second") == []
    assert store.asset(a["uuid"])["persons"] == ["Grace"]
    Path(a["path"]).write_bytes(b"changed preview bytes")
    assert synchronize(store,[a],"third") == [a]
    assert store.asset(b["uuid"]) is None
    assert store.status()["indexed"] == 0
    # An original disappearing cannot leave a searchable stale vector.
    a["path"] = None
    assert synchronize(store,[a],"fourth") == []
    assert store.status()["failed"] == 1


def test_filter_before_rank_and_dates_include_full_end_day(tmp_path):
    store = Store(tmp_path / "runtime")
    a=make_row(tmp_path,"a","2026-01-01T23:59:59+01:00","Ada")
    b=make_row(tmp_path,"b","2026-01-02T01:00:00+01:00","Grace","video")
    synchronize(store,[a,b],"test"); embed(store,a,[0,1]); embed(store,b,[1,0])
    assert store.search(np.array([1,0]))["results"][0]["uuid"] == b["uuid"]
    result=store.search(np.array([1,0]), person="Ada", date_to="2026-01-01")
    assert [r["uuid"] for r in result["results"]] == [a["uuid"]]
    assert "path" not in result["results"][0]
    assert store.search(media_type="video")["results"][0]["preview_scope"] == "local video poster"


def test_local_api_security_and_exact_identity(tmp_path):
    store=Store(tmp_path / "runtime")
    row=make_row(tmp_path,"a")
    synchronize(store,[row],"test"); embed(store,row,[1,0])
    opened=[]
    client=TestClient(create_app(store,opener=opened.append))
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/status",headers={"Host":"evil.test"}).status_code == 400
    assert client.get("/api/search",headers={"Origin":"https://evil.test"}).status_code == 403
    assert client.get("/api/status",headers={"Sec-Fetch-Site":"cross-site"}).status_code == 403
    assert client.get("/api/assets/not-a-uuid/thumbnail").status_code == 404
    assert client.get("/api/search?date_from=yesterday").status_code == 422
    assert client.get("/api/search?date_from=20260101").status_code == 422
    assert client.get("/api/search?date_from=2026-W01-4").status_code == 422
    assert client.get("/api/search?date_from=2026-02-01&date_to=2026-01-01").status_code == 422
    url=f"/api/assets/{row['uuid']}/open"
    assert client.post(url).status_code == 403
    assert client.post(url,headers={"Origin":"http://testserver"}).status_code == 200
    assert opened == [row["uuid"]]
    response=client.get(f"/api/assets/{row['uuid']}/thumbnail")
    with Image.open(BytesIO(response.content)) as thumbnail:
        assert thumbnail.format == "JPEG"
        assert thumbnail.size == (16, 16)
    assert response.headers["cache-control"] == "no-store"


def test_movie_derivative_and_corrupt_image_fall_back_to_jpeg(tmp_path):
    from PIL import Image
    poster=tmp_path/"poster.jpg"
    Image.new("RGB",(16,16),"red").save(poster)
    movie=tmp_path/"larger.mov"; movie.write_bytes(b"not an image"*1000)
    bad=tmp_path/"broken.jpg"; bad.write_bytes(b"broken JPEG"*100)
    candidates=local_previews([str(poster),str(movie),str(bad)])
    assert str(movie) not in candidates
    row={"path":candidates[0],"paths":candidates}
    assert read_preview(row).size == (16,16)
    assert row["path"] == str(poster)


def test_agent_preview_path_recovers_when_photos_removes_largest_derivative(tmp_path, monkeypatch, capsys):
    import sys
    import media_search.cli as cli
    store = Store(tmp_path / "runtime")
    row = make_row(tmp_path, "original")
    alternate = tmp_path / "alternate.jpg"
    Image.new("RGB", (12, 12), "blue").save(alternate)
    row["paths"] = [row["path"], str(alternate)]
    synchronize(store, [row], "initial")
    Path(row["path"]).unlink()
    client = TestClient(create_app(store))
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(cli, "Store", lambda: store)
    monkeypatch.setattr(sys, "argv", ["media-search", "asset", row["uuid"], "--preview-path"])
    cli.main()
    assert json.loads(capsys.readouterr().out)["local_preview_path"] == str(alternate)


def test_status_recovers_after_abandoned_process_and_respects_lock(tmp_path):
    import fcntl
    store=Store(tmp_path / "runtime")
    store.set_state(indexing=True)
    assert store.status()["indexing"] is False
    with (store.home/"index.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert store.status()["indexing"] is True
    assert store.status()["indexing"] is False


def test_background_opener_uses_app_identity_and_full_asset_identifier(tmp_path, monkeypatch):
    import media_search.server as server
    store=Store(tmp_path/"runtime")
    row=make_row(tmp_path,"a")
    synchronize(store,[row],"test"); embed(store,row,[1,0])
    monkeypatch.setattr(server,"settings",lambda:{"photos_python":"/usr/bin/python3","photos_helper_app":"/Example Helper.app"})
    monkeypatch.setattr(server,"runtime",lambda:store.home)
    commands=[]
    def run(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_text('{"ok":true}')
    monkeypatch.setattr(server.subprocess,"run",run)
    client=TestClient(server.create_app(store))
    response=client.post(f"/api/assets/{row['uuid']}/open",headers={"X-Media-Search-Client":"cli"})
    assert response.status_code == 200
    assert commands[0][:3] == ["/usr/bin/open","-n","/Example Helper.app"]
    assert commands[0][commands[0].index("--uuid")+1] == row["uuid"]+"/L0/001"


def test_cached_catalog_refreshes_after_incremental_update(tmp_path):
    store=Store(tmp_path/"runtime")
    row=make_row(tmp_path,"a")
    synchronize(store,[row],"test");embed(store,row,[1,0])
    assert store.search(np.array([1,0]))["results"][0]["score"] == 1
    embed(store,row,[0,1])
    assert store.search(np.array([1,0]))["results"][0]["score"] == 0
    synchronize(store,[],"next")
    assert store.search(np.array([1,0]))["results"] == []
