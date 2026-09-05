from forgemcp.assets import IconFile, Widget


def test_widget_reads_package_relative_html() -> None:
    widget = Widget("assets/workspace-overview.html")

    assert widget.uri == "ui://forgemcp/workspace-overview.html"
    assert "<!doctype html>" in widget.content


def test_icon_reads_separate_svg_file() -> None:
    icon = IconFile("icons/workspace.svg").icon

    assert icon.src.startswith("data:image/svg+xml;base64,")
    assert icon.mime_type == "image/svg+xml"
    assert icon.sizes == ["any"]
