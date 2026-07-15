"""Tests for BusinessManager business/product loading, validation, and compat.

Uses only safe example fixtures written into tmp dirs. No real .local.yaml.
"""

from pathlib import Path

import pytest
import yaml

from core.business_manager import (
    BusinessManager,
    BusinessManagerError,
    is_valid_slug,
)


# ── helpers ──────────────────────────────────────────────────────────────

def _write_biz(biz_dir: Path, biz_id: str, name: str = None, enabled: bool = True):
    biz_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "business": {
            "id": biz_id,
            "name": name or biz_id.replace("_", " ").title(),
            "description": "safe test business",
            "enabled": enabled,
        }
    }
    (biz_dir / f"{biz_id}.yaml").write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _write_product(
    proj_dir: Path,
    name: str,
    product_id: str = None,
    business_id: str = None,
    enabled: bool = True,
    weight: float = 1.0,
    unicode_name: str = None,
):
    proj_dir.mkdir(parents=True, exist_ok=True)
    project = {
        "name": unicode_name if unicode_name is not None else name,
        "url": f"https://{name.lower()}.example.com",
        "type": "SaaS",
        "description": "safe test product",
        "weight": weight,
        "enabled": enabled,
    }
    if product_id is not None:
        project["id"] = product_id
    if business_id is not None:
        project["business_id"] = business_id
    data = {"project": project}
    (proj_dir / f"{name.lower()}.yaml").write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False,
                  allow_unicode=True),
        encoding="utf-8",
    )


# ── slug validation ──────────────────────────────────────────────────────

def test_is_valid_slug_accepts_legal_slugs():
    assert is_valid_slug("ab")
    assert is_valid_slug("a1")
    assert is_valid_slug("my-cool-product")
    assert is_valid_slug("biz_123")
    assert is_valid_slug("x" * 64)


def test_is_valid_slug_rejects_illegal_slugs():
    assert not is_valid_slug("")
    assert not is_valid_slug(None)
    assert not is_valid_slug("A")          # uppercase
    assert not is_valid_slug("1")          # too short (need >=2)
    assert not is_valid_slug("-ab")        # leading hyphen
    assert not is_valid_slug("ab!")        # illegal char
    assert not is_valid_slug("x" * 65)     # too long
    assert not is_valid_slug("café")       # non-ascii


# ── load: valid relationships ────────────────────────────────────────────

def test_load_valid_business_and_products(tmp_path):
    biz_dir = tmp_path / "businesses"
    proj_dir = tmp_path / "projects"
    _write_biz(biz_dir, "acme")
    _write_product(proj_dir, "Widget", "widget", "acme")
    _write_product(proj_dir, "Gadget", "gadget", "acme")

    mgr = BusinessManager(str(proj_dir), str(biz_dir))
    try:
        assert [b["id"] for b in mgr.businesses] == ["acme"]
        products = mgr.get_products("acme")
        assert {p["project"]["id"] for p in products} == {"widget", "gadget"}
        assert mgr.get_business("acme") is not None
        assert mgr.get_business("nope") is None
    finally:
        mgr.stop_watching()


def test_load_disabled_entries_excluded(tmp_path):
    biz_dir = tmp_path / "businesses"
    proj_dir = tmp_path / "projects"
    _write_biz(biz_dir, "acme", enabled=False)
    _write_biz(biz_dir, "beta")
    _write_product(proj_dir, "Widget", "widget", "beta")
    _write_product(proj_dir, "Ghost", "ghost", "beta", enabled=False)

    mgr = BusinessManager(str(proj_dir), str(biz_dir))
    try:
        assert [b["id"] for b in mgr.businesses] == ["beta"]
        products = mgr.get_products("beta")
        # 'ghost' is disabled -> excluded
        assert [p["project"]["id"] for p in products] == ["widget"]
    finally:
        mgr.stop_watching()


# ── validate: duplicate / unknown ids fail ───────────────────────────────

def test_duplicate_product_ids_fail(tmp_path):
    biz_dir = tmp_path / "businesses"
    proj_dir = tmp_path / "projects"
    _write_biz(biz_dir, "acme")
    _write_product(proj_dir, "Widget", "widget", "acme")
    _write_product(proj_dir, "Other", "widget", "acme")  # dup id
    with pytest.raises(BusinessManagerError, match="Duplicate product id"):
        BusinessManager(str(proj_dir), str(biz_dir))


def test_duplicate_business_ids_fail(tmp_path):
    biz_dir = tmp_path / "businesses"
    proj_dir = tmp_path / "projects"
    _write_biz(biz_dir, "acme")
    (biz_dir / "acme_too.yaml").write_text(
        yaml.dump({"business": {"id": "acme", "name": "Dup"}}, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(BusinessManagerError, match="Duplicate business id"):
        BusinessManager(str(proj_dir), str(biz_dir))


def test_unknown_business_id_fails(tmp_path):
    biz_dir = tmp_path / "businesses"
    proj_dir = tmp_path / "projects"
    _write_biz(biz_dir, "acme")
    _write_product(proj_dir, "Widget", "widget", "ghost")  # unknown biz
    with pytest.raises(BusinessManagerError, match="unknown business_id"):
        BusinessManager(str(proj_dir), str(biz_dir))


def test_invalid_product_id_fails(tmp_path):
    biz_dir = tmp_path / "businesses"
    proj_dir = tmp_path / "projects"
    _write_biz(biz_dir, "acme")
    _write_product(proj_dir, "Widget", "Bad ID!", "acme")  # illegal slug
    with pytest.raises(BusinessManagerError):
        BusinessManager(str(proj_dir), str(biz_dir))


# ── legacy compatibility ─────────────────────────────────────────────────

def test_legacy_single_business_compat_loads(tmp_path):
    """Legacy product without ids loads under the single configured business."""
    biz_dir = tmp_path / "businesses"
    proj_dir = tmp_path / "projects"
    _write_biz(biz_dir, "acme")
    # No id / no business_id on the product:
    _write_product(proj_dir, "OldProduct")

    mgr = BusinessManager(str(proj_dir), str(biz_dir))
    try:
        products = mgr.projects
        assert len(products) == 1
        proj = products[0]["project"]
        assert proj["business_id"] == "acme"
        # An id is synthesized so downstream code has a stable handle.
        assert is_valid_slug(proj["id"])
    finally:
        mgr.stop_watching()


def test_legacy_multi_business_is_hard_error(tmp_path):
    """Legacy products with multiple businesses configured is ambiguous."""
    biz_dir = tmp_path / "businesses"
    proj_dir = tmp_path / "projects"
    _write_biz(biz_dir, "acme")
    _write_biz(biz_dir, "beta")
    _write_product(proj_dir, "OldProduct")  # no ids

    with pytest.raises(BusinessManagerError, match="legacy product"):
        BusinessManager(str(proj_dir), str(biz_dir))


# ── add_business ─────────────────────────────────────────────────────────

def test_add_business_creates_file(tmp_path):
    biz_dir = tmp_path / "businesses"
    proj_dir = tmp_path / "projects"
    mgr = BusinessManager(str(proj_dir), str(biz_dir))
    try:
        path = mgr.add_business("acme", "Acme", "desc")
        assert Path(path).exists()
        assert mgr.get_business("acme") is not None
    finally:
        mgr.stop_watching()


def test_add_business_rejects_invalid_id(tmp_path):
    mgr = BusinessManager(str(tmp_path / "projects"), str(tmp_path / "businesses"))
    try:
        with pytest.raises(ValueError, match="Invalid business id"):
            mgr.add_business("Bad ID!", "Acme")
    finally:
        mgr.stop_watching()


def test_add_business_rejects_duplicate(tmp_path):
    biz_dir = tmp_path / "businesses"
    mgr = BusinessManager(str(tmp_path / "projects"), str(biz_dir))
    try:
        mgr.add_business("acme", "Acme")
        with pytest.raises(ValueError, match="already exists"):
            mgr.add_business("acme", "Acme Again")
    finally:
        mgr.stop_watching()


# ── unicode round-trip (no BOM) ──────────────────────────────────────────

def test_unicode_display_name_round_trips_utf8_no_bom(tmp_path):
    biz_dir = tmp_path / "businesses"
    proj_dir = tmp_path / "projects"
    _write_biz(biz_dir, "acme")
    _write_product(proj_dir, "Cafe", "cafe", "acme", unicode_name="Café Bryllaup")

    mgr = BusinessManager(str(proj_dir), str(biz_dir))
    try:
        raw = (proj_dir / "cafe.yaml").read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), "file must not start with BOM"
        products = mgr.projects
        assert products[0]["project"]["name"] == "Café Bryllaup"
    finally:
        mgr.stop_watching()
