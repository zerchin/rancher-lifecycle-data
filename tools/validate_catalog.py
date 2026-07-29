from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from report_engine.lifecycle import load_lifecycle_catalog


def validate(path: Path) -> None:
    catalog = load_lifecycle_catalog(path)
    if catalog.schema_version != "1.1":
        raise ValueError(f"不支持的 schema_version: {catalog.schema_version}")
    for product in ("rancher", "rke2", "k3s"):
        if not catalog.products.get(product):
            raise ValueError(f"缺少 {product} 生命周期数据")
    matrices = catalog.support_matrices.get("rancher", {})
    if not matrices:
        raise ValueError("缺少 Rancher 支持矩阵")

    source_urls = list(catalog.sources.values())
    source_urls.extend(
        matrix["source"]
        for matrix in matrices.values()
    )
    for source in source_urls:
        parsed = urlsplit(source)
        if parsed.scheme != "https" or parsed.hostname not in {
            "www.suse.com",
            "kubernetes.io",
        }:
            raise ValueError(f"发现非官方数据来源: {source}")

    print(
        f"数据校验通过：Rancher={len(catalog.products['rancher'])}，"
        f"RKE2={len(catalog.products['rke2'])}，"
        f"K3s={len(catalog.products['k3s'])}，"
        f"Rancher matrices={len(matrices)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="校验公开生命周期 JSON")
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path("report_engine/data/lifecycle.json"),
    )
    args = parser.parse_args()
    validate(args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
