"""The names of the PostgreSQL adapter's API.

Its example needs a database, so tests/integration runs it.
"""

from __future__ import annotations

import skewsplit.postgres
from skewsplit.postgres.catalog import CatalogError
from skewsplit.postgres.planning import distribution, plan
from skewsplit.postgres.verify import Verification, verify, verify_plan


def test_the_names_of_the_api() -> None:
    assert skewsplit.postgres.__all__ == [
        "CatalogError",
        "CatalogErrorCode",
        "Verification",
        "distribution",
        "plan",
        "verify",
        "verify_plan",
    ]
    assert skewsplit.postgres.plan is plan
    assert skewsplit.postgres.distribution is distribution
    assert skewsplit.postgres.verify is verify
    assert skewsplit.postgres.verify_plan is verify_plan
    assert skewsplit.postgres.Verification is Verification
    assert skewsplit.postgres.CatalogError is CatalogError
    assert skewsplit.postgres.CatalogErrorCode is not None
