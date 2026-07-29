from __future__ import annotations

import copy
import unittest

from report_engine.lifecycle import LifecycleCatalog, load_lifecycle_catalog
from tools.update_catalog import validate_no_rollback


class CatalogUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_lifecycle_catalog()

    def candidate(self) -> LifecycleCatalog:
        return LifecycleCatalog(
            schema_version=self.catalog.schema_version,
            data_version=self.catalog.data_version,
            as_of=self.catalog.as_of,
            sources=copy.deepcopy(self.catalog.sources),
            products=copy.deepcopy(self.catalog.products),
            support_matrices=copy.deepcopy(self.catalog.support_matrices),
        )

    def test_current_catalog_is_valid_candidate(self) -> None:
        validate_no_rollback(self.catalog, self.candidate())

    def test_rejects_lifecycle_version_removal(self) -> None:
        candidate = self.candidate()
        candidate.products["rancher"].pop(next(iter(candidate.products["rancher"])))
        with self.assertRaisesRegex(ValueError, "生命周期版本不得减少"):
            validate_no_rollback(self.catalog, candidate)

    def test_rejects_matrix_removal(self) -> None:
        candidate = self.candidate()
        candidate.support_matrices["rancher"].pop(
            next(iter(candidate.support_matrices["rancher"]))
        )
        with self.assertRaisesRegex(ValueError, "支持矩阵版本不得减少"):
            validate_no_rollback(self.catalog, candidate)


if __name__ == "__main__":
    unittest.main()
