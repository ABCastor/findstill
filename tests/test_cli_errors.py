import json
import subprocess
import sys

import pytest
from media_search import cli, indexer


@pytest.mark.parametrize("failure", [OSError("Photos unavailable"), subprocess.CalledProcessError(1, ["provider"])])
def test_provider_failures_return_json_without_a_traceback(monkeypatch, capsys, failure):
    def fail(**kwargs):
        raise failure
    monkeypatch.setattr(indexer, "index", fail)
    monkeypatch.setattr(sys, "argv", ["findstill", "index"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"] == str(failure)
    assert "Traceback" not in captured.err


def test_model_preparation_is_explicit_and_independent_of_index(monkeypatch, capsys):
    from media_search import model
    from media_search.config import MODEL, REVISION
    calls = []
    monkeypatch.setattr(model, "Encoder", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(indexer, "index", lambda **kwargs: pytest.fail("Preparation must not read or reindex Photos"))
    monkeypatch.setattr(sys, "argv", ["findstill", "prepare-model"])
    cli.main()
    assert calls == [{"device": "cpu", "allow_download": True}]
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"model": MODEL, "revision": REVISION, "cache_ready": True}
