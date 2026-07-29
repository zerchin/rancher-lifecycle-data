from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from report_engine.lifecycle import load_lifecycle_catalog  # noqa: E402


LIFECYCLE_URL = "https://www.suse.com/lifecycle/"
MATRIX_INDEX_URL = "https://www.suse.com/suse-rancher/support-matrix/all-supported-versions/"
MATRIX_URL = (
    "https://www.suse.com/suse-rancher/support-matrix/"
    "all-supported-versions/rancher-v{slug}/"
)
FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://www.suse.com/",
    "Upgrade-Insecure-Requests": "1",
}
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
# 这些 EOL 版本的官方内容使用旧网页或 PDF 存档，已人工核对并固化在种子数据中。
FROZEN_LEGACY_MATRIX_VERSIONS = {"2.4.17", "2.4.18", "2.5.15", "2.5.16"}
PRODUCT_MARKERS = {
    "rancher": "SUSE Rancher Prime",
    "rke2": "RKE2",
    "k3s": "k3s",
}
MATRIX_PLATFORM_ALIASES = {
    "rke": "rke1",
    "rke1": "rke1",
    "rke2": "rke2",
    "k3s": "k3s",
    "aks": "aks",
    "eks": "eks",
    "gke": "gke",
    "ack": "ack",
}


def _text(value: str) -> str:
    return " ".join(html.unescape(value).replace("\xa0", " ").split())


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: List[List[List[str]]] = []
        self._depth = 0
        self._table: List[List[str]] = []
        self._row: List[str] = []
        self._cell: List[str] = []
        self._in_cell = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._table = []
        elif tag == "tr" and self._depth == 1:
            self._row = []
        elif tag in {"th", "td"} and self._depth == 1:
            self._cell = []
            self._in_cell = True
        elif tag == "sup" and self._in_cell:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "sup" and self._skip_depth:
            self._skip_depth -= 1
        elif tag in {"th", "td"} and self._depth == 1 and self._in_cell:
            self._row.append(_text("".join(self._cell)))
            self._in_cell = False
        elif tag == "tr" and self._depth == 1 and self._row:
            self._table.append(self._row)
            self._row = []
        elif tag == "table" and self._depth:
            if self._depth == 1 and self._table:
                self.tables.append(self._table)
                self._table = []
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_cell and not self._skip_depth:
            self._cell.append(data)


def parse_tables(value: str) -> List[List[List[str]]]:
    parser = TableParser()
    parser.feed(value)
    parser.close()
    return parser.tables


def _iso_date(value: str) -> str:
    normalized = _text(value).replace("Sept ", "Sep ")
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(normalized, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"无法解析官方生命周期日期: {value!r}")


def _product_fragment(page: str, marker: str) -> str:
    start_match = re.search(
        rf'<tr\s+class=["\']row["\'][^>]*data-productFilter=["\']{re.escape(marker)}["\']',
        page,
        flags=re.IGNORECASE,
    )
    if not start_match:
        raise ValueError(f"官方生命周期页面缺少产品区块: {marker}")
    next_match = re.search(
        r'<tr\s+class=["\']row["\'][^>]*data-productFilter=',
        page[start_match.end():],
        flags=re.IGNORECASE,
    )
    end = start_match.end() + next_match.start() if next_match else len(page)
    return page[start_match.start():end]


def parse_product_lifecycle(page: str, marker: str) -> Dict[str, Dict[str, str]]:
    releases: Dict[str, Dict[str, str]] = {}
    for table in parse_tables(_product_fragment(page, marker)):
        if not table:
            continue
        header = [item.lower() for item in table[0]]
        if header[:4] != ["version", "ga", "eom", "eol"]:
            continue
        for row in table[1:]:
            if len(row) < 4 or not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.x)?", row[0]):
                continue
            version = row[0].removesuffix(".x")
            releases[version] = {
                "ga": _iso_date(row[1]),
                "eom": _iso_date(row[2]),
                "eol": _iso_date(row[3]),
            }
    if releases:
        return releases
    raise ValueError(f"官方生命周期页面缺少标准 GA/EOM/EOL 表格: {marker}")


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(item) for item in value.split("."))


def _platform_name(value: str) -> Optional[str]:
    normalized = re.sub(r"[^a-z0-9]", "", _text(value).lower())
    return MATRIX_PLATFORM_ALIASES.get(normalized)


def _kubernetes_minors(value: str) -> List[str]:
    versions: List[str] = []
    for match in re.findall(r"\bv?(1\.[0-9]+)(?:\.[0-9]+)?(?:[-+][a-zA-Z0-9.-]+)?", value):
        if match not in versions:
            versions.append(match)
    return versions


def _minor_range(minimum: str, maximum: str) -> List[str]:
    minimum_key = _version_key(minimum)
    maximum_key = _version_key(maximum)
    if len(minimum_key) != 2 or len(maximum_key) != 2 or minimum_key[0] != maximum_key[0]:
        raise ValueError(f"Kubernetes 版本范围无效: {minimum}-{maximum}")
    if minimum_key > maximum_key or maximum_key[1] - minimum_key[1] > 30:
        raise ValueError(f"Kubernetes 版本范围无效: {minimum}-{maximum}")
    return [f"{minimum_key[0]}.{minor}" for minor in range(minimum_key[1], maximum_key[1] + 1)]


def discover_rancher_versions(page: str) -> List[str]:
    versions = {
        match.replace("-", ".")
        for match in re.findall(r"rancher-v([0-9]+-[0-9]+-[0-9]+)(?:/|[\"'])", page, re.IGNORECASE)
    }
    if not versions:
        versions = set(re.findall(r"\b2\.[0-9]+\.[0-9]+\b", _text(page)))
    return sorted(versions, key=_version_key)


def supported_rancher_versions(
    page: str,
    lifecycle: Dict[str, Dict[str, str]],
    today: Optional[date] = None,
) -> List[str]:
    current_date = today or datetime.now(timezone.utc).date()
    supported_minors = {
        version
        for version, release in lifecycle.items()
        if release.get("eol") and date.fromisoformat(release["eol"]) >= current_date
    }
    return [
        version for version in discover_rancher_versions(page)
        if ".".join(version.split(".")[:2]) in supported_minors
    ]


def parse_rancher_matrix(page: str, rancher_version: str, source: str) -> Dict[str, object]:
    title_matches = re.search(
        rf"Rancher(?:\s+Manager)?(?:\s+v|\s+Version\s+){re.escape(rancher_version)}\b",
        _text(page),
        re.IGNORECASE,
    )
    slug_matches = re.search(
        rf"rancher-v{re.escape(rancher_version.replace('.', '-'))}(?:/|[\\\"'])",
        page,
        re.IGNORECASE,
    )
    if not title_matches and not slug_matches:
        raise ValueError(f"支持矩阵页面版本与请求不一致: {rancher_version}")
    tables = parse_tables(page)
    certified_ranges: Dict[str, tuple[str, str]] = {}
    certified_versions: Dict[str, set[str]] = {}
    modes: Dict[str, Dict[str, Dict[str, str]]] = {}
    for table in tables:
        if not table:
            continue
        header_index = next(
            (
                index for index, row in enumerate(table)
                if row and row[0].lower() in {
                    "distro", "rke1 versions", "rke2 versions", "k3s versions"
                }
            ),
            None,
        )
        header = [item.lower() for item in table[header_index]] if header_index is not None else []
        rows = table[:header_index] + table[header_index + 1:] if header_index is not None else []
        if header and header[0] == "distro":
            for row in rows:
                platform = _platform_name(row[0]) if len(row) >= 3 else None
                minimum = _kubernetes_minors(row[1]) if platform else []
                maximum = _kubernetes_minors(row[2]) if platform else []
                if platform and minimum and maximum:
                    certified_ranges[platform] = (minimum[0], maximum[0])
        elif header and header[0] in {"rke1 versions", "rke2 versions", "k3s versions"}:
            platform = header[0].split()[0]
            modes[platform] = {}
            for row in rows:
                versions = _kubernetes_minors(row[0]) if row else []
                if len(row) >= 3 and versions:
                    modes[platform][versions[0]] = {
                        "provisioned": row[1],
                        "imported": row[2],
                    }

        # Rancher 2.4/2.5 旧页面使用单独的发行版表，HTML 已包含所需版本，无需解析 PDF。
        first_header = [item.lower() for item in table[0]]
        if first_header and first_header[0] in {"k3s version", "rke2 version", "upstream k8s version"}:
            platform = {
                "k3s version": "k3s",
                "rke2 version": "rke2",
                "upstream k8s version": "rke1",
            }[first_header[0]]
            target = certified_versions.setdefault(platform, set())
            for row in table[1:]:
                if row:
                    target.update(_kubernetes_minors(row[0]))
        elif first_header[:2] == ["type", "upstream version"]:
            for row in table[1:]:
                if len(row) < 2:
                    continue
                row_type = row[0].strip().lower()
                platform = "rke1" if row_type == "rancher launched" else _platform_name(row_type)
                if platform:
                    certified_versions.setdefault(platform, set()).update(_kubernetes_minors(row[1]))

        # Imported 集群的 Any 行代表通用 Kubernetes 认证版本。
        for row in table:
            if len(row) >= 2 and row[0].strip().lower() == "any":
                versions = _kubernetes_minors(row[1])
                if len(versions) == 2 and re.fullmatch(
                    r"\s*v?1\.[0-9]+(?:\.[0-9]+)?\s*[-–]\s*v?1\.[0-9]+(?:\.[0-9]+)?\s*",
                    row[1],
                    re.IGNORECASE,
                ):
                    versions = _minor_range(versions[0], versions[1])
                certified_versions.setdefault("kubernetes", set()).update(versions)

    platforms: Dict[str, object] = {}
    all_platforms = set(certified_ranges) | set(certified_versions) | set(modes)
    for platform in sorted(all_platforms):
        versions = set(certified_versions.get(platform, set()))
        if platform in certified_ranges:
            minimum, maximum = certified_ranges[platform]
            versions.update(_minor_range(minimum, maximum))
        versions.update(modes.get(platform, {}))
        if not versions:
            continue
        ordered_versions = sorted(versions, key=_version_key)
        if platform in certified_ranges and modes.get(platform):
            minimum, maximum = certified_ranges[platform]
            mode_versions = sorted(modes[platform], key=_version_key)
            if mode_versions[0] != minimum or mode_versions[-1] != maximum:
                raise ValueError(
                    f"{platform} 认证范围与明细不一致: {minimum}-{maximum}, {mode_versions}"
                )
        platforms[platform] = {
            "certified_min": ordered_versions[0],
            "certified_max": ordered_versions[-1],
            "certified_versions": ordered_versions,
            "modes": modes.get(platform, {}),
        }
    return {"source": source, "platforms": platforms}


def _request_error_message(url: str, status: int, headers: object) -> str:
    message = f"SUSE 官方页面请求失败 HTTP {status}: {url}"
    if status == 403:
        message += "；官方站点拒绝了当前服务器请求，请检查出口 IP 或 HTTPS_PROXY"
    details = []
    get_header = getattr(headers, "get", None)
    if callable(get_header):
        server = get_header("Server")
        request_id = (
            get_header("CF-Ray")
            or get_header("X-Request-ID")
            or get_header("X-Azure-Ref")
        )
        if server:
            details.append(f"server={server}")
        if request_id:
            details.append(f"request_id={request_id}")
    if details:
        message += f" ({', '.join(details)})"
    return message


def fetch(
    url: str,
    max_bytes: int = 8 * 1024 * 1024,
    attempts: int = 3,
) -> str:
    if attempts < 1:
        raise ValueError("请求尝试次数必须至少为 1")
    for attempt in range(attempts):
        request = Request(url, headers=FETCH_HEADERS)
        try:
            with urlopen(request, timeout=30) as response:
                final_url = response.geturl()
                if response.status != 200:
                    raise RuntimeError(
                        _request_error_message(
                            final_url, response.status, response.headers
                        )
                    )
                content = response.read(max_bytes + 1)
        except HTTPError as exc:
            try:
                final_url = exc.geturl() or url
            except Exception:
                final_url = url
            message = _request_error_message(final_url, exc.code, exc.headers)
            if exc.code not in RETRYABLE_HTTP_STATUS or attempt + 1 >= attempts:
                raise RuntimeError(message) from exc
        except (TimeoutError, URLError, OSError) as exc:
            message = f"SUSE 官方页面请求失败: {url}；{type(exc).__name__}: {exc}"
            if attempt + 1 >= attempts:
                raise RuntimeError(message) from exc
        else:
            if len(content) > max_bytes:
                raise RuntimeError(f"官方页面超过 {max_bytes} 字节限制: {final_url}")
            return content.decode("utf-8")
        time.sleep(min(2 ** attempt, 4))
    raise RuntimeError(f"SUSE 官方页面请求失败: {url}")


def update_catalog(
    path: Path,
    rancher_versions: Sequence[str],
    data_version: str,
    lifecycle_page: Optional[str] = None,
) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    lifecycle_page = lifecycle_page or fetch(LIFECYCLE_URL)
    products = value.setdefault("products", {})
    for product, marker in PRODUCT_MARKERS.items():
        products[product] = parse_product_lifecycle(lifecycle_page, marker)
    value.setdefault("sources", {}).update({
        "rancher": f"{LIFECYCLE_URL}#suse-rancher-prime",
        "rke2": f"{LIFECYCLE_URL}#rke2",
        "k3s": f"{LIFECYCLE_URL}#k3s",
    })
    rancher_matrices = value.setdefault("support_matrices", {}).setdefault("rancher", {})

    def fetch_matrix(rancher_version: str) -> tuple[str, Dict[str, object]]:
        slug = rancher_version.replace(".", "-")
        source = MATRIX_URL.format(slug=slug)
        try:
            matrix = parse_rancher_matrix(fetch(source), rancher_version, source)
        except Exception as exc:
            raise ValueError(f"Rancher {rancher_version} 支持矩阵解析失败: {exc}") from exc
        return rancher_version, matrix

    versions_to_fetch = [
        version for version in rancher_versions
        if version not in FROZEN_LEGACY_MATRIX_VERSIONS
    ]
    # 日常更新通常只新增少量补丁版本，顺序请求可降低触发官网 WAF 或限流的概率。
    for rancher_version in versions_to_fetch:
        version, matrix = fetch_matrix(rancher_version)
        rancher_matrices[version] = matrix
    value["schema_version"] = "1.1"
    value["data_version"] = data_version
    value["as_of"] = datetime.now(timezone.utc).date().isoformat()

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
        load_lifecycle_catalog(temporary)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def update_catalog_auto(path: Path, data_version: Optional[str] = None) -> List[str]:
    lifecycle_page = fetch(LIFECYCLE_URL)
    versions = discover_rancher_versions(fetch(MATRIX_INDEX_URL))
    if not versions:
        raise ValueError("官方支持矩阵首页未发现 Rancher 补丁版本")
    existing = json.loads(path.read_text(encoding="utf-8"))
    recorded = existing.get("support_matrices", {}).get("rancher", {})
    recorded_versions = set(recorded) if isinstance(recorded, dict) else set()
    missing_versions = [version for version in versions if version not in recorded_versions]
    update_catalog(
        path,
        missing_versions,
        data_version or datetime.now(timezone.utc).strftime("%Y-%m-%d.%H%M%S"),
        lifecycle_page=lifecycle_page,
    )
    return versions


def main() -> int:
    parser = argparse.ArgumentParser(description="从 SUSE 官方页面更新离线生命周期与 Rancher 支持矩阵")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "report_engine/data/lifecycle.json",
        help="待更新的离线 JSON 文件",
    )
    parser.add_argument(
        "--rancher-version",
        action="append",
        dest="rancher_versions",
        help="需要获取支持矩阵的 Rancher 完整版本，可重复指定",
    )
    parser.add_argument("--data-version", help="离线数据版本，默认使用当天日期")
    args = parser.parse_args()
    existing = json.loads(args.output.read_text(encoding="utf-8"))
    versions = args.rancher_versions or sorted(
        existing.get("support_matrices", {}).get("rancher", {}),
        key=_version_key,
    )
    if not versions:
        parser.error("至少需要一个 --rancher-version，或输出文件中已有 Rancher 矩阵")
    update_catalog(args.output.resolve(), versions, args.data_version or date.today().isoformat())
    print(f"已更新 {args.output}: Rancher 矩阵 {', '.join(versions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
