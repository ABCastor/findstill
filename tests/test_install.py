import importlib
import json
from pathlib import Path
import plistlib
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture
def installation(tmp_path, monkeypatch):
    module = importlib.import_module("media_search.install")
    home = tmp_path / "home"
    home.mkdir()
    runtime = home / "runtime"
    runtime.mkdir()
    bin_dir = home / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("interpreter")
    (bin_dir / "media-search").write_text("cli")
    photos_python = home / "photos-venv" / "bin" / "python"
    photos_python.parent.mkdir(parents=True)
    photos_python.symlink_to(bin_dir / "python")
    library = home / "Photos Library.photoslibrary"
    (library / "database").mkdir(parents=True)
    (library / "database" / "Photos.sqlite").write_bytes(b"fixture")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(module, "runtime", lambda: runtime)
    monkeypatch.setattr(module.sys, "executable", str(bin_dir / "python"))
    commands = []
    loaded = set()

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "print":
            return SimpleNamespace(returncode=0 if command[-1] in loaded else 1)
        if command[1] == "bootstrap":
            label = plistlib.loads(Path(command[-1]).read_bytes())["Label"]
            loaded.add(command[-2] + "/" + label)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    return module, home, runtime, photos_python, library, commands


def test_launch_services_preserve_interpreter_library_offline_and_schedules(installation):
    module, home, runtime, photos_python, library, commands = installation
    result = module.install(photos_python, photos_library=library)
    config = json.loads((runtime / "settings.json").read_text())
    assert config["photos_python"] == str(photos_python)
    assert config["photos_library"] == str(library)
    assert (runtime / "settings.json").stat().st_mode & 0o777 == 0o600
    assert result["refresh_hours"] == 6
    assert result["dashboard"] == "http://127.0.0.1:18473"
    assert len([c for c in commands if c[1] == "bootstrap"]) == 2
    for suffix in ("server", "index"):
        spec = plistlib.loads((home / "Library/LaunchAgents" / f"com.media-search.{suffix}.plist").read_bytes())
        assert spec["EnvironmentVariables"] == {
            "TOKENIZERS_PARALLELISM": "false", "HF_HUB_OFFLINE": "1", "MEDIA_SEARCH_HOME": str(runtime)
        }
        assert spec["ProcessType"] == ("Interactive" if suffix == "server" else "Standard")
        assert spec["ProgramArguments"][0] == str(home / "venv/bin/media-search")
        if suffix == "server":
            assert spec["KeepAlive"] is True
            assert spec["RunAtLoad"] is True
            assert "StartInterval" not in spec
        else:
            assert spec["StartInterval"] == 21600
            assert spec["RunAtLoad"] is False
            assert "KeepAlive" not in spec
    module.install(photos_python, photos_library=library)
    assert len([c for c in commands if c[1] == "bootstrap"]) == 2


def test_installer_preserves_differing_existing_service(installation):
    module, home, runtime, photos_python, library, _ = installation
    module.install(photos_python, photos_library=library)
    plist = home / "Library/LaunchAgents/com.media-search.index.plist"
    spec = plistlib.loads(plist.read_bytes())
    spec["StartInterval"] = 3600
    original = plistlib.dumps(spec)
    plist.write_bytes(original)
    settings_file = runtime / "settings.json"
    config = json.loads(settings_file.read_text())
    config["photos_helper_app"] = "/existing/Helper.app"
    settings_file.write_text(json.dumps(config))
    settings_before = settings_file.read_bytes()
    with pytest.raises(ValueError, match="Existing service differs"):
        module.install(photos_python, photos_library=library)
    assert plist.read_bytes() == original
    assert settings_file.read_bytes() == settings_before


def test_installer_preserves_settings_on_an_unrelated_cli_conflict(installation):
    module, home, runtime, photos_python, library, commands = installation
    settings_file = runtime / "settings.json"
    original = b'{"photos_helper_app":"/existing/Helper.app","port":19000}'
    settings_file.write_bytes(original)
    link = home / ".local/bin/media-search"
    link.parent.mkdir(parents=True)
    link.write_text("unrelated executable")
    with pytest.raises(ValueError, match="unrelated media-search"):
        module.install(photos_python, photos_library=library)
    assert settings_file.read_bytes() == original
    assert link.read_text() == "unrelated executable"
    assert not commands


def test_installer_retains_helper_when_reinstalled_without_that_option(installation):
    module, home, runtime, photos_python, library, _ = installation
    helper = home / "Helper.app"
    helper.mkdir()
    module.install(photos_python, photos_library=library, photos_helper_app=helper)
    module.install(photos_python, photos_library=library)
    assert json.loads((runtime / "settings.json").read_text())["photos_helper_app"] == str(helper)


def test_installer_rejects_a_library_without_a_photos_database(installation):
    module, home, runtime, photos_python, _, commands = installation
    with pytest.raises(ValueError, match="existing Photos database"):
        module.install(photos_python, photos_library=home)
    assert not (runtime / "settings.json").exists()
    assert not commands


def test_index_passes_configured_library_without_discovering_photos_preferences(tmp_path, monkeypatch):
    from media_search import indexer
    from media_search.store import Store

    store = Store(tmp_path / "runtime")
    config = {"photos_python": "/provider-venv/bin/python", "photos_library": "/Pictures/Selected.photoslibrary"}
    monkeypatch.setattr(indexer, "settings", lambda: config)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        Path(command[2]).write_text(json.dumps({"assets": [], "excluded": 0, "version": "fixture"}))

    monkeypatch.setattr(indexer.subprocess, "run", run)
    indexer.index(store=store)
    command, kwargs = calls[0]
    assert command[0] == config["photos_python"]
    assert command[-1] == config["photos_library"]
    assert kwargs == {"check": True, "timeout": 180}
    assert store.status()["indexing"] is False
    assert store.status()["error"] is None


def test_provider_failure_preserves_catalog_and_is_not_reported_as_success(tmp_path, monkeypatch):
    from media_search import indexer
    from media_search.store import Store

    store = Store(tmp_path / "runtime")
    with store.connect() as db:
        db.execute("INSERT INTO assets VALUES (?,?,?,NULL,NULL,?)", ("existing", "{}", "fingerprint", "before"))
    (store.home / "snapshot.json").write_text(json.dumps({"assets": [], "excluded": 0, "version": "stale"}))
    monkeypatch.setattr(indexer, "settings", lambda: {})

    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 180)

    monkeypatch.setattr(indexer.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        indexer.index(store=store)
    with store.connect() as db:
        assert db.execute("SELECT uuid FROM assets").fetchall()[0]["uuid"] == "existing"
    assert store.status()["indexing"] is False
    assert store.status()["error"].startswith("TimeoutExpired:")
    assert "indexed_at" not in store.status()


def test_photos_bridge_opens_explicit_library(tmp_path, monkeypatch):
    import sys
    from media_search.photos_bridge import snapshot

    libraries = []

    def database(**kwargs):
        libraries.append(kwargs)
        return SimpleNamespace(photos=lambda: [])

    monkeypatch.setitem(sys.modules, "osxphotos", SimpleNamespace(PhotosDB=database, __version__="fixture"))
    result = snapshot(str(tmp_path / "Selected.photoslibrary"))
    assert libraries == [{"dbfile": str(tmp_path / "Selected.photoslibrary")}]
    assert result["assets"] == []
