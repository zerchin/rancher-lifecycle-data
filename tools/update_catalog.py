from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from report_engine.lifecycle import LifecycleCatalog, load_lifecycle_catalog
from report_engine.tools.update_suse_lifecycle import update_catalog_auto


def _semantic_payload(value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": value.get("schema_version"),
        "sources": value.get("sources"),
        "products": value.get("products"),
        "support_matrices": value.get("support_matrices"),
    }


def validate_no_rollback(
    previous: LifecycleCatalog,
    candidate: LifecycleCatalog,
) -> None:
    if candidate.schema_version != previous.schema_version:
        raise ValueError(
            f"schema_version 不允许从 {previous.schema_version} 变为 "
            f"{candidate.schema_version}"
        )
    for product, releases in previous.products.items():
        missing = set(releases) - set(candidate.products.get(product, {}))
        if missing:
            raise ValueError(
                f"{product} 生命周期版本不得减少: {', '.join(sorted(missing))}"
            )
    for product, matrices in previous.support_matrices.items():
        missing = set(matrices) - set(candidate.support_matrices.get(product, {}))
        if missing:
            raise ValueError(
                f"{product} 支持矩阵版本不得减少: {', '.join(sorted(missing))}"
            )


def _write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _heartbeat_due(status_path: Path, now: datetime) -> bool:
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        checked_at = datetime.fromisoformat(status["last_successful_check"])
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return True
    return (now - checked_at.astimezone(timezone.utc)).days >= 28


def _write_outputs(path: Path | None, values: Dict[str, str]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as target:
        for key, value in values.items():
            target.write(f"{key}={value}\n")


def update(output: Path, status_path: Path, github_output: Path | None = None) -> bool:
    output = output.resolve()
    previous_value = json.loads(output.read_text(encoding="utf-8"))
    previous_catalog = load_lifecycle_catalog(output)
    now = datetime.now(timezone.utc)
    initial_publication = not status_path.is_file()

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.candidate.",
        suffix=".json",
        dir=str(output.parent),
    )
    os.close(descriptor)
    candidate_path = Path(temporary_name)
    try:
        shutil.copyfile(output, candidate_path)
        discovered_versions = update_catalog_auto(
            candidate_path,
            now.strftime("%Y-%m-%d.%H%M%S"),
        )
        candidate_value = json.loads(candidate_path.read_text(encoding="utf-8"))
        candidate_catalog = load_lifecycle_catalog(candidate_path)
        validate_no_rollback(previous_catalog, candidate_catalog)

        catalog_changed = initial_publication or (
            _semantic_payload(previous_value) != _semantic_payload(candidate_value)
        )
        if catalog_changed:
            candidate_value["as_of"] = now.date().isoformat()
            candidate_value["data_version"] = now.strftime("%Y-%m-%d.%H%M%S")
            _write_json(output, candidate_value)
            load_lifecycle_catalog(output)

        active_catalog = load_lifecycle_catalog(output)
        publish_status = catalog_changed or _heartbeat_due(status_path, now)
        if publish_status:
            _write_json(status_path, {
                "last_successful_check": now.isoformat(),
                "data_version": active_catalog.data_version,
                "data_as_of": active_catalog.as_of,
                "rancher_matrix_count": len(
                    active_catalog.support_matrices.get("rancher", {})
                ),
                "discovered_rancher_versions": len(discovered_versions),
            })

        publish_changed = catalog_changed or publish_status
        _write_outputs(github_output, {
            "catalog_changed": str(catalog_changed).lower(),
            "publish_changed": str(publish_changed).lower(),
            "data_version": active_catalog.data_version,
            "rancher_matrix_count": str(
                len(active_catalog.support_matrices.get("rancher", {}))
            ),
        })
        print(
            "生命周期数据检查完成："
            f"catalog_changed={str(catalog_changed).lower()}，"
            f"data_version={active_catalog.data_version}，"
            "rancher_matrix_count="
            f"{len(active_catalog.support_matrices.get('rancher', {}))}"
        )
        return catalog_changed
    finally:
        if candidate_path.exists():
            candidate_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="检查并发布 Rancher 生命周期数据")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("report_engine/data/lifecycle.json"),
    )
    parser.add_argument("--status", type=Path, default=Path("status.json"))
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    update(args.output, args.status, args.github_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
