"""read_image: attach a workspace image to the conversation (MS-6).

A READ tool with the fs_read plumbing: the model names a screenshot or
diagram, the tool base64-encodes the bytes into ``ToolResult.data`` (the
harness-only channel), and the ENGINE — the one layer that imports both
tools and providers — turns that payload into a ``Message.images``
attachment on the next call. The tool itself never touches provider
types (tools/ may not import providers/ or envelope/).

Degrade is honest and fail-closed: ``vision_check`` (a plain callable seam
the engine late-binds to its capability check) gating False — or never
being wired at all — returns a clear ok=False error naming the config
override, so a text-only model gets truth instead of a hallucination.

SAFETY note: image bytes bypass ``redact_context`` (binary payloads cannot
be text-scanned) and visual prompt injection is invisible to
``detect_injection``. Acceptable for a model/user-initiated local READ
inside the jail — the engine's out-of-workspace READ escalation still
applies to the ``path`` argument — but do not widen this tool to URLs.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ironcore.tools.base import ToolResult
from ironcore.tools.fs_read import _FsReadTool

#: hard cap on the raw file size; a data-URI of a bigger file would blow the
#: request body long before it helped the model.
MAX_IMAGE_BYTES = 5_000_000

#: magic-byte signatures -> MIME type. Sniffed, never trusted from the
#: extension: the media_type goes verbatim into the wire data URI.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _fail(reason: str) -> ToolResult:
    """A failed image read whose reason is visible to BOTH the model and the UI.

    ``output`` is the only channel the engine feeds back to the model; ``error``
    is user/UI-facing. Every read_image failure carries the same actionable text
    on both so the model never gets an empty result to blind-retry against.
    """
    return ToolResult(ok=False, output=reason, error=reason)


def _sniff_media_type(head: bytes) -> str | None:
    """PNG/JPEG/GIF/WEBP magic-byte sniff; None for anything else."""
    for signature, media_type in _SIGNATURES:
        if head.startswith(signature):
            return media_type
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


class ReadImageTool(_FsReadTool):
    """Attach a workspace image (PNG/JPEG/GIF/WEBP) for the model to see."""

    name = "read_image"
    description = (
        "Attach an image file (a screenshot, diagram, or picture) so you can see it. "
        "Supported: PNG, JPEG, GIF, WEBP. The image becomes visible on your next "
        "response. Example: read_image(path='docs/architecture.png')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative image path, e.g. 'shots/error.png'.",
            },
        },
        "required": ["path"],
    }

    def __init__(
        self, workspace: Path, vision_check: Callable[[], bool] | None = None
    ) -> None:
        super().__init__(workspace)
        #: capability seam: the engine late-binds this to its vision check when
        #: it is still None. None or a False return = honest degrade, no bytes.
        self.vision_check = vision_check

    async def run(self, **kwargs: Any) -> ToolResult:
        # Every failure reason rides OUTPUT as well as ERROR. The engine feeds
        # only OUTPUT back to the model (error is user/UI-facing), so a failed
        # read that left output empty handed the model a blank result and it
        # blind-retried the same doomed call; mirroring the actionable reason
        # lets it self-correct (pick another path, fix the format, give up).
        path = kwargs.get("path")
        if not isinstance(path, str) or not path:
            return _fail("'path' (string) is required")
        if self.vision_check is None or not self.vision_check():
            # the whole point of this branch is that the MODEL learns the truth
            # instead of hallucinating what the file "shows".
            return _fail(
                "this model has no vision capability - read_image cannot attach "
                "images (probe/seed detected none; set [envelope] vision = true "
                "in config to override)"
            )
        target = self._resolve(path)
        try:
            raw = target.read_bytes()
        except FileNotFoundError:
            return _fail(f"file not found: {self._rel(target)}")
        except OSError as exc:
            return _fail(f"cannot read {self._rel(target)}: {exc}")
        if len(raw) > MAX_IMAGE_BYTES:
            return _fail(
                f"{self._rel(target)} is {len(raw):,} bytes; read_image caps images "
                f"at {MAX_IMAGE_BYTES:,} bytes"
            )
        media_type = _sniff_media_type(raw[:16])
        if media_type is None:
            return _fail(
                f"{self._rel(target)} is not a supported image "
                "(PNG, JPEG, GIF, or WEBP magic bytes expected)"
            )
        rel = self._rel(target)
        return ToolResult(
            ok=True,
            output=(
                f"attached image {rel} ({media_type}, {len(raw):,} bytes); "
                "it will be visible to the model on the next call"
            ),
            # plain dicts, not provider types: the engine converts to ImageData
            # (tools/ must not import providers/).
            data={
                "images": [
                    {
                        "base64": base64.b64encode(raw).decode("ascii"),
                        "media_type": media_type,
                    }
                ]
            },
        )
