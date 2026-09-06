from forgemcp.assets import IconFile, Widget
from forgemcp.toolchain.service import ToolchainService


def test_widget_reads_package_relative_html() -> None:
    widget = Widget("assets/workspace-overview.html")

    assert widget.uri == "ui://forgemcp/workspace-overview.html"
    assert "<!doctype html>" in widget.content


def test_icon_reads_separate_svg_file() -> None:
    icon = IconFile("icons/workspace.svg").icon

    assert icon.src.startswith("data:image/svg+xml;base64,")
    assert icon.mime_type == "image/svg+xml"
    assert icon.sizes == ["any"]


def test_toolset_widget_and_icon_are_packaged():
    html = ToolchainService.WIDGET.content
    assert "<!doctype html>" in html
    assert "Available toolsets" in html
    assert "toolset_get" in html and "toolsets_list" in html
    assert ToolchainService.ICON.icon.mime_type == "image/svg+xml"
