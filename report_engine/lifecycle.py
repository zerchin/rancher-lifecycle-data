from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional


class LifecycleDataError(ValueError):
    """Raised when lifecycle data is unavailable or malformed."""


@dataclass(frozen=True)
class LifecycleCatalog:
    schema_version: str
    data_version: str
    as_of: str
    sources: Dict[str, str]
    products: Dict[str, Dict[str, Dict[str, str]]]
    support_matrices: Dict[str, Dict[str, Dict[str, Any]]]

    def release(self, product: str, version: str) -> Optional[Dict[str, str]]:
        product_data = self.products.get(product)
        if not isinstance(product_data, dict):
            return None
        release = product_data.get(version)
        return release if isinstance(release, dict) else None

    def support_matrix(self, product: str, version: str) -> Optional[Dict[str, Any]]:
        product_data = self.support_matrices.get(product)
        if not isinstance(product_data, dict):
            return None
        matrix = product_data.get(version)
        return matrix if isinstance(matrix, dict) else None

    def metadata(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "data_version": self.data_version,
            "as_of": self.as_of,
            "sources": self.sources,
            "support_matrix_versions": {
                product: sorted(matrices)
                for product, matrices in self.support_matrices.items()
            },
        }


def default_lifecycle_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "lifecycle.json"


def load_lifecycle_catalog(path: Optional[Path] = None) -> LifecycleCatalog:
    data_path = (path or default_lifecycle_path()).resolve()
    try:
        if data_path.stat().st_size > 1024 * 1024:
            raise LifecycleDataError("生命周期数据文件超过 1 MiB 限制")
        value = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleDataError(f"无法读取生命周期数据 {data_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LifecycleDataError("生命周期数据根节点必须是对象")
    required_strings = ("schema_version", "data_version", "as_of")
    if any(not isinstance(value.get(key), str) or not value[key] for key in required_strings):
        raise LifecycleDataError("生命周期数据缺少版本信息")
    sources = value.get("sources")
    products = value.get("products")
    support_matrices = value.get("support_matrices", {})
    if not isinstance(sources, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in sources.items()):
        raise LifecycleDataError("生命周期数据 sources 格式无效")
    if not isinstance(products, dict):
        raise LifecycleDataError("生命周期数据 products 格式无效")
    for product, releases in products.items():
        if not isinstance(product, str) or not isinstance(releases, dict):
            raise LifecycleDataError("生命周期产品数据格式无效")
        for version, release in releases.items():
            if not isinstance(version, str) or not isinstance(release, dict):
                raise LifecycleDataError("生命周期版本数据格式无效")
            if not all(isinstance(key, str) and isinstance(item, str) for key, item in release.items()):
                raise LifecycleDataError("生命周期日期字段必须是字符串")
            for field, field_value in release.items():
                if field in {"ga", "eom", "eol", "maintenance"}:
                    try:
                        date.fromisoformat(field_value)
                    except ValueError as exc:
                        raise LifecycleDataError(
                            f"生命周期日期格式无效: {product} {version} {field}={field_value}"
                        ) from exc
    if not isinstance(support_matrices, dict):
        raise LifecycleDataError("生命周期数据 support_matrices 格式无效")
    for product, matrices in support_matrices.items():
        if not isinstance(product, str) or not isinstance(matrices, dict):
            raise LifecycleDataError("支持矩阵产品数据格式无效")
        for version, matrix in matrices.items():
            if not isinstance(version, str) or not isinstance(matrix, dict):
                raise LifecycleDataError("支持矩阵版本数据格式无效")
            source = matrix.get("source")
            platforms = matrix.get("platforms")
            if not isinstance(source, str) or not source.startswith("https://"):
                raise LifecycleDataError(f"支持矩阵来源无效: {product} {version}")
            if not isinstance(platforms, dict):
                raise LifecycleDataError(f"支持矩阵 platforms 格式无效: {product} {version}")
            for platform, platform_data in platforms.items():
                if not isinstance(platform, str) or not isinstance(platform_data, dict):
                    raise LifecycleDataError(f"支持矩阵平台数据格式无效: {product} {version}")
                certified_versions = platform_data.get("certified_versions")
                if (
                    not isinstance(certified_versions, list)
                    or not certified_versions
                    or not all(isinstance(item, str) and item for item in certified_versions)
                ):
                    raise LifecycleDataError(
                        f"支持矩阵 certified_versions 格式无效: {product} {version} {platform}"
                    )
                modes = platform_data.get("modes", {})
                if not isinstance(modes, dict):
                    raise LifecycleDataError(f"支持矩阵 modes 格式无效: {product} {version} {platform}")
                unknown_versions = set(modes) - set(certified_versions)
                if unknown_versions:
                    raise LifecycleDataError(
                        f"支持矩阵 modes 包含未认证版本: {product} {version} {platform}"
                    )
                for platform_version, mode_data in modes.items():
                    if not isinstance(platform_version, str) or not isinstance(mode_data, dict):
                        raise LifecycleDataError(
                            f"支持矩阵模式数据格式无效: {product} {version} {platform}"
                        )
                    if not all(
                        key in {"provisioned", "imported"} and isinstance(item, str)
                        for key, item in mode_data.items()
                    ):
                        raise LifecycleDataError(
                            f"支持矩阵模式字段无效: {product} {version} {platform} {platform_version}"
                        )
    return LifecycleCatalog(
        schema_version=value["schema_version"],
        data_version=value["data_version"],
        as_of=value["as_of"],
        sources=sources,
        products=products,
        support_matrices=support_matrices,
    )
