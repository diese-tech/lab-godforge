from utils import formatter


def test_help_footer_uses_central_release_version():
    page1 = formatter.format_help_page1().to_dict()
    page2 = formatter.format_help_page2().to_dict()

    assert formatter.GODFORGE_VERSION == "2.2.0"
    assert f"GodForge v{formatter.GODFORGE_VERSION}" in page1["footer"]["text"]
    assert f"GodForge v{formatter.GODFORGE_VERSION}" in page2["footer"]["text"]
    assert "VERSION_HISTORY.md" in page1["footer"]["text"]
    assert "VERSION_HISTORY.md" in page2["footer"]["text"]
