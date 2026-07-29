# Rancher Lifecycle Data

该仓库保存 Rancher Inspector 使用的公开生命周期与支持矩阵数据，不包含客户信息、账号、Token 或巡检结果。

数据来源：

- [SUSE Product Support Lifecycle](https://www.suse.com/lifecycle/)
- [SUSE Rancher Support Matrix](https://www.suse.com/suse-rancher/support-matrix/all-supported-versions/)

GitHub Actions 每天从 SUSE 官方页面检查一次：

1. 完整刷新 Rancher、RKE2 和 K3s 生命周期。
2. 从官方索引发现 Rancher 补丁版本，只下载尚未收录的新矩阵。
3. 校验 JSON 格式以及版本、矩阵不得回退。
4. 数据变化时提交 `lifecycle.json`；无变化时至少每 28 天提交一次成功检查状态。
5. 任何下载或校验失败都不会覆盖上一份有效数据。

## 数据地址

```text
https://raw.githubusercontent.com/zerchin/rancher-lifecycle-data/main/report_engine/data/lifecycle.json
```

## 本地校验

```bash
python3 tools/validate_catalog.py
python3 -m unittest discover -s tests -p "test_*.py"
```

## 手动检查

```bash
python3 tools/update_catalog.py
```

更新脚本只处理 SUSE 官方公开页面。报告生成过程不依赖该仓库实时在线，Rancher Inspector UI 会在下载、校验成功后原子替换本地数据，失败时继续使用最近一次有效版本。
