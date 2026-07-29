#!/usr/bin/env python3
"""由 `src/.env` 與目前 Azure 登入狀態設定 Lab 3 的 azd 環境。"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIRECTORY = REPOSITORY_ROOT / "deploy"
SOURCE_ENV_FILE = REPOSITORY_ROOT / "src" / ".env"

# 共用 preflight 已驗證過的 endpoint 解析與 Azure CLI 查詢邏輯。
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from preflight import AzCliError, run_az_json  # noqa: E402
from preflight.azure_checks import ProjectLocation, resolve_project_location  # noqa: E402

PROJECT_API_VERSION = "2025-04-01-preview"
LOCAL_REQUIRED_VARIABLES = ("FOUNDRY_PROJECT_ENDPOINT", "MODEL_DEPLOYMENT_NAME")


class ConfigurationError(RuntimeError):
    """無法安全推導或寫入部署環境。"""


@dataclass(frozen=True)
class AzureContext:
    """可由 Azure CLI 唯一確認的部署目標。"""

    subscription_id: str
    tenant_id: str
    resource_group_location: str
    project: ProjectLocation


def read_local_settings(env_file: Path = SOURCE_ENV_FILE) -> tuple[str, str]:
    """讀取本機執行已驗證過的專案端點與模型部署名稱。"""
    if not env_file.is_file():
        raise ConfigurationError(
            f"找不到 {env_file}。請先完成 Lab 0，建立並驗證 src/.env。"
        )

    values = dotenv_values(env_file)
    missing = [
        name
        for name in LOCAL_REQUIRED_VARIABLES
        if not _is_filled(values.get(name))
    ]
    if missing:
        raise ConfigurationError(
            f"{env_file} 缺少有效值：{', '.join(missing)}。請先執行 Lab 0 preflight。"
        )

    return (
        str(values["FOUNDRY_PROJECT_ENDPOINT"]).strip().rstrip("/"),
        str(values["MODEL_DEPLOYMENT_NAME"]).strip(),
    )


def _is_filled(raw: str | None) -> bool:
    """排除空值與 `.env.template` 的 `<TODO: ...>` 佔位符。"""
    if raw is None:
        return False
    value = raw.strip()
    return bool(value) and not (value.startswith("<") and value.endswith(">"))


def resolve_azure_context(endpoint: str, model_deployment_name: str) -> AzureContext:
    """查出 endpoint 對應的 Azure 資源，並確認專案與模型部署存在。"""
    project = resolve_project_location(endpoint)
    if isinstance(project, AzCliError):
        raise ConfigurationError(str(project))

    try:
        account = run_az_json(
            [
                "account",
                "show",
                "--query",
                "{subscriptionId:id,tenantId:tenantId}",
            ]
        )
        resource_group_location = run_az_json(
            [
                "group",
                "show",
                "--name",
                project.resource_group,
                "--query",
                "location",
            ]
        )
        run_az_json(
            [
                "resource",
                "show",
                "--ids",
                project.project_scope,
                "--api-version",
                PROJECT_API_VERSION,
            ]
        )
        run_az_json(
            [
                "cognitiveservices",
                "account",
                "deployment",
                "show",
                "--name",
                project.account_name,
                "--resource-group",
                project.resource_group,
                "--deployment-name",
                model_deployment_name,
            ]
        )
    except AzCliError as error:
        raise ConfigurationError(str(error)) from error

    if not isinstance(account, dict):
        raise ConfigurationError("az account show 未回傳可用的訂閱資訊。")

    subscription_id = str(account.get("subscriptionId", "")).strip()
    tenant_id = str(account.get("tenantId", "")).strip()
    location = str(resource_group_location).strip()
    if not subscription_id or not tenant_id or not location:
        raise ConfigurationError("Azure CLI 回傳的訂閱、租戶或資源群組區域不完整。")

    return AzureContext(
        subscription_id=subscription_id,
        tenant_id=tenant_id,
        resource_group_location=location,
        project=project,
    )


def build_azd_values(
    endpoint: str,
    model_deployment_name: str,
    context: AzureContext,
) -> dict[str, str]:
    """建立 provision 與 hosted-agent extension 共用的完整 azd 設定。"""
    project = context.project
    return {
        "AZURE_SUBSCRIPTION_ID": context.subscription_id,
        "AZURE_TENANT_ID": context.tenant_id,
        "AZURE_LOCATION": context.resource_group_location,
        "AZURE_RESOURCE_GROUP": project.resource_group,
        "AZURE_AI_ACCOUNT_NAME": project.account_name,
        "AZURE_AI_PROJECT_NAME": project.project_name,
        "AZURE_AI_PROJECT_ID": project.project_scope,
        "AZURE_AI_PROJECT_ENDPOINT": endpoint,
        "FOUNDRY_PROJECT_ENDPOINT": endpoint,
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": model_deployment_name,
        "MODEL_DEPLOYMENT_NAME": model_deployment_name,
    }


def set_azd_values(
    values: dict[str, str], deploy_directory: Path = DEPLOY_DIRECTORY
) -> None:
    """一次寫入目前選取的 azd environment，不觸發互動式提示。"""
    command = [
        "azd",
        "env",
        "set",
        *(f"{name}={value}" for name, value in values.items()),
        "--no-prompt",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=deploy_directory,
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError as error:
        raise ConfigurationError("找不到 azd，請先完成 Lab 0 的工具安裝。") from error

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ConfigurationError(f"azd env set 失敗：{detail}")


def main() -> int:
    """解析、驗證並匯入目前 Lab 3 需要的所有環境變數。"""
    try:
        endpoint, model_deployment_name = read_local_settings()
        context = resolve_azure_context(endpoint, model_deployment_name)
        values = build_azd_values(endpoint, model_deployment_name, context)
        set_azd_values(values)
    except ConfigurationError as error:
        print(f"設定失敗：{error}", file=sys.stderr)
        return 1

    print("azd 環境設定完成：")
    print(f"  訂閱：{context.subscription_id}")
    print(f"  資源群組：{context.project.resource_group}")
    print(f"  Foundry 帳戶：{context.project.account_name}")
    print(f"  Foundry 專案：{context.project.project_name}")
    print(f"  模型部署：{model_deployment_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())