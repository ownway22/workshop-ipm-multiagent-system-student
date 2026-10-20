"""Offline bootstrap contracts. Every az/azd call is mocked, including PowerShell."""

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "deploy" / "scripts"
SPEC = importlib.util.spec_from_file_location(
    "lab3_configuration", SCRIPTS / "configure_azd_env.py"
)
assert SPEC is not None and SPEC.loader is not None
configure = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = configure
SPEC.loader.exec_module(configure)

import preflight  # noqa: E402
from preflight import azure_checks  # noqa: E402

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
TENANT = "22222222-2222-2222-2222-222222222222"
STUDENT = "33333333-3333-3333-3333-333333333333"
PROJECT_IDENTITY = "55555555-5555-5555-5555-555555555555"
SCOPE = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-offline"
ACCOUNT_ID = f"{SCOPE}/providers/Microsoft.CognitiveServices/accounts/offline-foundry"
PROJECT_ID = f"{ACCOUNT_ID}/projects/offline-project"
ENDPOINT = "https://offline-foundry.services.ai.azure.com/api/projects/offline-project"
REQUEST = {
    "schemaVersion": 1,
    "subscriptionId": SUBSCRIPTION,
    "resourceGroup": "rg-offline",
    "environmentName": "original-student-env",
    "location": "eastus",
    "accountName": "offline-foundry",
    "projectName": "offline-project",
    "studentObjectId": STUDENT,
}
ENVIRONMENT = {
    "AZURE_ENV_NAME": REQUEST["environmentName"],
    "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
    "AZURE_TENANT_ID": TENANT,
    "AZURE_LOCATION": "eastus",
    "AZURE_RESOURCE_GROUP": "rg-offline",
    "AZURE_AI_ACCOUNT_NAME": "offline-foundry",
    "AZURE_AI_PROJECT_NAME": "offline-project",
    "AZURE_AI_PROJECT_ID": PROJECT_ID,
    "AZURE_AI_PROJECT_ENDPOINT": ENDPOINT,
    "FOUNDRY_PROJECT_ENDPOINT": ENDPOINT,
    "AZURE_AI_MODEL_DEPLOYMENT_NAME": "offline-model",
    "MODEL_DEPLOYMENT_NAME": "offline-model",
}


def outputs_for(request=REQUEST):
    scope = (
        f"/subscriptions/{request['subscriptionId']}"
        f"/resourceGroups/{request['resourceGroup']}"
    )
    return {
        "AZURE_CONTAINER_REGISTRY_ENDPOINT": "crsynthetic123.azurecr.io",
        "AZURE_CONTAINER_REGISTRY_RESOURCE_ID":
            f"{scope}/providers/Microsoft.ContainerRegistry/registries/crsynthetic123",
        "AZURE_ACR_CONNECTION_NAME": "acr-abcdefghijklm",
    }


def result_for(request=REQUEST):
    return {"schemaVersion": 1, "request": copy.deepcopy(request),
            "outputs": outputs_for(request)}


class MockCli:
    def __init__(self):
        self.calls = []
        self.values = dict(ENVIRONMENT)
        self.overrides = {}
        self.azure = {
            ("cognitiveservices", "account", "list"):
                [{"location": "eastus", "rg": "rg-offline", "id": ACCOUNT_ID}],
            ("account", "show"): {"subscriptionId": SUBSCRIPTION, "tenantId": TENANT},
            ("group", "show"): "eastus",
            ("resource", "show"): {"id": PROJECT_ID},
            ("cognitiveservices", "account", "deployment", "show"): {"name": "offline-model"},
            ("ad", "signed-in-user", "show"): STUDENT,
        }

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        for prefix, response in self.overrides.items():
            if command[:len(prefix)] == list(prefix):
                return subprocess.CompletedProcess(command, *response)
        if command[0] == "az-mock":
            assert command[-2:] == ["--output", "json"]
            for prefix, value in self.azure.items():
                if command[1:1 + len(prefix)] == list(prefix):
                    return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
        elif command[:3] == ["azd", "env", "get-value"]:
            assert kwargs["cwd"] == configure.DEPLOY_DIRECTORY
            assert "--no-prompt" in command and "--output" not in command
            value = self.values.get(command[3], "")
            return subprocess.CompletedProcess(command, 0, value + "\n", "")
        elif command[:3] == ["azd", "env", "set"]:
            assert kwargs["cwd"] == configure.DEPLOY_DIRECTORY
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"Unmocked command forbidden: {command}")

    @property
    def writes(self):
        return [command for command, _ in self.calls if command[:3] == ["azd", "env", "set"]]


@pytest.fixture
def cli(monkeypatch, tmp_path):
    fake = MockCli()
    local_env = tmp_path / "student settings.env"
    local_env.write_text(
        f"FOUNDRY_PROJECT_ENDPOINT={ENDPOINT}\nMODEL_DEPLOYMENT_NAME=offline-model\n"
        "UNRELATED_SECRET=do-not-export-this-value\n", encoding="utf-8"
    )
    read_settings = configure.read_local_settings
    monkeypatch.setattr(configure, "read_local_settings", lambda: read_settings(local_env))
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "az-mock" if name == "az" else None)
    monkeypatch.setattr(subprocess, "run", fake.run)
    return fake


def test_export_uses_original_environment_and_only_seven_fields(cli, tmp_path, monkeypatch, capsys):
    directory = tmp_path / "unrelated cwd with spaces"
    directory.mkdir()
    monkeypatch.chdir(directory)
    path = directory / "student request.json"
    assert configure.main(["--export-bootstrap-request", str(path)]) == 0
    assert json.loads(path.read_text()) == REQUEST
    assert "do-not-export" not in path.read_text() + capsys.readouterr().out
    assert cli.writes == []
    assert all("get-values" not in command for command, _ in cli.calls)
    assert all(command[command.index("--environment") + 1] == REQUEST["environmentName"]
               for command, _ in cli.calls if "--environment" in command)


def test_default_self_service_configuration_is_preserved(cli):
    assert configure.main([]) == 0
    assert len(cli.writes) == 1
    assert "--environment" not in cli.writes[0]
    assert set(cli.writes[0][3:-1]) == {
        f"{key}={value}" for key, value in ENVIRONMENT.items() if key != "AZURE_ENV_NAME"
    }


def test_import_only_three_outputs_to_original_environment(cli, tmp_path):
    path = tmp_path / "admin result.json"
    path.write_text(json.dumps(result_for()), encoding="utf-8-sig")
    assert configure.main(["--import-bootstrap-result", str(path)]) == 0
    assert cli.writes == [[
        "azd", "env", "set",
        *(f"{key}={value}" for key, value in outputs_for().items()),
        "--environment", REQUEST["environmentName"], "--no-prompt",
    ]]
    assert not any("provision" in command or "deploy" in command for command, _ in cli.calls)


@pytest.mark.parametrize("field,value", [
    ("environmentName", "administrator-env"),
    ("subscriptionId", "99999999-9999-9999-9999-999999999999"),
    ("resourceGroup", "rg-other"),
    ("location", "westus"),
    ("accountName", "other-account"),
    ("projectName", "other-project"),
    ("studentObjectId", "66666666-6666-6666-6666-666666666666"),
])
def test_import_rejects_different_environment_scope_or_student(cli, tmp_path, field, value):
    request = {**REQUEST, field: value}
    path = tmp_path / "mismatched.json"
    path.write_text(json.dumps(result_for(request)))
    assert configure.main(["--import-bootstrap-result", str(path)]) == 1
    assert cli.writes == []


@pytest.mark.parametrize("variable", [key for key in ENVIRONMENT if key != "AZURE_ENV_NAME"])
def test_export_refuses_inconsistent_azd_environment(cli, tmp_path, variable):
    cli.values[variable] = "different-target"
    path = tmp_path / "request.json"
    assert configure.main(["--export-bootstrap-request", str(path)]) == 1
    assert not path.exists() and cli.writes == []


@pytest.mark.parametrize("raw", ["", "null", "[]", "{}", "not-json"])
def test_import_rejects_empty_or_invalid_json_before_any_cli(cli, tmp_path, raw):
    path = tmp_path / "invalid.json"
    path.write_text(raw)
    assert configure.main(["--import-bootstrap-result", str(path)]) == 1
    assert cli.calls == []


@pytest.mark.parametrize("case", [
    "missing-request", "missing-output", "empty-output", "wrong-scope",
    "wrong-endpoint", "extra-env-key", "schema-bool", "request-schema-bool",
    "bad-student", "request-template",
])
def test_import_rejects_malformed_envelopes(cli, tmp_path, case):
    result = result_for()
    if case == "missing-request":
        del result["request"]["environmentName"]
    elif case == "missing-output":
        del result["outputs"]["AZURE_ACR_CONNECTION_NAME"]
    elif case == "empty-output":
        result["outputs"]["AZURE_ACR_CONNECTION_NAME"] = ""
    elif case == "wrong-scope":
        result["outputs"]["AZURE_CONTAINER_REGISTRY_RESOURCE_ID"] = "/wrong/scope/registry"
    elif case == "wrong-endpoint":
        result["outputs"]["AZURE_CONTAINER_REGISTRY_ENDPOINT"] = "attacker.invalid"
    elif case == "extra-env-key":
        result["outputs"]["AZURE_PRINCIPAL_ID"] = "administrator"
    elif case == "schema-bool":
        result["schemaVersion"] = True
    elif case == "request-schema-bool":
        result["request"]["schemaVersion"] = True
    elif case == "bad-student":
        result["request"]["studentObjectId"] = "not-a-guid"
    elif case == "request-template":
        result["request"]["templatePath"] = "unreviewed.bicep"
    path = tmp_path / "malformed.json"
    path.write_text(json.dumps(result))
    assert configure.main(["--import-bootstrap-result", str(path)]) == 1
    assert cli.calls == []


@pytest.mark.parametrize("prefix,code,stdout,stderr", [
    (("azd", "env", "get-value"), 1, "", "No environment selected"),
    (("azd", "env", "get-value"), 0, "", ""),
    (("az-mock", "account", "show"), 1, "", "403 AuthorizationFailed: ABAC condition denied"),
    (("az-mock", "resource", "show"), 0, "", ""),
    (("az-mock", "resource", "show"), 0, "not-json", ""),
    (("az-mock", "resource", "show"), 0, "{}", ""),
    (("az-mock", "group", "show"), 0, "null", ""),
    (("az-mock", "ad", "signed-in-user", "show"), 0, "null", ""),
])
def test_export_fails_closed_without_retry(cli, tmp_path, prefix, code, stdout, stderr, capsys):
    cli.overrides[prefix] = (code, stdout, stderr)
    path = tmp_path / "request.json"
    assert configure.main(["--export-bootstrap-request", str(path)]) == 1
    assert not path.exists() and cli.writes == []
    matching = [command for command, _ in cli.calls if command[:len(prefix)] == list(prefix)]
    assert len(matching) == 1
    if stderr:
        assert stderr in capsys.readouterr().err


def test_import_does_not_report_success_when_azd_set_fails(cli, tmp_path, capsys):
    cli.overrides[("azd", "env", "set")] = (9, "", "original azd failure")
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result_for()))
    assert configure.main(["--import-bootstrap-result", str(path)]) == 1
    captured = capsys.readouterr()
    assert "original azd failure" in captured.err
    assert "azd deploy" not in captured.out
    assert len(cli.writes) == 1


def test_export_never_overwrites_an_existing_request(cli, tmp_path):
    path = tmp_path / "existing.json"
    path.write_text("original")
    assert configure.main(["--export-bootstrap-request", str(path)]) == 1
    assert path.read_text() == "original"


def test_preflight_role_presence_is_not_effective_authorization(monkeypatch):
    location = azure_checks.ProjectLocation(
        "offline-foundry", "offline-project", "rg-offline", "eastus", ACCOUNT_ID
    )
    calls = []
    monkeypatch.setattr(azure_checks, "_current_principal_id", lambda: STUDENT)

    def role_list(args):
        calls.append(args)
        return [{"condition": "synthetic condition"}]

    monkeypatch.setattr(azure_checks, "run_az_json", role_list)
    result = azure_checks.check_role_assignments(location)
    assert result.passed
    assert "ABAC" in result.detail and "ACR" in result.detail
    assert all("--include-inherited" in args and "--include-groups" in args for args in calls)


@pytest.mark.parametrize("relative", [
    "scripts/configure_azd_env.py", "scripts/Invoke-Lab3Bootstrap.ps1",
    "infra/main.bicep", "infra/main.parameters.json", "azure.yaml",
])
def test_solution_deploy_surfaces_stay_identical(relative):
    assert (ROOT / "deploy" / relative).read_bytes() == (
        ROOT / "solutions" / "lab3" / "deploy" / relative
    ).read_bytes()


FAKE_AZ = r'''
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:2] == ["account", "show"]:
    operation = "account"
elif args[:2] == ["group", "show"]:
    operation = "group"
elif args[:2] == ["resource", "show"]:
    operation = "project"
elif args[:3] == ["deployment", "group", "create"]:
    operation = "deployment"
else:
    raise SystemExit("Unmocked az command forbidden: " + repr(args))
record = {"operation": operation, "args": args}
if operation == "deployment":
    parameter_path = args[args.index("--parameters") + 1]
    assert parameter_path.startswith("@")
    record["parameters"] = json.loads(Path(parameter_path[1:]).read_text(encoding="utf-8-sig"))
with Path(os.environ["FAKE_AZ_LOG"]).open("a", encoding="utf-8") as output:
    output.write(json.dumps(record) + "\n")
responses = json.loads(Path(os.environ["FAKE_AZ_RESPONSES"]).read_text(encoding="utf-8"))
response = responses[operation]
sys.stdout.write(response.get("stdout", json.dumps(response.get("value"))))
sys.stderr.write(response.get("stderr", ""))
raise SystemExit(response.get("code", 0))
'''


@pytest.fixture(params=["powershell", "pwsh"])
def powershell_bootstrap(request, tmp_path):
    if os.name != "nt":
        pytest.skip("Windows native-command contract runs on Windows CI")
    engine = shutil.which(request.param)
    if engine is None:
        pytest.skip(f"{request.param} is not installed")
    reviewed = tmp_path / "reviewed repo with spaces" / "deploy"
    scripts = reviewed / "scripts"
    scripts.mkdir(parents=True)
    (reviewed / "infra").mkdir()
    shutil.copy2(SCRIPTS / "Invoke-Lab3Bootstrap.ps1", scripts)
    shutil.copy2(ROOT / "deploy" / "infra" / "main.bicep", reviewed / "infra")
    cwd = tmp_path / "unrelated working directory"
    cwd.mkdir()
    fake_bin = tmp_path / "fake CLI with spaces"
    fake_bin.mkdir()
    (fake_bin / "fake_az.py").write_text(FAKE_AZ, encoding="utf-8")
    (fake_bin / "az.cmd").write_text(
        f'@echo off\n"{sys.executable}" "%~dp0fake_az.py" %*\n', encoding="ascii"
    )
    (fake_bin / "azd.cmd").write_text(
        "@echo off\necho Unexpected azd command forbidden 1>&2\nexit /b 97\n",
        encoding="ascii",
    )
    request_path = tmp_path / "student request.json"
    result_path = tmp_path / "bootstrap result.json"
    responses_path = tmp_path / "fake responses.json"
    log_path = tmp_path / "fake calls.jsonl"
    environment = {
        **os.environ,
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "AZURE_CONFIG_DIR": str(tmp_path / "no azure credentials"),
        "AZD_CONFIG_DIR": str(tmp_path / "no azd credentials"),
        "BOOTSTRAP_SCRIPT": str(scripts / "Invoke-Lab3Bootstrap.ps1"),
        "BOOTSTRAP_REQUEST": str(request_path),
        "BOOTSTRAP_RESULT": str(result_path),
        "BOOTSTRAP_CONFIRM": "BOOTSTRAP",
        "FAKE_AZ_RESPONSES": str(responses_path),
        "FAKE_AZ_LOG": str(log_path),
        "FAKE_AZ_PATH": str(fake_bin / "az.cmd"),
    }
    responses = {
        "account": {"value": {
            "id": SUBSCRIPTION, "tenantId": TENANT,
            "user": {"name": "offline-admin@example.invalid", "type": "user"},
        }},
        "group": {"value": {"id": SCOPE, "location": "eastus"}},
        "project": {"value": {
            "id": PROJECT_ID, "identity": {"principalId": PROJECT_IDENTITY},
        }},
        "deployment": {"value": {
            "id": f"{SCOPE}/providers/Microsoft.Resources/deployments/lab3-bootstrap",
            "properties": {"provisioningState": "Succeeded", "outputs": {
                key: {"type": "String", "value": value}
                for key, value in outputs_for().items()
            }},
        }},
    }

    def run(case="success"):
        payload = copy.deepcopy(REQUEST)
        if case == "missing-field":
            del payload["environmentName"]
        elif case == "request-template":
            payload["templatePath"] = "unreviewed.bicep"
        elif case == "request-array":
            payload = [payload]
        elif case == "request-schema-bool":
            payload["schemaVersion"] = True
        elif case == "request-field-case":
            payload["StudentObjectId"] = payload.pop("studentObjectId")
        elif case == "wrong-subscription":
            responses["account"]["value"]["id"] = "99999999-9999-9999-9999-999999999999"
        elif case == "wrong-group":
            responses["group"]["value"]["id"] = "/wrong/group"
        elif case == "wrong-project":
            responses["project"]["value"]["id"] = "/wrong/project"
        elif case == "wrong-location":
            responses["group"]["value"]["location"] = "westus"
        elif case == "cancel":
            environment["BOOTSTRAP_CONFIRM"] = "NO"
        elif case == "missing-template":
            (reviewed / "infra" / "main.bicep").unlink()
        elif case == "existing-result":
            result_path.write_text("keep original")
        elif case == "missing-parent":
            environment["BOOTSTRAP_RESULT"] = str(tmp_path / "absent" / "result.json")
        elif case in {"abac", "abac-with-json"}:
            responses["deployment"] = {
                "code": 1, "stdout": (
                    json.dumps(responses["deployment"]["value"]) if case == "abac-with-json" else ""
                ),
                "stderr": "ERROR: 403 AuthorizationFailed: ABAC condition denied\n",
            }
        elif case == "array-json":
            responses["deployment"] = {
                "stdout": json.dumps([responses["deployment"]["value"]])
            }
        elif case in {"empty-json", "invalid-json", "null-json"}:
            responses["deployment"] = {
                "stdout": {"empty-json": "", "invalid-json": "not-json", "null-json": "null"}[case]
            }
        elif case == "failed-state":
            responses["deployment"]["value"]["properties"]["provisioningState"] = "Failed"
        elif case == "wrong-deployment":
            responses["deployment"]["value"]["id"] = "/wrong/deployment"
        elif case == "missing-output":
            del responses["deployment"]["value"]["properties"]["outputs"]["AZURE_ACR_CONNECTION_NAME"]
        elif case == "empty-output":
            responses["deployment"]["value"]["properties"]["outputs"]["AZURE_ACR_CONNECTION_NAME"]["value"] = ""
        elif case == "wrong-output-scope":
            responses["deployment"]["value"]["properties"]["outputs"]["AZURE_CONTAINER_REGISTRY_RESOURCE_ID"]["value"] = "/wrong/registry"
        elif case == "warning":
            responses["deployment"]["stderr"] = "WARNING: synthetic native stderr\n"
        request_text = {"empty-request": "", "invalid-request": "not-json"}.get(
            case, json.dumps(payload)
        )
        request_path.write_text(request_text, encoding="utf-8")
        if case == "missing-request":
            request_path.unlink()
        responses_path.write_text(json.dumps(responses), encoding="utf-8")
        command = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
if ((Get-Command az).Source -ne $env:FAKE_AZ_PATH) { throw 'Real az is forbidden' }
if ((Get-Command azd).Source -notlike '*fake CLI with spaces*') { throw 'Real azd is forbidden' }
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $true
}
function Read-Host { param([string] $Prompt) return $env:BOOTSTRAP_CONFIRM }
& $env:BOOTSTRAP_SCRIPT -RequestPath $env:BOOTSTRAP_REQUEST -ResultPath $env:BOOTSTRAP_RESULT
'''
        completed = subprocess.run(
            [engine, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=cwd, env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False, timeout=40,
        )
        calls = [json.loads(line) for line in log_path.read_text().splitlines()] if log_path.exists() else []
        return completed, result_path, calls, reviewed

    return run


@pytest.mark.parametrize("case", ["success", "warning"])
def test_powershell_bootstrap_keeps_executor_separate_and_uses_reviewed_template(powershell_bootstrap, case):
    completed, path, calls, reviewed = powershell_bootstrap(case)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(path.read_text(encoding="utf-8-sig")) == result_for()
    deployment = calls[-1]
    args = deployment["args"]
    assert Path(args[args.index("--template-file") + 1]) == reviewed / "infra" / "main.bicep"
    parameters = deployment["parameters"]["parameters"]
    assert parameters["principalId"] == {"value": STUDENT}
    assert parameters["principalType"] == {"value": "User"}
    assert parameters["environmentName"] == {"value": REQUEST["environmentName"]}
    assert "offline-admin@example.invalid" in completed.stdout
    assert STUDENT in completed.stdout and PROJECT_IDENTITY in completed.stdout
    assert [call["operation"] for call in calls] == ["account", "group", "project", "deployment"]
    if case == "warning":
        assert "synthetic native stderr" in completed.stderr


@pytest.mark.parametrize("case", [
    "missing-field", "request-template", "request-array", "request-schema-bool", "request-field-case",
    "empty-request", "invalid-request", "missing-request", "wrong-subscription", "wrong-group",
    "wrong-project", "wrong-location", "cancel", "missing-template",
    "missing-parent", "abac", "abac-with-json", "empty-json", "invalid-json", "null-json", "array-json",
    "failed-state", "wrong-deployment", "missing-output", "empty-output",
    "wrong-output-scope", "existing-result",
])
def test_powershell_failure_never_publishes_success_or_retries(powershell_bootstrap, case):
    completed, path, calls, _ = powershell_bootstrap(case)
    assert completed.returncode != 0, completed.stdout + completed.stderr
    if case == "existing-result":
        assert path.read_text() == "keep original"
    else:
        assert not path.exists()
    assert "Bootstrap succeeded" not in completed.stdout
    deployments = [call for call in calls if call["operation"] == "deployment"]
    assert len(deployments) <= 1
    if case in {"abac", "abac-with-json"}:
        assert "403 AuthorizationFailed: ABAC condition denied" in completed.stderr
    if case in {
        "missing-field", "request-template", "request-array", "request-schema-bool", "request-field-case",
        "empty-request", "invalid-request", "missing-request", "missing-template",
        "missing-parent", "existing-result",
    }:
        assert calls == []
    elif case in {"wrong-subscription", "wrong-group", "wrong-project", "wrong-location", "cancel"}:
        assert deployments == []
    else:
        assert len(deployments) == 1
