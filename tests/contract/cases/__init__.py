"""Contract cases, one module per route group. Each module exports `ROUTES: list[RouteCases]`.

`ALL` gathers them; tests/contract/test_contract.py runs every documented entry of every route
against them. To port a route: add its RouteCases here (see docs/darwin-api.md, "Adding contract cases").
"""

from __future__ import annotations

from tests.contract.cases import admin, brands, exports, storyline
from tests.contract.harness import RouteCases

ALL: dict[str, RouteCases] = {rc.route: rc for module in (storyline, exports) for rc in module.ROUTES}
# --- feature/darwin-brands: brand routes; admin, analytics and userinfo ---
ALL.update({rc.route: rc for module in (brands, admin) for rc in module.ROUTES})
