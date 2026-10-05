import hashlib
import json
from types import SimpleNamespace

import pytest

from media_search.export import export_asset

UUID = "12345678-1234-1234-1234-123456789ABC"


def test_export_requires_exact_indexed_asset_and_new_private_destination(tmp_path, monkeypatch):
    helper = tmp_path / "Helper.app"
    helper.mkdir()
    out = tmp_path / "new"
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        path = out / (UUID + ".mov")
        path.write_bytes(b"original video fixture")
        response = out / "photos-response.json"
        response.write_text(json.dumps([{"uuid": UUID, "path": str(path), "mediaType": "video"}]))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("media_search.export.subprocess.run", run)
    with pytest.raises(ValueError, match="indexed asset"):
        export_asset(UUID, out, store=SimpleNamespace(asset=lambda _: None), config={"photos_helper_app": str(helper)})
    assert not out.exists()
    store = SimpleNamespace(asset=lambda _: {"uuid": UUID})
    result = export_asset(UUID.lower(), out, store=store, config={"photos_helper_app": str(helper)})
    assert result["sha256"] == hashlib.sha256(b"original video fixture").hexdigest()
    assert result["download_missing_allowed"] is False
    assert "--download-missing" not in commands[0]
    assert commands[0][commands[0].index("--uuid")+1] == UUID + "/L0/001"
    assert out.stat().st_mode & 0o777 == 0o700
    assert (out/(UUID+".mov")).stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="new destination"):
        export_asset(UUID, out, store=store, config={"photos_helper_app": str(helper)})


def test_export_rejects_native_response_outside_destination(tmp_path, monkeypatch):
    helper=tmp_path/"Helper.app";helper.mkdir()
    external=tmp_path/"other.mov";external.write_bytes(b"must remain")
    out=tmp_path/"export"
    def run(command, **kwargs):
        (out/"photos-response.json").write_text(json.dumps([{"uuid":UUID,"path":str(external)}]))
    monkeypatch.setattr("media_search.export.subprocess.run",run)
    with pytest.raises(RuntimeError,match="unexpected export"):
        export_asset(UUID,out,True,store=SimpleNamespace(asset=lambda _:{"uuid":UUID}),config={"photos_helper_app":str(helper)})
    assert external.read_bytes()==b"must remain"
