import re
import subprocess
import sys
from importlib.metadata import entry_points, version
from pathlib import Path

import nodeartifact

ROOT = Path(__file__).parents[1]
SERVER_MODULES = ("typer", "starlette", "jinja2", "uvicorn")
DOCS_URL = "https://nodestep-ai.github.io/nodestep/nodeartifact/"
RELEASE_HEADING = re.compile(
    r"^## \[([0-9][^\]]*)\] - [0-9]{4}-[0-9]{2}-[0-9]{2}$", re.MULTILINE
)
STATUS_NOTE = [
    "> [!WARNING]",
    "> Alpha (0.1.0a1). Anything may change between releases without a "
    "deprecation period, so pin a tag or a commit.",
]


def test_version_matches_installed_metadata():
    assert nodeartifact.__version__ == version("nodeartifact")


def test_import_does_not_load_server_modules():
    code = f"import sys, nodeartifact; print([name for name in {SERVER_MODULES!r} if name in sys.modules])"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        encoding="utf-8",
    )
    assert result.stdout.strip() == "[]"


def test_console_script_points_at_cli_main():
    (script,) = entry_points(group="console_scripts", name="nodeartifact")
    assert script.value == "nodeartifact.cli:main"


def test_version_is_the_first_alpha():
    assert nodeartifact.__version__ == "0.1.0a1"


def test_readme_opens_with_the_logo_the_title_and_the_alpha_status_note():
    root = Path(__file__).parents[1]
    lines = (root / "README.md").read_text(encoding="utf-8").splitlines()

    assert lines[:6] == [
        "<picture>",
        '  <source media="(prefers-color-scheme: dark)" srcset="assets/nodeartifact-dark.svg">',
        '  <img src="assets/nodeartifact.svg" alt="" width="56">',
        "</picture>",
        "",
        "# nodeartifact",
    ]
    assert lines[7:9] == STATUS_NOTE
    for name in ("nodeartifact.svg", "nodeartifact-dark.svg"):
        assert (root / "assets" / name).read_text(encoding="utf-8").startswith("<svg ")


def test_readme_links_to_the_documentation():
    assert DOCS_URL in (ROOT / "README.md").read_text(encoding="utf-8")


def test_changelog_newest_release_is_the_package_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert text.startswith("# Changelog\n\n## [")
    assert "[Unreleased]" not in text
    assert RELEASE_HEADING.findall(text)[0] == nodeartifact.__version__
    assert (
        f"[{nodeartifact.__version__}]: https://github.com/nodestep-ai/nodeartifact"
        f"/releases/tag/v{nodeartifact.__version__}"
    ) in text.splitlines()
