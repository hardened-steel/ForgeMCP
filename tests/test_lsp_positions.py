"""Position encoding and source-boundary tests for the LSP adapter."""

from __future__ import annotations

import pytest

from forgemcp.lsp import LspCoordinateError, PositionEncoding, from_lsp_position, to_lsp_position
from forgemcp.models import Position


def test_lsp_position_conversion_preserves_public_code_point_columns_for_non_bmp_text():
    text = "auto😀name\n"
    position = Position(line=0, column=5)

    assert to_lsp_position(text, position, PositionEncoding.UTF8) == {"line": 0, "character": 8}
    assert to_lsp_position(text, position, PositionEncoding.UTF16) == {"line": 0, "character": 6}
    assert to_lsp_position(text, position, PositionEncoding.UTF32) == {"line": 0, "character": 5}
    assert from_lsp_position(text, {"line": 0, "character": 6}, PositionEncoding.UTF16) == position
    with pytest.raises(LspCoordinateError, match="splits"):
        from_lsp_position(text, {"line": 0, "character": 5}, PositionEncoding.UTF16)
