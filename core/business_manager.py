"""Business and product manager with hot-reload and CRUD operations.

Concepts:
- A *business* is the owning entity (one or more products). Business IDs are
  immutable lowercase slugs stored in businesses/*.yaml.
- A *product* is what used to be called a "project". Product YAML still lives in
  projects/*.yaml and a `project:` block is kept for compatibility, but it now
  carries an immutable `id` (the product slug) and a `business_id`.

Historical project files that lack `id`/`business_id` load only in a single
*compatibility mode* (one business, one warning). More than one business with
any legacy product is a hard error.
"""

import os
import re
import time
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, Callable, Tuple

import yaml

logger = logging.getLogger(__name__)

# Immutable slug for business_id / product id: ^[a-z0-9][a-z0-9_-]{1,63}$
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")

# The single business used to host legacy project files without explicit ids
# during compatibility mode. Lowercase slug, never collides with real ids.
_COMPAT_BUSINESS_ID = "default"

_COMPAT_WARNING_EMITTED = False


def is_valid_slug(slug: str) -> bool:
    """Return True if slug matches the immutable id grammar."""
    return bool(slug) and isinstance(slug, str) and bool(SLUG_RE.match(slug))


def _slugify(name: str) -> str:
    """Derive a best-effort slug from a display name (lowercase, [a-z0-9_-]).

    Used only for *generating new file names / new ids from human input* — never
    to remap existing historical project names (the migrator does that).
    """
    s = name.strip().lower()
    # Replace any run of non [a-z0-9_-] with a single underscore.
    s = re.sub(r"[^a-z0-9_-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        return "product"
    # Guarantee the leading char is alphanumeric (slug grammar requirement).
    if not s[0].isalnum():
        s = "p" + s
    return s[:64]


class BusinessManagerError(Exception):
    """Raised for unrecoverable business/product registry errors."""


class BusinessManager:
    """Manages business + product YAML files with hot-reload capability.

    Features:
    - Load businesses from businesses/*.yaml and products from projects/*.yaml
    - Validate business_id references and slug grammar
    - Watch for file changes (add/edit/delete) via mtime polling
    - CRUD operations for projects (kept as a compatibility alias for products)
    - Thread-safe access to registries
    - Callbacks for reload notification
    """

    def __init__(
        self,
        projects_dir: str = "projects/",
        businesses_dir: str = "businesses/",
    ):
        self.projects_dir = Path(projects_dir)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.businesses_dir = Path(businesses_dir)
        self.businesses_dir.mkdir(parents=True, exist_ok=True)
        self._projects: List[Dict] = []
        self._businesses: List[Dict] = []
        self._file_mtimes: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._watcher_thread: Optional[threading.Thread] = None
        self._watching = False
        self._on_reload_callbacks: List[Callable] = []

        # Initial load
        self.reload()

    # ── Public accessors ──────────────────────────────────────────────

    @property
    def projects(self) -> List[Dict]:
        """Thread-safe access to current products list (compat alias)."""
        with self._lock:
            return list(self._projects)

    @property
    def businesses(self) -> List[Dict]:
        """Thread-safe access to current businesses list."""
        with self._lock:
            return list(self._businesses)

    def get_business(self, business_id: str) -> Optional[Dict]:
        """Get a business by id (case-sensitive; ids are immutable slugs)."""
        if not business_id:
            return None
        for b in self.businesses:
            if b.get("id") == business_id:
                return b
        return None

    def get_products(self, business_id: str) -> List[Dict]:
        """Get all enabled products that belong to a given business_id."""
        bid = business_id
        return [
            p for p in self.projects
            if p.get("project", {}).get("business_id") == bid
        ]

    def add_business(
        self,
        business_id: str,
        name: str,
        description: str = "",
        enabled: bool = True,
    ) -> str:
        """Create a new business YAML file. Returns the created filepath.

        Raises ValueError if the id is invalid or the file already exists.
        """
        if not is_valid_slug(business_id):
            raise ValueError(
                f"Invalid business id '{business_id}': must match "
                r"^[a-z0-9][a-z0-9_-]{1,63}$"
            )
        filepath = self.businesses_dir / f"{business_id}.yaml"
        if filepath.exists():
            raise ValueError(f"Business file already exists: {filepath}")

        data = {
            "business": {
                "id": business_id,
                "name": name,
                "description": description,
                "enabled": enabled,
            }
        }
        with open(filepath, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        self.reload()
        return str(filepath)

    # ── Reload / load ─────────────────────────────────────────────────

    def reload(self):
        """Reload all businesses + products from disk."""
        businesses, products, new_mtimes = self._load_all()
        self._apply_loaded(businesses, products, new_mtimes)

    def _load_all(self) -> Tuple[List[Dict], List[Dict], Dict[str, float]]:
        """Read businesses and products from disk without mutating state.

        Returns (businesses, products, file_mtimes). Raises BusinessManagerError
        on unrecoverable registry conflicts (duplicate ids, broken refs, or
        ambiguous multi-business legacy products).
        """
        businesses: List[Dict] = []
        products: List[Dict] = []
        new_mtimes: Dict[str, float] = {}

        # ── Businesses ────────────────────────────────────────────────
        for f in sorted(self.businesses_dir.glob("*.yaml")):
            try:
                new_mtimes[str(f)] = f.stat().st_mtime
                with open(f, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                biz = data.get("business") if isinstance(data, dict) else None
                if not isinstance(biz, dict):
                    continue
                bid = biz.get("id")
                if not is_valid_slug(bid):
                    logger.error(
                        f"Skipping business file {f}: invalid or missing id"
                    )
                    continue
                if not biz.get("enabled", True):
                    continue
                businesses.append({"id": bid, **biz})
            except Exception as e:
                logger.error(f"Error loading business file {f}: {e}")

        seen_biz: Dict[str, str] = {}
        for b in businesses:
            bid = b["id"]
            prior = seen_biz.get(bid)
            if prior is not None:
                raise BusinessManagerError(
                    f"Duplicate business id '{bid}' in files "
                    f"{prior} and {b.get('_file', '?')}"
                )
            seen_biz[bid] = b.get("_file", "?")

        # ── Products (legacy project files) ──────────────────────────
        legacy_products: List[Dict] = []   # missing id/business_id
        for f in sorted(self.projects_dir.glob("*.yaml")):
            try:
                new_mtimes[str(f)] = f.stat().st_mtime
                with open(f, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                if not data or not isinstance(data.get("project"), dict):
                    continue
                proj = data["project"]
                if not proj.get("enabled", True):
                    continue
                products.append(data)
            except Exception as e:
                logger.error(f"Error loading project file {f}: {e}")

        # Separate products with explicit ids from legacy ones.
        # - Legacy: 'id' absent/None (pre-migration file).
        # - Identified: 'id' present AND valid slug AND business_id set.
        # - An 'id' present but malformed, or a business_id set with no id,
        #   is a config error (not silently treated as legacy).
        identified: List[Dict] = []
        for p in products:
            proj = p["project"]
            pid = proj.get("id")
            bid = proj.get("business_id")
            id_provided = "id" in proj and pid is not None
            if not id_provided and not bid:
                legacy_products.append(p)
                continue
            if id_provided and not is_valid_slug(pid):
                raise BusinessManagerError(
                    f"Product '{proj.get('name', '?')}' has invalid id '{pid}': "
                    r"must match ^[a-z0-9][a-z0-9_-]{1,63}$"
                )
            if not is_valid_slug(pid) or not bid:
                # id missing but business_id present, or vice versa: ambiguous.
                raise BusinessManagerError(
                    f"Product '{proj.get('name', '?')}' has partial identity "
                    f"(id={pid!r}, business_id={bid!r}); set both explicitly "
                    "or remove both to use legacy compatibility."
                )
            identified.append(p)

        # Validate identified products: unique ids and resolvable business_id.
        seen_prod: Dict[str, str] = {}
        for p in identified:
            proj = p["project"]
            pid = proj["id"]
            bid = proj["business_id"]
            if prior := seen_prod.get(pid):
                raise BusinessManagerError(
                    f"Duplicate product id '{pid}' referenced by {prior} "
                    f"and another file"
                )
            seen_prod[pid] = pid
            if not is_valid_slug(bid):
                raise BusinessManagerError(
                    f"Product '{pid}' has invalid business_id '{bid}'"
                )
            if bid not in seen_biz:
                raise BusinessManagerError(
                    f"Product '{pid}' references unknown business_id '{bid}'"
                )

        # ── Legacy compatibility ─────────────────────────────────────
        if legacy_products:
            self._resolve_legacy_compat(
                legacy_products, businesses, identified
            )

        final_products = identified + legacy_products
        final_products.sort(
            key=lambda p: p.get("project", {}).get("weight", 1.0),
            reverse=True,
        )
        return businesses, final_products, new_mtimes

    def _resolve_legacy_compat(
        self,
        legacy_products: List[Dict],
        businesses: List[Dict],
        identified: List[Dict],
    ) -> None:
        """Assign the compatibility business to legacy products, or fail.

        Rules (Step 5 of plan 003):
        - If exactly one real business is configured, legacy products adopt it.
        - Otherwise legacy products adopt the synthetic 'default' business, but
          ONLY if there are no other businesses AND no identified products. Any
          hint of a second tenant with unmapped legacy data is a hard error.
        """
        global _COMPAT_WARNING_EMITTED
        biz_ids = {b["id"] for b in businesses}

        if len(businesses) == 1:
            sole_biz = businesses[0]["id"]
            for p in legacy_products:
                p["project"]["business_id"] = sole_biz
                if not p["project"].get("id"):
                    p["project"]["id"] = _slugify(p["project"].get("name", ""))
            if not _COMPAT_WARNING_EMITTED:
                logger.warning(
                    "Loaded %d legacy product(s) without explicit ids in "
                    "single-business compatibility mode (business '%s'). "
                    "Add explicit id/business_id to each product and run "
                    "'business migrate-legacy --apply'. (planned removal: 011)",
                    len(legacy_products),
                    sole_biz,
                )
                _COMPAT_WARNING_EMITTED = True
            return

        # Multiple (or zero) businesses with unmapped legacy products.
        raise BusinessManagerError(
            f"{len(legacy_products)} legacy product(s) lack explicit "
            "id/business_id while multiple businesses are configured "
            f"({sorted(biz_ids) or 'none'}). Resolve ownership explicitly "
            "by adding id/business_id to each product, or run "
            "'business migrate-legacy --apply' against a single business."
        )

    def _apply_loaded(
        self,
        businesses: List[Dict],
        products: List[Dict],
        new_mtimes: Dict[str, float],
    ):
        """Swap in freshly loaded state under the lock and fire callbacks."""
        with self._lock:
            old_pnames = {p["project"]["name"] for p in self._projects}
            new_pnames = {p["project"]["name"] for p in products}
            self._projects = products
            self._businesses = businesses
            self._file_mtimes = new_mtimes

        added = new_pnames - old_pnames
        removed = old_pnames - new_pnames
        if added:
            logger.info(f"Products added: {added}")
        if removed:
            logger.info(f"Products removed: {removed}")
        if not added and not removed and old_pnames:
            logger.info(
                f"Products reloaded: {[p['project']['name'] for p in products]}"
            )
        elif not old_pnames:
            logger.info(
                f"Loaded {len(products)} products, {len(businesses)} businesses"
            )

        for cb in self._on_reload_callbacks:
            try:
                cb(products)
            except Exception as e:
                logger.error(f"Reload callback error: {e}")

    # ── File Watcher ──────────────────────────────────────────────────

    def start_watching(self, interval: float = 5.0):
        """Start file watcher thread (daemon, polls every interval seconds)."""
        if self._watching:
            return
        self._watching = True
        self._watcher_thread = threading.Thread(
            target=self._watch_loop, args=(interval,), daemon=True
        )
        self._watcher_thread.start()
        logger.info(f"Product file watcher started (interval={interval}s)")

    def stop_watching(self):
        """Stop file watcher thread."""
        self._watching = False

    def _watch_loop(self, interval: float):
        """Poll for file changes in projects/ and businesses/ directories."""
        while self._watching:
            time.sleep(interval)
            try:
                current_mtimes = {}
                for d in (self.projects_dir, self.businesses_dir):
                    for f in d.glob("*.yaml"):
                        current_mtimes[str(f)] = f.stat().st_mtime

                if current_mtimes != self._file_mtimes:
                    logger.info("Config files changed, reloading...")
                    self.reload()
            except Exception as e:
                logger.error(f"File watcher error: {e}")

    def on_reload(self, callback: Callable):
        """Register a callback for when products are reloaded.

        Callback receives: callback(products: List[Dict])
        """
        self._on_reload_callbacks.append(callback)

    # ── CRUD Operations (products; 'project' kept as compat alias) ────

    def add_project(
        self,
        name: str,
        url: str,
        description: str,
        project_type: str = "SaaS",
        business_id: Optional[str] = None,
        **kwargs,
    ) -> str:
        """Create a new product YAML file.

        Returns the filepath of the created file.
        Raises ValueError if the file already exists or business_id is invalid.
        """
        slug = _slugify(name)
        if business_id is not None and not is_valid_slug(business_id):
            raise ValueError(f"Invalid business_id '{business_id}'")
        filepath = self.projects_dir / f"{slug}.yaml"

        if filepath.exists():
            raise ValueError(f"Project file already exists: {filepath}")

        data = {
            "project": {
                "id": slug,
                "business_id": business_id or "",
                "name": name,
                "url": url,
                "type": project_type,
                "description": description,
                "tagline": kwargs.get("tagline", ""),
                "weight": kwargs.get("weight", 1.0),
                "enabled": True,
                "selling_points": kwargs.get("selling_points", []),
                "target_audiences": kwargs.get("target_audiences", []),
                "business_profile": {
                    "socials": {
                        "twitter": "",
                        "website": url,
                    },
                    "features": [],
                    "pricing": {
                        "model": "unknown",
                        "free_tier": "",
                        "paid_plans": [],
                    },
                    "faqs": [],
                    "competitors": [],
                    "rules": {
                        "never_say": [],
                        "always_accurate": [
                            f"Product name is exactly '{name}'",
                            f"URL is {url}",
                        ],
                    },
                },
            },
            "reddit": {
                "target_subreddits": {"primary": [], "secondary": []},
                "keywords": [],
                "min_post_score": 3,
                "max_post_age_hours": 24,
            },
            "twitter": {
                "keywords": [],
                "hashtags": [],
            },
            "tone": {
                "style": "helpful_casual",
                "language": "en",
                "formality": "casual",
            },
        }

        with open(filepath, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False,
                      allow_unicode=True)

        self.reload()
        return str(filepath)

    def delete_project(self, name: str) -> bool:
        """Delete a product by name. Returns True if found and deleted."""
        for f in self.projects_dir.glob("*.yaml"):
            try:
                with open(f, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                if data.get("project", {}).get("name", "").lower() == name.lower():
                    f.unlink()
                    self.reload()
                    return True
            except Exception:
                continue
        return False

    def get_project(self, name: str) -> Optional[Dict]:
        """Get a product by name (case-insensitive)."""
        for p in self.projects:
            if p.get("project", {}).get("name", "").lower() == name.lower():
                return p
        return None

    def list_projects(self) -> List[str]:
        """List all product names."""
        return [p["project"]["name"] for p in self.projects]

    def get_project_filepath(self, name: str) -> Optional[str]:
        """Get the YAML file path for a product."""
        for f in self.projects_dir.glob("*.yaml"):
            try:
                with open(f, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                if data.get("project", {}).get("name", "").lower() == name.lower():
                    return str(f)
            except Exception:
                continue
        return None
