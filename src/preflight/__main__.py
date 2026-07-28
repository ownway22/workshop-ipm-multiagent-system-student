"""環境前置檢查的單一指令入口。

執行方式（在 `src/` 目錄下）：

```bash
uv run python -m preflight
```

契約：[contracts/preflight-contract.md](../../specs/001-ipm-workshop-delivery/contracts/preflight-contract.md)

行為要點：

- **單一入口**：六項檢查 MUST 由這一支指令全部涵蓋，MUST NOT 拆成多支（FR-006）。
- **不早退**：某項失敗時仍繼續檢查其餘項目，一次呈現完整狀況（FR-004）。
- **結束碼**：`0` 全數通過｜`1` 至少一項失敗｜`2` 必要環境變數缺漏。

不論執行幾次、在什麼時點執行，跑的都是**同一支**指令，結果可直接比對。
"""

import sys
import unicodedata

from config import (
    MissingEnvironmentError,
    format_missing_env_message,
    load_settings,
)
from preflight import CheckResult
from preflight.azure_checks import (
    check_providers,
    check_region,
    check_role_assignments,
    check_subscription,
    resolve_project_location,
)
from preflight.foundry_checks import check_model_capabilities, check_project_endpoint

TOTAL_CHECKS = 6

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_MISSING_ENV = 2

_HEADER = "IPM Workshop 環境前置檢查"
_RULE = "=" * 40

#: 項目標題欄的顯示寬度（以半形字為單位）。
_TITLE_COLUMN_WIDTH = 26


def _display_width(text: str) -> int:
    """計算字串在等寬終端上的顯示寬度。

    中文屬於全形字，`str.ljust()` 以字元數計算會造成欄位錯位，因此改以
    East Asian Width 屬性判定。
    """
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _pad(text: str, width: int) -> str:
    """依顯示寬度補上右側空白。"""
    return text + " " * max(1, width - _display_width(text))


def run_all_checks() -> list[CheckResult]:
    """依序執行六項檢查並回傳全部結果。

    Raises:
        MissingEnvironmentError: 必要環境變數缺漏，無法開始檢查。
    """
    settings = load_settings()

    results = [check_subscription()]

    # 檢查 2 與檢查 4 都需要「專案位於哪個資源群組、哪個區域」，只解析一次共用。
    location = resolve_project_location(settings.foundry_project_endpoint)
    results.append(check_region(location))
    results.append(check_providers())
    results.append(check_role_assignments(location))

    results.append(
        check_project_endpoint(
            settings.foundry_project_endpoint,
            settings.model_deployment_name,
        )
    )
    results.append(
        check_model_capabilities(
            settings.foundry_project_endpoint,
            settings.model_deployment_name,
        )
    )
    return results


def render(results: list[CheckResult]) -> str:
    """把檢查結果組成契約第 4 節規定的輸出。"""
    lines = [_HEADER, _RULE]

    for result in results:
        marker = "✅" if result.passed else "❌"
        prefix = f"[{result.index}/{TOTAL_CHECKS}] {result.title}"
        lines.append(f"{_pad(prefix, _TITLE_COLUMN_WIDTH)}{marker} {result.detail}")

        if result.passed:
            continue

        if result.impact:
            lines.append(f"    影響：{result.impact}")
        if not result.onsite_fixable:
            lines.append("    ⚠️  這一項通常無法在現場修復，請及早改走觀摩路徑（docs/06-fallback.md）。")
        if result.remediation:
            lines.append("    修復（可直接複製執行）：")
            lines.extend(f"        {step}" for step in result.remediation)
        lines.append("")

    passed_count = sum(1 for result in results if result.passed)
    lines.append("")
    if passed_count == TOTAL_CHECKS:
        lines.append(f"結果：{TOTAL_CHECKS}/{TOTAL_CHECKS} 通過。可以進入 Lab 1。")
    else:
        failed_count = TOTAL_CHECKS - passed_count
        lines.append(f"結果：{passed_count}/{TOTAL_CHECKS} 通過，{failed_count} 項待修復。")

    return "\n".join(lines)


def main() -> int:
    """程式進入點；回傳結束碼。"""
    try:
        results = run_all_checks()
    except MissingEnvironmentError as error:
        print(str(error), file=sys.stderr)
        return EXIT_MISSING_ENV

    print(render(results))
    return EXIT_OK if all(result.passed for result in results) else EXIT_CHECK_FAILED


if __name__ == "__main__":
    raise SystemExit(main())


# `format_missing_env_message` 由 config 提供，於此重新匯出僅為讓測試可從單一入口取得。
__all__ = ["EXIT_CHECK_FAILED", "EXIT_MISSING_ENV", "EXIT_OK", "format_missing_env_message", "main", "render", "run_all_checks"]
