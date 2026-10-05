import re
from importlib.resources import files
from pathlib import Path

import pytest

SERVER = files("nodeartifact.server")
STATIC = Path(str(SERVER.joinpath("static")))
TEMPLATES = Path(str(SERVER.joinpath("templates")))
NODESTEP_CSS = STATIC / "nodestep.css"
APP_CSS = STATIC / "style.css"
FAVICON = STATIC / "favicon.svg"
README_LOGO = Path(__file__).parents[2] / "assets" / "nodeartifact.svg"
README_LOGO_DARK = README_LOGO.with_name("nodeartifact-dark.svg")
SOURCES = sorted(
    [*STATIC.glob("*.css"), *STATIC.glob("*.js"), *TEMPLATES.glob("*.html")]
)
HEX = re.compile(r"(?:#|%23)([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
FUNCTION = re.compile(r"\b(rgba?|hsla?|oklch|oklab|lab|lch|hwb)\(", re.IGNORECASE)


def light_accent() -> str:
    match = re.search(
        r"--nodestep-color-accent: light-dark\(#([0-9a-f]{6}), ",
        NODESTEP_CSS.read_text(encoding="utf-8"),
    )
    assert match is not None
    return match.group(1)


def token_values() -> set[str]:
    declarations = re.findall(
        r"--nodestep-color-[a-z-]+: light-dark\(#([0-9a-f]{6}), #([0-9a-f]{6})\);",
        NODESTEP_CSS.read_text(encoding="utf-8"),
    )
    return {value for pair in declarations for value in pair}


def test_vendored_nodestep_design_is_the_pinned_version():
    assert '--nodestep-design-version: "0.1.0a1";' in NODESTEP_CSS.read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    "path",
    [path for path in SOURCES if path != NODESTEP_CSS],
    ids=lambda path: path.name,
)
def test_only_nodestep_design_writes_colors(path):
    text = path.read_text(encoding="utf-8")

    assert HEX.findall(text) == []
    assert FUNCTION.findall(text) == []


@pytest.mark.parametrize(
    "path", [FAVICON, README_LOGO, README_LOGO_DARK], ids=lambda path: path.name
)
def test_logo_files_use_only_nodestep_design_token_colors(path):
    text = path.read_text(encoding="utf-8")
    colors = {value.lower() for value in HEX.findall(text)}

    assert light_accent() in colors
    assert colors <= token_values()
    assert FUNCTION.findall(text) == []


def test_the_favicon_follows_the_color_scheme():
    assert "prefers-color-scheme: dark" in FAVICON.read_text(encoding="utf-8")


def test_the_readme_logos_have_one_ink_each_for_the_picture_element():
    light = README_LOGO.read_text(encoding="utf-8")
    dark = README_LOGO_DARK.read_text(encoding="utf-8")

    assert "prefers-color-scheme" not in light
    assert "prefers-color-scheme" not in dark
    assert "#1f1b17" in light
    assert "#ede9e4" in dark
    assert light.replace("#1f1b17", "#ede9e4") == dark


def test_app_stylesheet_uses_tokens_and_declares_none_of_its_own_colors():
    text = APP_CSS.read_text(encoding="utf-8")

    assert "--nodestep-color-" not in re.sub(
        r"var\(--nodestep-color-[a-z-]+\)", "", text
    )
    assert set(re.findall(r"var\((--nodestep-color-[a-z-]+)\)", text)) <= set(
        re.findall(
            r"(--nodestep-color-[a-z-]+):", NODESTEP_CSS.read_text(encoding="utf-8")
        )
    )


def test_no_style_attributes_in_the_templates():
    for path in TEMPLATES.glob("*.html"):
        assert " style=" not in path.read_text(encoding="utf-8"), path.name


def test_mermaid_edge_labels_and_stacked_badges_come_from_nodestep_design():
    shared = NODESTEP_CSS.read_text(encoding="utf-8")
    own = APP_CSS.read_text(encoding="utf-8")

    assert (
        ".nodestep-mermaid :is(.edgeLabel, .edgeLabel p, .edgeLabel rect, .labelBkg) {"
        in shared
    )
    assert ".nodestep-table-stack .nodestep-badge {" in shared
    assert "edgeLabel" not in own
    assert "justify-self" not in own


def test_the_vendored_files_match_nodestep_stylesheet_when_it_is_checked_out():
    stylesheet = Path(__file__).resolve().parents[3] / "nodestep-stylesheet"
    if not stylesheet.is_dir():
        pytest.skip("nodestep-stylesheet is not checked out next to nodeartifact")
    for name in ("nodestep.css", "nodestep-theme.js", "nodestep-data.js"):
        assert (STATIC / name).read_bytes() == (stylesheet / name).read_bytes(), name


def test_the_app_stylesheet_leaves_the_shared_parts_to_nodestep_design():
    own = APP_CSS.read_text(encoding="utf-8")

    for name in (
        ".crumbs",
        ".facts",
        ".pairs",
        ".views",
        ".nowrap",
        ".wrap",
        ".events pre",
    ):
        assert name not in own, name
    assert not (STATIC / "theme.js").exists()
    assert not (STATIC / "theme-toggle.js").exists()


def test_the_templates_use_the_shared_navigation_and_data_classes():
    trace = (TEMPLATES / "trace.html").read_text(encoding="utf-8")
    span = (TEMPLATES / "span.html").read_text(encoding="utf-8")
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    for page in (trace, span):
        assert '<nav aria-label="Breadcrumb"><ol class="nodestep-breadcrumbs">' in page
        assert '<dl class="nodestep-facts">' in page
    assert '<ul class="nodestep-tabs">' in trace
    assert trace.count('class="nodestep-tab"') == 2
    assert '<section class="nodestep-section' in span
    assert '<main class="nodestep-main nodestep-stack" id="content"' in base


def test_the_trace_list_centers_each_row_vertically():
    own = APP_CSS.read_text(encoding="utf-8")

    rule = re.search(r"\.traces td \{([^}]*)\}", own)
    assert rule is not None
    assert "vertical-align: middle;" in rule.group(1)


def test_the_search_field_takes_its_look_from_nodestep_design_only():
    own = APP_CSS.read_text(encoding="utf-8")

    for selector in (".trace-search", ".trace-list-head", ".nodestep-search"):
        assert selector not in own
    assert ".nodestep-search {" in NODESTEP_CSS.read_text(encoding="utf-8")
