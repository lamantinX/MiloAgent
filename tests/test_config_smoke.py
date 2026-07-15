"""Offline smoke test for committed YAML config templates.

Parses every checked-in config/*.yaml and projects/*.yaml with yaml.safe_load
and asserts each is a mapping. Local override files (*.local.yaml) are never
loaded — they may contain real secrets and are gitignored.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
PROJECTS_DIR = REPO_ROOT / "projects"

# Every file committed under these dirs. We do NOT glob *.local.yaml — those
# are gitignored user overrides and are explicitly out of scope.
TEMPLATE_PATHS = sorted(
    [p for p in CONFIG_DIR.glob("*.yaml") if not p.name.endswith(".local.yaml")]
    + [p for p in CONFIG_DIR.glob("*.yml") if not p.name.endswith(".local.yml")]
    + [p for p in PROJECTS_DIR.glob("*.yaml") if not p.name.endswith(".local.yaml")]
    + [p for p in PROJECTS_DIR.glob("*.yml") if not p.name.endswith(".local.yml")]
)


def test_committed_templates_exist():
    """Guards against the template list silently becoming empty."""
    assert TEMPLATE_PATHS, "expected at least one committed YAML template"


def test_every_committed_template_parses_to_mapping():
    """Each committed template must parse to a YAML mapping (dict)."""
    parsed = []
    for path in TEMPLATE_PATHS:
        with open(path, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        assert isinstance(doc, dict), f"{path.relative_to(REPO_ROOT)} did not parse to a mapping"
        parsed.append((path, doc))
    # sanity: we actually exercised the loop body
    assert len(parsed) == len(TEMPLATE_PATHS)


def test_no_local_override_files_are_loaded():
    """*.local.yaml files are never part of the committed template set."""
    for path in TEMPLATE_PATHS:
        assert ".local." not in path.name, f"local override leaked into template set: {path}"
