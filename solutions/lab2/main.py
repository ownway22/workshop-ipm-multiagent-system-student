"""Lab 2 的本機進入點，提供 DevUI 與 Responses 兩種模式。

執行方式（cwd 必須是 `src`）：

    cd src && uv run python main.py              # DevUI：有畫面，看得到交接
    cd src && uv run python main.py --responses  # Responses：純 API，與 Lab 3 部署後相同

## 兩種模式的關係

兩者包的是**同一套**拓撲，差別只在對外的那層外皮：

| 模式      | 包裝方式                                      | 用途                |
| --------- | --------------------------------------------- | ------------------- |
| DevUI     | `serve(entities=[agent 實體, workflow 實體])` | 開發時觀察交接      |
| Responses | `ResponsesHostServer(workflow.as_agent(...))` | 與 Lab 3 部署後相同 |

`--responses` 存在的理由：通訊協定的選擇必須在**寫程式碼的階段**就確立，不是部署時才改。
先在本機用 Responses 跑起來，Lab 3 部署時才不會出現跨協定的意外。

## DevUI 會看到兩個實體

同一套四代理 Handoff 拓撲在 DevUI 上會出現**兩次**，分屬不同類別：

| 實體名稱                  | DevUI 類別 | 它是什麼                 |
| ------------------------- | ---------- | ------------------------ |
| `qvn-ipm-review`          | `agent`    | 整套工作流包成一個 agent |
| `qvn-ipm-review-workflow` | `workflow` | 未包裝的工作流本體       |

`qvn-ipm-review` 就是 Lab 3 部署後對外呈現的樣子。同時註冊兩個實體的目的，
是讓學員看到 DevUI **同時支援 agent 與 workflow 兩種類別**。

兩者來自同一份 `src/agents/` 定義檔，但必須是**兩個獨立建構的實例**：
共用同一個 `Workflow` 物件時，只要一邊還在串流，另一邊就會撞上框架的併發防護。

> ⚠️ workflow 類別實體的**每一次送出都是新的一輪**，不是交接續接，而且狀態會跨對話殘留。
> 詳見 `docs/02-lab2-multi-agent.md` 與 `references/known-gaps.md`。

> 本專案不提供 Invocations、WebSocket 或 A2A 端點。`WorkflowAgent` 並非 `Agent` 子類，
> 但 `ResponsesHostServer` 接受的是 `SupportsAgentRun` protocol，因此可直接包裝（實測）。

## 兩處必須明確指定、不可依賴預設值的設定

**`port`**：`agent_framework_devui.serve()` 的套件預設是 `8080`。講義與驗收標準都寫死了
連接埠，所以這裡必須明確傳入 `settings.devui_port`——否則套件哪天改了預設值，
現場三十個人會同時開錯網址。

**`auth_enabled`**：套件預設是 `True`，未給 token 時會**自動產生**一組，
學員得先回終端機找 token 才能送出第一則訊息。本 workshop 一律以 `False` 啟動。

> ⚠️ `auth_enabled=False` 是 **workshop 的權宜做法，不是生產建議**，正式環境必須保留驗證。
> 這裡關掉，是因為 DevUI 只繫結在你自己機器的 loopback 位址上，且是三十人同時操作的教學場景。

## 繫結位址與驗證是綁在一起的

`agent_framework_devui` 只允許在 **loopback** 位址上關閉驗證；繫結 `0.0.0.0` 又設
`auth_enabled=False` 會在啟動當下就拋 `ValueError`。反過來，繫結 loopback 會啟用
`Host` 標頭允許清單（只收 `127.0.0.1`、`localhost`、`::1`），其他 Host 一律回 400。

因此 DevUI 模式提供兩條路徑：

| 路徑 | 指令                              | host        | 驗證             |
| ---- | --------------------------------- | ----------- | ---------------- |
| 主要 | `uv run python main.py`           | `127.0.0.1` | 關閉             |
| 備援 | `uv run python main.py --forward` | `0.0.0.0`   | 開啟（需 token） |

備援路徑用於連接埠轉發（例如 Codespaces）導致主要路徑回 400 時。
實測 UI 首頁**不需** token 即可載入，token 只在 API 呼叫時檢查，所以可以先開頁面，
再把終端機印出的 token 貼進 DevUI 設定對話框的 `devui_auth_token` 欄位。
"""

import argparse
import os
import secrets

from config import DEFAULT_DEVUI_HOST, load_settings_or_exit
from workflows.handoff import (
    WORKFLOW_ENTITY_NAME,
    WORKFLOW_NAME,
    create_devui_entities,
    create_workflow,
)

#: 備援路徑的繫結位址。
_FORWARD_HOST = "0.0.0.0"  # noqa: S104 — 僅在使用者明確指定 --forward 時採用

#: 備援路徑的 token 環境變數。未設定時自動產生一組並印出。
_AUTH_TOKEN_ENV = "DEVUI_AUTH_TOKEN"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="啟動 Lab 2 的本機伺服器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "主要路徑失敗（瀏覽器顯示 400 Bad Request）時，改用：\n"
            "    uv run python main.py --forward"
        ),
    )
    parser.add_argument(
        "--responses",
        action="store_true",
        help="以 Responses 通訊協定啟動（沒有 UI），與 Lab 3 部署後相同",
    )
    parser.add_argument(
        "--forward",
        action="store_true",
        help=f"DevUI 備援路徑：繫結 {_FORWARD_HOST} 並開啟 token 驗證，供連接埠轉發使用",
    )
    parser.add_argument(
        "--host",
        default=None,
        help=(
            "覆寫繫結位址。預設 loopback；容器內必須傳 0.0.0.0（見 `deploy/Dockerfile`）。"
            "DevUI 模式請改用 --forward，不要用本參數"
        ),
    )
    parser.add_argument(
        "--port", type=int, default=None, help="覆寫連接埠（預設取 PORT 環境變數或 .env）"
    )
    return parser.parse_args()


def _resolve_port(explicit: int | None, settings_port: int) -> int:
    """決定連接埠：命令列 > `PORT` 環境變數 > `.env`。

    `PORT` 這一層是給**容器**用的——託管平台以它告知應用該聽哪一個埠。
    本機開發通常沒有這個變數，會落到 `.env` 的值。
    """
    if explicit is not None:
        return explicit
    from_env = os.environ.get("PORT")
    return int(from_env) if from_env else settings_port


def _resolve_auth(forward: bool) -> tuple[str, bool, str | None]:
    """決定 `(host, auth_enabled, auth_token)`。

    這三個值必須一起決定：`auth_enabled=False` 只在 loopback 位址上合法，
    分開設定必然會組出啟動就失敗的搭配。
    """
    if not forward:
        return DEFAULT_DEVUI_HOST, False, None

    token = os.environ.get(_AUTH_TOKEN_ENV) or secrets.token_urlsafe(24)
    return _FORWARD_HOST, True, token


def _serve_responses(workflow, host: str, port: int) -> None:
    """以 Responses 通訊協定啟動。

    `workflow.as_agent()` 回傳的 `WorkflowAgent` **並非** `Agent` 子類（實測
    `isinstance(...) is False`），但 `ResponsesHostServer` 接受的是 `SupportsAgentRun`
    protocol，因此可以直接包裝——這就是「整個多代理系統對外看起來就像一個 agent」的落點。

    `ResponsesHostServer` 本身**就是** ASGI 應用（Starlette 子類），並自帶 `run()`。
    不可去取 `.app` 屬性再交給 uvicorn——那個屬性不存在。

    `run()` 的預設是 `0.0.0.0`，本檔一律**明確傳入** host，不依賴那個預設值：
    本機開發繫 loopback（避免把一個免驗證的模型端點暴露到區域網路），容器內才繫 `0.0.0.0`。

    來源 M14（Foundry Hosted Agents，Agent Framework 側 hosting 文件）：
    https://learn.microsoft.com/en-us/agent-framework/hosting/foundry-hosted-agent
    —— 說明如何以最少程式碼把任何 MAF agent 或 workflow 以 Responses protocol 暴露。
    """
    from agent_framework_foundry_hosting import ResponsesHostServer

    server = ResponsesHostServer(workflow.as_agent(name=WORKFLOW_NAME))
    server.run(host=host, port=port)


def main() -> None:
    args = _parse_args()
    settings = load_settings_or_exit()
    devui_host, auth_enabled, auth_token = _resolve_auth(args.forward)
    port = _resolve_port(args.port, settings.devui_port)

    from azure.identity.aio import AzureCliCredential

    # 伺服器自己管理事件迴圈，因此這裡不可用 `async with` 包 credential
    # ——那樣 credential 會在伺服器開始前就被關閉。交給行程結束時回收即可。
    #
    # 容器內沒有 `az login`，但 `AzureCliCredential` 這一行不用改：託管環境會以
    # 受控識別提供權杖，並自動注入 `FOUNDRY_PROJECT_ENDPOINT`。
    credential = AzureCliCredential()

    print(f"Foundry 專案：{settings.foundry_project_endpoint}")
    print(f"模型部署　　：{settings.model_deployment_name}")

    if args.responses:
        # Responses 模式對外**只暴露一個**實體，與 Lab 3 部署後完全一致。
        workflow = create_workflow(
            settings.foundry_project_endpoint,
            settings.model_deployment_name,
            credential,
        )
        host = args.host or DEFAULT_DEVUI_HOST
        print(f"Responses　 ：http://{host}:{port}/responses")
        print("註：此模式沒有 UI，請以 curl 或 SDK 呼叫；協定與 Lab 3 部署後相同。")
        print()
        _serve_responses(workflow, host, port)
        return

    from agent_framework_devui import serve

    # DevUI 模式註冊**兩個**實體：同一套拓撲的兩種呈現。
    agent_entity, workflow_entity = create_devui_entities(
        settings.foundry_project_endpoint,
        settings.model_deployment_name,
        credential,
    )

    host = args.host or devui_host
    print(f"DevUI　　　 ：http://{'localhost' if not args.forward else host}:{port}")
    print(f"實體　　　　：{WORKFLOW_NAME}（agent 類別）")
    print(f"　　　　　　　{WORKFLOW_ENTITY_NAME}（workflow 類別）")
    if auth_enabled:
        print(f"存取權杖　　：{auth_token}")
        print("　　　　　　　（先開頁面，再貼進 DevUI 設定對話框的 devui_auth_token 欄位）")
    else:
        print("存取驗證　　：已關閉（workshop 權宜做法，不是生產建議）")
    print()

    serve(
        entities=[agent_entity, workflow_entity],
        port=port,
        host=host,
        auth_enabled=auth_enabled,
        auth_token=auth_token,
        auto_open=False,
    )


if __name__ == "__main__":
    main()
