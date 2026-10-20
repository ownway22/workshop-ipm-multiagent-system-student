#!/usr/bin/env python3
"""由 `src/.env` 與目前 Azure 登入狀態設定 Lab 3 的 azd 環境。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

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
BOOTSTRAP_FIELDS = {
    "subscriptionId": "AZURE_SUBSCRIPTION_ID",
    "resourceGroup": "AZURE_RESOURCE_GROUP",
    "environmentName": "AZURE_ENV_NAME",
    "location": "AZURE_LOCATION",
    "accountName": "AZURE_AI_ACCOUNT_NAME",
    "projectName": "AZURE_AI_PROJECT_NAME",
    "studentObjectId": None,
}
BOOTSTRAP_OUTPUTS = (
    "AZURE_CONTAINER_REGISTRY_ENDPOINT",
    "AZURE_CONTAINER_REGISTRY_RESOURCE_ID",
    "AZURE_ACR_CONNECTION_NAME",
)
BOOTSTRAP_RESOURCE_NAME_PATTERNS = {
    "resourceGroup": r"[A-Za-z0-9_.-]{1,90}(?<!\.)",
    "accountName": r"[A-Za-z0-9][A-Za-z0-9-]{0,62}[A-Za-z0-9]",
    "projectName": r"[A-Za-z0-9][A-Za-z0-9_.-]{1,63}",
}


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
        project_resource = run_az_json(
            [
                "resource",
                "show",
                "--ids",
                project.project_scope,
                "--api-version",
                PROJECT_API_VERSION,
            ]
        )
        model_resource = run_az_json(
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

    subscription_id = account.get("subscriptionId")
    tenant_id = account.get("tenantId")
    location = resource_group_location
    if (
        not isinstance(subscription_id, str) or not subscription_id.strip()
        or not isinstance(tenant_id, str) or not tenant_id.strip()
        or not isinstance(location, str) or not location.strip()
    ):
        raise ConfigurationError("Azure CLI 回傳的訂閱、租戶或資源群組區域不完整。")
    if (
        not isinstance(project_resource, dict)
        or str(project_resource.get("id", "")).casefold() != project.project_scope.casefold()
        or not isinstance(model_resource, dict)
        or not model_resource
    ):
        raise ConfigurationError("Azure CLI 未回傳有效的既有專案或模型部署。")
    if project.account_id.split("/")[2].casefold() != subscription_id.casefold():
        raise ConfigurationError("Foundry 專案與目前 Azure 訂閱不一致。")

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


def run_azd(args: list[str], deploy_directory: Path = DEPLOY_DIRECTORY) -> str:
    """只捕捉所需值，不輸出完整 azd environment。"""
    try:
        result = subprocess.run(
            ["azd", *args, "--no-prompt"],
            cwd=deploy_directory,
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as error:
        raise ConfigurationError("找不到 azd，請先完成 Lab 0 的工具安裝。") from error
    except subprocess.TimeoutExpired as error:
        raise ConfigurationError(f"azd {' '.join(args[:2])} 逾時（60 秒）。") from error

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ConfigurationError(
            f"azd {' '.join(args[:2])} 失敗（exit {result.returncode}）：{detail}"
        )
    return result.stdout.strip()


def set_azd_values(
    values: dict[str, str],
    deploy_directory: Path = DEPLOY_DIRECTORY,
    *,
    environment: str | None = None,
) -> None:
    """一次寫入 azd environment；bootstrap 匯入時固定原 environment。"""
    args = ["env", "set", *(f"{name}={value}" for name, value in values.items())]
    if environment is not None:
        args.extend(["--environment", environment])
    run_azd(args, deploy_directory)


def read_azd_value(name: str, environment: str | None = None) -> str:
    args = ["env", "get-value", name]
    if environment is not None:
        args.extend(["--environment", environment])
    value = run_azd(args)
    if not _is_filled(value):
        raise ConfigurationError(f"原 azd environment 缺少 {name}；不猜測或另建環境。")
    return value


def validate_bootstrap_request(raw: object) -> dict[str, str | int]:
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schemaVersion", *BOOTSTRAP_FIELDS}
        or type(raw.get("schemaVersion")) is not int
        or raw["schemaVersion"] != 1
    ):
        raise ConfigurationError("bootstrap request 必須是 schemaVersion 1 與七個必要欄位。")
    for name in BOOTSTRAP_FIELDS:
        value = raw[name]
        if (
            not isinstance(value, str)
            or not _is_filled(value)
            or value != value.strip()
            or re.search(r"[/\\\x00-\x1f]", value)
        ):
            raise ConfigurationError(f"bootstrap request 的 {name} 無效。")
    for name, pattern in BOOTSTRAP_RESOURCE_NAME_PATTERNS.items():
        if not re.fullmatch(pattern, raw[name]):
            raise ConfigurationError(
                f"bootstrap request 的 {name} 不符合安全命名限制；"
                "請參閱 docs/03-lab3-deploy.md，不會自動改名。"
            )
    for name in ("subscriptionId", "studentObjectId"):
        try:
            valid = str(UUID(raw[name])).casefold() == raw[name].casefold()
        except ValueError:
            valid = False
        if not valid:
            raise ConfigurationError(f"bootstrap request 的 {name} 必須是 GUID。")
    if len(raw["environmentName"]) > 64 or not re.fullmatch(r"[a-z0-9]+", raw["location"]):
        raise ConfigurationError("bootstrap request 的 environmentName 或 location 無效。")
    return raw


def build_bootstrap_request() -> dict[str, str | int]:
    """以學員的原 azd environment、.env 與 Azure context 交叉核對。"""
    environment = read_azd_value("AZURE_ENV_NAME")
    endpoint, model = read_local_settings()
    context = resolve_azure_context(endpoint, model)
    values = build_azd_values(endpoint, model, context)
    values["AZURE_ENV_NAME"] = environment
    for name, expected in values.items():
        actual = read_azd_value(name, environment)
        if actual != expected:
            raise ConfigurationError(
                f"原 azd environment 的 {name} 與目前 .env／Azure context 不一致；"
                "請先確認目標，不自動覆寫。"
            )
    try:
        student_id = run_az_json(["ad", "signed-in-user", "show", "--query", "id"])
    except AzCliError as error:
        raise ConfigurationError(str(error)) from error
    request = {
        "schemaVersion": 1,
        **{field: values[key] for field, key in BOOTSTRAP_FIELDS.items() if key},
        "studentObjectId": student_id,
    }
    return validate_bootstrap_request(request)


def validate_bootstrap_outputs(
    raw: object, request: dict[str, str | int]
) -> dict[str, str]:
    if (
        not isinstance(raw, dict)
        or set(raw) != set(BOOTSTRAP_OUTPUTS)
        or not all(isinstance(value, str) and value.strip() == value and value
                   for value in raw.values())
    ):
        raise ConfigurationError("bootstrap result 缺少有效的三個 ACR outputs。")
    scope = (
        f"/subscriptions/{request['subscriptionId']}"
        f"/resourceGroups/{request['resourceGroup']}"
        "/providers/Microsoft.ContainerRegistry/registries/"
    )
    registry_id = raw["AZURE_CONTAINER_REGISTRY_RESOURCE_ID"]
    registry_name = registry_id[len(scope):]
    if (
        not registry_id.casefold().startswith(scope.casefold())
        or not re.fullmatch(r"[a-zA-Z0-9]{5,50}", registry_name)
        or raw["AZURE_CONTAINER_REGISTRY_ENDPOINT"].casefold()
        != f"{registry_name}.azurecr.io".casefold()
        or not re.fullmatch(r"acr-[a-z0-9]{13}", raw["AZURE_ACR_CONNECTION_NAME"])
    ):
        raise ConfigurationError("bootstrap result 的 ACR scope、endpoint 或 connection 無效。")
    return raw


def import_bootstrap_result(path: Path) -> None:
    result = json.loads(path.read_text(encoding="utf-8-sig"))
    if (
        not isinstance(result, dict)
        or set(result) != {"schemaVersion", "request", "outputs"}
        or type(result.get("schemaVersion")) is not int
        or result["schemaVersion"] != 1
    ):
        raise ConfigurationError("bootstrap result 必須是 schemaVersion 1 的結果 envelope。")
    request = validate_bootstrap_request(result["request"])
    outputs = validate_bootstrap_outputs(result["outputs"], request)
    if request != build_bootstrap_request():
        raise ConfigurationError(
            "bootstrap result 不屬於目前學員的原 azd environment／project／scope；未匯入。"
        )
    set_azd_values(outputs, environment=str(request["environmentName"]))


def main(argv: list[str] | None = None) -> int:
    """預設保留自助設定；bootstrap 匯出與匯入不執行 provision／deploy。"""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--export-bootstrap-request", type=Path, metavar="PATH")
    mode.add_argument("--import-bootstrap-result", type=Path, metavar="PATH")
    args = parser.parse_args(argv)
    try:
        if args.export_bootstrap_request:
            request = build_bootstrap_request()
            content = json.dumps(request, indent=2, ensure_ascii=True) + "\n"
            with args.export_bootstrap_request.open("x", encoding="utf-8") as output:
                output.write(content)
            print(f"bootstrap request 已匯出：{args.export_bootstrap_request}")
            print("僅交接此檔；管理員必須核對 scope 與 studentObjectId。")
            return 0
        if args.import_bootstrap_result:
            import_bootstrap_result(args.import_bootstrap_result)
            print("已匯入三個 ACR outputs。請執行 azd deploy，不要重跑 azd provision。")
            return 0
        endpoint, model_deployment_name = read_local_settings()
        context = resolve_azure_context(endpoint, model_deployment_name)
        values = build_azd_values(endpoint, model_deployment_name, context)
        set_azd_values(values)
    except (ConfigurationError, OSError, UnicodeError, json.JSONDecodeError) as error:
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