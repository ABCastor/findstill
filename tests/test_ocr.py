import platform
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw, ImageFont
import pytest

from media_search import ocr


def test_invalid_file_rejected_before_compilation(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "_helper", lambda: pytest.fail("must not compile for invalid input"))
    with pytest.raises(ValueError, match="existing image"):
        ocr.read_page(tmp_path / "missing.png")
    invalid = tmp_path / "bad.png"
    invalid.write_bytes(b"not an image")
    with pytest.raises(ValueError, match="valid, bounded"):
        ocr.read_page(invalid)
    with pytest.raises(ValueError, match="existing image"):
        ocr.read_page(tmp_path)


def test_image_size_and_multiple_frames_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "_helper", lambda: pytest.fail("must not compile for rejected input"))
    image = tmp_path / "page.png"
    Image.new("RGB", (32, 20), "white").save(image)
    monkeypatch.setattr(ocr, "MAX_EDGE", 16)
    with pytest.raises(ValueError, match="pixel limit"):
        ocr.read_page(image)
    monkeypatch.setattr(ocr, "MAX_EDGE", 12000)
    monkeypatch.setattr(ocr, "MAX_BYTES", 1)
    with pytest.raises(ValueError, match="file limit"):
        ocr.read_page(image)
    monkeypatch.setattr(ocr, "MAX_BYTES", 150000000)
    animation = tmp_path / "animation.gif"
    Image.new("RGB", (32, 20), "white").save(animation, save_all=True,
        append_images=[Image.new("RGB", (32, 20), "black")])
    with pytest.raises(ValueError, match="one still image"):
        ocr.read_page(animation)


def test_native_failure_is_clear_without_shell(monkeypatch):
    def fail(arguments, **kwargs):
        assert isinstance(arguments, list)
        assert "shell" not in kwargs
        raise subprocess.CalledProcessError(1, arguments, stderr="Vision failed")
    monkeypatch.setattr(ocr.subprocess, "run", fail)
    with pytest.raises(ocr.OCRError, match="Vision failed"):
        ocr._run(["helper", "path with spaces.png"], timeout=120)


def test_compiler_cache_tracks_source_and_uses_argument_lists(tmp_path, monkeypatch):
    if platform.system() != "Darwin":
        pytest.skip("compilation cache locking requires macOS")
    source = tmp_path / "ocr.swift"
    source.write_text("first source")
    monkeypatch.setattr(ocr, "_source", lambda: source)
    monkeypatch.setattr(ocr, "runtime", lambda: tmp_path / "runtime")
    commands = []
    def run(arguments, *, timeout):
        commands.append(arguments)
        if arguments[0] == "/usr/bin/xcrun":
            output = "/sdk with spaces\n" if "--show-sdk-path" in arguments else "/path with spaces/swiftc\n"
            return subprocess.CompletedProcess(arguments, 0, output, "")
        Path(arguments[-1]).write_bytes(b"test binary")
        return subprocess.CompletedProcess(arguments, 0, "", "")
    monkeypatch.setattr(ocr, "_run", run)
    first = ocr._helper()
    assert first.is_file()
    assert ocr._helper() == first
    assert len(commands) == 3
    assert commands[2][0] == "/path with spaces/swiftc"
    assert commands[2][commands[2].index("-sdk") + 1] == "/sdk with spaces"
    source.write_text("changed source")
    assert ocr._helper() != first
    assert len(commands) == 6


@pytest.mark.skipif(platform.system() != "Darwin", reason="Apple Vision requires macOS")
@pytest.mark.parametrize("exif_rotated", [False, True])
def test_native_printed_page_and_exif_geometry(tmp_path, monkeypatch, exif_rotated):
    monkeypatch.setenv("MEDIA_SEARCH_HOME", str(tmp_path / "runtime"))
    page = Image.new("RGB", (1800, 1100), "white")
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 72)
    draw = ImageDraw.Draw(page)
    draw.text((130, 120), "LOCAL PAGE CHECK ALPHA 123", font=font, fill="black")
    draw.text((130, 400), "SECOND LINE BRAVO 456", font=font, fill="black")
    path = tmp_path / "printed page.jpg"
    if exif_rotated:
        # Store pixels counterclockwise; EXIF 6 restores clockwise display orientation.
        exif = Image.Exif()
        exif[274] = 6
        page.transpose(Image.Transpose.ROTATE_90).save(path, quality=95, exif=exif)
    else:
        page.save(path, quality=95)
    result = ocr.read_page(path)
    assert (result["image"]["width"], result["image"]["height"]) == (1800, 1100)
    assert result["recognition"]["language_correction"] is False
    assert result["coordinate_space"] == "normalized_exif_oriented_image_top_left_x_right_y_down"
    first = next(line for line in result["lines"] if "LOCAL PAGE CHECK ALPHA 123" in line["text"])
    second = next(line for line in result["lines"] if "SECOND LINE BRAVO 456" in line["text"])
    box = first["bounding_box"]
    assert 0.05 < box["x"] < 0.1
    assert 0.09 < box["y"] < 0.2
    assert box["width"] > 0.5
    assert 0.02 < box["height"] < 0.1
    assert second["bounding_box"]["y"] > box["y"]
    assert first["source"] == "apple-vision"
    assert 0 <= first["confidence"] <= 1
    assert "not calibrated" in result["confidence_semantics"]
