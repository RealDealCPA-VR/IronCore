"""ReadImageTool (MS-6): base64 payload, magic-byte sniffing, size cap, and the
honest vision_check degrade. All offline against tmp_path files; async pattern
is asyncio.run (pytest-asyncio is not a dependency of this repo)."""

import asyncio
import base64

from ironcore.safety.risk import ToolRisk
from ironcore.tools import image as image_mod
from ironcore.tools.image import MAX_IMAGE_BYTES, ReadImageTool

# a real 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR4nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="
)
JPEG_HEAD = b"\xff\xd8\xff\xe0" + b"\x00" * 20
GIF_HEAD = b"GIF89a" + b"\x00" * 20
WEBP_HEAD = b"RIFF\x24\x00\x00\x00WEBP" + b"\x00" * 20


def run(tool, **kwargs):
    return asyncio.run(tool.run(**kwargs))


def _tool(tmp_path, vision=True):
    return ReadImageTool(tmp_path, vision_check=(lambda: vision))


# --- contract -----------------------------------------------------------------


def test_risk_is_read_and_spec_is_schema_valid(tmp_path):
    tool = _tool(tmp_path)
    assert tool.risk is ToolRisk.READ
    spec = tool.spec()
    assert spec["type"] == "function"
    fn = spec["function"]
    assert fn["name"] == "read_image"
    assert "Example:" in fn["description"]
    params = fn["parameters"]
    assert params["type"] == "object"
    assert set(params["required"]) == {"path"}
    assert set(params["required"]) <= set(params["properties"])


def test_read_is_side_effect_free(tmp_path):
    target = tmp_path / "shot.png"
    target.write_bytes(PNG)
    before = target.read_bytes()
    run(_tool(tmp_path), path="shot.png")
    assert target.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["shot.png"]


# --- happy path ---------------------------------------------------------------


def test_png_round_trips_through_the_base64_payload(tmp_path):
    (tmp_path / "shot.png").write_bytes(PNG)
    result = run(_tool(tmp_path), path="shot.png")
    assert result.ok
    payload = result.data["images"]
    assert len(payload) == 1
    assert payload[0]["media_type"] == "image/png"
    assert base64.b64decode(payload[0]["base64"]) == PNG
    assert "attached image shot.png" in result.output
    assert "visible to the model on the next call" in result.output


def test_sniffs_jpeg_gif_and_webp_headers(tmp_path):
    cases = [
        ("a.jpg", JPEG_HEAD, "image/jpeg"),
        ("b.gif", GIF_HEAD, "image/gif"),
        ("c.webp", WEBP_HEAD, "image/webp"),
    ]
    for name, head, media_type in cases:
        (tmp_path / name).write_bytes(head)
        result = run(_tool(tmp_path), path=name)
        assert result.ok, (name, result.error)
        assert result.data["images"][0]["media_type"] == media_type


# --- degrade + user errors ----------------------------------------------------


def test_unwired_vision_check_degrades_honestly(tmp_path):
    (tmp_path / "shot.png").write_bytes(PNG)
    result = run(ReadImageTool(tmp_path), path="shot.png")  # vision_check=None
    assert not result.ok
    assert "no vision capability" in result.error
    assert "[envelope] vision" in result.error  # names the override knob
    # the engine feeds only output back to the model: the truth must ride there
    assert "no vision capability" in result.output
    assert result.data == {}  # nothing image-shaped leaves the tool


def test_vision_check_false_degrades_honestly(tmp_path):
    (tmp_path / "shot.png").write_bytes(PNG)
    result = run(_tool(tmp_path, vision=False), path="shot.png")
    assert not result.ok
    assert "no vision capability" in result.error


def test_non_image_file_is_a_clear_error(tmp_path):
    (tmp_path / "notes.txt").write_text("just text", encoding="utf-8")
    result = run(_tool(tmp_path), path="notes.txt")
    assert not result.ok
    assert "not a supported image" in result.error


def test_missing_file_is_a_clear_error(tmp_path):
    result = run(_tool(tmp_path), path="ghost.png")
    assert not result.ok
    assert "file not found" in result.error


def test_oversize_image_error_names_the_size(tmp_path, monkeypatch):
    (tmp_path / "big.png").write_bytes(PNG)
    monkeypatch.setattr(image_mod, "MAX_IMAGE_BYTES", 10)
    result = run(_tool(tmp_path), path="big.png")
    assert not result.ok
    assert f"{len(PNG):,} bytes" in result.error
    assert MAX_IMAGE_BYTES > 10  # the real cap is untouched


def test_missing_path_argument(tmp_path):
    result = run(_tool(tmp_path))
    assert not result.ok
    assert "'path'" in result.error


# --- every failure reason is visible to the MODEL, not only the UI -----------
# The engine feeds only ToolResult.output back to the model (error is UI-facing).
# A failure that leaves output empty hands the model a blank result to blind-
# retry against; each branch must mirror its actionable reason into output too.


def test_missing_file_reason_reaches_the_model(tmp_path):
    result = run(_tool(tmp_path), path="ghost.png")
    assert not result.ok
    assert result.output  # not the empty string the model used to get
    assert "file not found" in result.output
    assert result.output == result.error  # same reason on both channels


def test_unsupported_format_reason_reaches_the_model(tmp_path):
    (tmp_path / "notes.txt").write_text("just text", encoding="utf-8")
    result = run(_tool(tmp_path), path="notes.txt")
    assert not result.ok
    assert "not a supported image" in result.output
    assert result.output == result.error


def test_oversize_reason_reaches_the_model(tmp_path, monkeypatch):
    (tmp_path / "big.png").write_bytes(PNG)
    monkeypatch.setattr(image_mod, "MAX_IMAGE_BYTES", 10)
    result = run(_tool(tmp_path), path="big.png")
    assert not result.ok
    assert f"{len(PNG):,} bytes" in result.output
    assert "caps images" in result.output
    assert result.output == result.error


def test_missing_path_reason_reaches_the_model(tmp_path):
    result = run(_tool(tmp_path))
    assert not result.ok
    assert "'path'" in result.output
    assert result.output == result.error
