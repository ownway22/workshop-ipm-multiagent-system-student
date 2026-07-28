"""前置檢查 5–6：專案端點可達性與模型能力實測。

檢查 6 是整份前置檢查中**最關鍵**的一項：它 MUST 以**實際呼叫**驗證 tool calling 與
structured outputs 兩項能力，MUST NOT 只檢查部署是否存在（FR-006）。

不鎖定型號（FR-007）：`gpt-5.4-mini` 只是前置作業文件中的**建議**型號，唯一判定依據是
下方兩項能力實測。本模組 MUST NOT 出現任何寫死的模型型號。
"""

import asyncio

from pydantic import BaseModel, Field

from preflight import CheckResult

#: 兩項能力實測的逾時（秒）。模型冷啟動可能較慢，但超過這個時間就該當成環境問題處理。
_CAPABILITY_TIMEOUT_SECONDS = 90


class _CapabilityProbe(BaseModel):
    """structured outputs 實測用的最小 schema。

    刻意做得極簡：本檢查要驗的是「模型能不能回傳可解析的結構化輸出」，
    不是模型的分析品質。
    """

    city: str = Field(description="城市名稱")
    is_capital: bool = Field(description="是否為首都")


def check_project_endpoint(endpoint: str, deployment_name: str) -> CheckResult:
    """檢查 5：以開發者憑證連線專案端點。

    MUST 使用 `AzureCliCredential`，MUST NOT 使用 API key（FR-005、憲章原則 I 的過時訊號）。
    """
    try:
        asyncio.run(_probe_endpoint(endpoint, deployment_name))
    except Exception as error:  # noqa: BLE001 - 需一次呈現所有失敗原因，不中斷其餘檢查
        return CheckResult(
            index=5,
            title="專案端點",
            passed=False,
            detail=f"無法以開發者憑證連線：{_summarize(error)}",
            impact="端點不可達時，Lab 1 之後的所有步驟都無法進行。",
            remediation=_endpoint_remediation(str(error)),
        )

    return CheckResult(index=5, title="專案端點", passed=True, detail="可連線")


def _endpoint_remediation(message: str) -> list[str]:
    """依實際錯誤內容給出對症的修復步驟。"""
    # 企業租戶常見情形：Conditional Access 的 authentication flows 政策讓 az login 取得的
    # refresh token 對 https://ai.azure.com 這個 scope 永遠無效，錯誤碼固定為 AADSTS530036。
    # 一般的「重新登入」救不了，必須帶 --scope 重新登入（2026-07-27 實測確認）。
    if "AADSTS530036" in message or "Conditional Access" in message:
        return [
            "你的租戶套用了 Conditional Access 的 authentication flows 政策，",
            "一般的 az login 取得的權杖對 AI 端點永遠無效。MUST 帶 --scope 重新登入：",
            "    az logout",
            '    az login --scope "https://ai.azure.com/.default"',
            "（若你有多個租戶，再加上 --tenant <你的 tenant ID>）",
        ]
    return [
        "確認 .env 的 FOUNDRY_PROJECT_ENDPOINT 與 portal 上的「專案端點」逐字相同。",
        "確認登入的租戶與專案所在租戶一致：az account show --query tenantId",
        "重新登入：az login --use-device-code",
    ]


def _summarize(error: Exception) -> str:
    """把 SDK 的巢狀例外壓成一行可讀訊息。

    `ChatClientException` 會把底層錯誤全文（含多行修復指引）包進 args，直接印出會淹沒版面。
    """
    text = " ".join(str(error).split())
    return text if len(text) <= 220 else f"{text[:220]}…"


def check_model_capabilities(endpoint: str, deployment_name: str) -> CheckResult:
    """檢查 6：以實際呼叫驗證 tool calling 與 structured outputs。"""
    try:
        tool_calling_ok, structured_ok, notes = asyncio.run(
            _probe_capabilities(endpoint, deployment_name)
        )
    except Exception as error:  # noqa: BLE001 - 同上
        return CheckResult(
            index=6,
            title="模型能力",
            passed=False,
            detail=f"實測失敗：{_summarize(error)}",
            impact="無法確認模型能力，Lab 2 的 Handoff 與結構化輸出可能全盤失敗。",
            remediation=_deployment_remediation(deployment_name),
        )

    if tool_calling_ok and structured_ok:
        return CheckResult(
            index=6,
            title="模型能力",
            passed=True,
            detail="tool calling、structured outputs",
        )

    missing = []
    if not tool_calling_ok:
        missing.append("tool calling")
    if not structured_ok:
        missing.append("structured outputs")

    # 兩項能力同時失敗且錯誤是憑證問題時，真正的原因不是模型不支援，而是根本沒呼叫成功。
    # 不區分這兩者會把學員引到錯的方向（去重新部署模型）。
    auth_failure = "AADSTS" in notes or "ClientAuthenticationError" in notes
    if auth_failure:
        return CheckResult(
            index=6,
            title="模型能力",
            passed=False,
            detail="無法實測：憑證無效，呼叫未抵達模型（不是模型不支援）",
            impact="先修復檢查 5 的憑證問題，本項才能得到有效結論。",
            remediation=_endpoint_remediation(notes),
        )

    return CheckResult(
        index=6,
        title="模型能力",
        passed=False,
        detail=f"部署 {deployment_name} 不支援：{'、'.join(missing)}",
        impact=(
            "tool calling 不支援 → Lab 2 的 Handoff 完全無法運作（框架靠注入的交接工具交棒）。"
            "structured outputs 不支援 → SpecialistReview 資料契約失效。"
        ),
        remediation=_deployment_remediation(deployment_name),
    )


def _deployment_remediation(deployment_name: str) -> list[str]:
    """模型部署相關失敗的共用修復指引（編號步驟，FR-006）。"""
    return [
        f"目前 .env 指定的部署名稱是「{deployment_name}」。請確認它存在且支援上述能力。",
        "改用另一個部署的步驟：",
        "  1. 開啟 Foundry portal → 你的專案 → Deployments。",
        "  2. 若沒有合適的部署，按 Deploy model → 選擇建議型號 gpt-5.4-mini。",
        "     （型號僅為建議；唯一判定依據是本檢查的能力實測結果。）",
        "  3. 部署完成後複製「部署名稱」。",
        "  4. 把 .env 的 MODEL_DEPLOYMENT_NAME 換成該名稱。",
        "  5. 重新執行本前置檢查。",
    ]


def _build_credential():
    """建立開發者憑證。

    本機執行一律使用開發者身分憑證（FR-005），hosted agent 執行期則由平台指派的
    managed identity 接手。MUST NOT 使用 API key（憲章原則 I 的過時訊號）。
    """
    # 延後匯入：讓「環境變數缺漏」這類失敗能更快回報，不必先付出載入 SDK 的時間成本。
    from azure.identity.aio import AzureCliCredential

    return AzureCliCredential()


def _build_client(endpoint: str, deployment_name: str, credential):
    """建立以開發者憑證驗證的 `FoundryChatClient`。

    注意：`FoundryChatClient` **不是** async context manager（實測無 `__aenter__` / `close`），
    需要被關閉的是傳入的 credential。
    """
    from agent_framework.foundry import FoundryChatClient

    return FoundryChatClient(
        project_endpoint=endpoint,
        model=deployment_name,
        credential=credential,
    )


async def _probe_endpoint(endpoint: str, deployment_name: str) -> None:
    """以一次最小的對話呼叫確認端點可達且憑證有效。"""
    from agent_framework import ChatOptions, Message

    async with _build_credential() as credential:
        client = _build_client(endpoint, deployment_name, credential)
        await asyncio.wait_for(
            client.get_response(
                [Message("user", "回覆兩個字：可用")],
                options=ChatOptions(max_tokens=32),
            ),
            timeout=_CAPABILITY_TIMEOUT_SECONDS,
        )


async def _probe_capabilities(endpoint: str, deployment_name: str) -> tuple[bool, bool, str]:
    """實測兩項能力，回傳 `(tool_calling_ok, structured_ok, 補充說明)`。"""
    from agent_framework import ChatOptions, Message, tool

    notes: list[str] = []
    invoked = {"called": False}

    @tool(description="查詢指定城市目前的天氣。這是前置檢查專用的假資料，不對外連線。")
    def get_weather(city: str) -> str:
        """回傳固定字串。實際被呼叫即代表模型支援 tool calling。"""
        invoked["called"] = True
        return f"{city} 目前晴天，攝氏 26 度。"

    structured_ok = False
    async with _build_credential() as credential:
        client = _build_client(endpoint, deployment_name, credential)

        # 子項 1：tool calling。
        # 判準刻意設為「函式真的被叫到」而非解析回應中的 content 型別——前者是端到端事實，
        # 後者會隨 SDK 內部表示法變動。
        try:
            await asyncio.wait_for(
                client.get_response(
                    [Message("user", "台北現在天氣如何？請使用可用的工具查詢。")],
                    options=ChatOptions(tools=[get_weather], max_tokens=256),
                ),
                timeout=_CAPABILITY_TIMEOUT_SECONDS,
            )
        except Exception as error:  # noqa: BLE001 - 兩個子項互相獨立，其一失敗不應中斷另一項
            notes.append(f"tool calling 呼叫發生錯誤：{type(error).__name__}: {error}")

        # 子項 2：structured outputs。
        try:
            response = await asyncio.wait_for(
                client.get_response(
                    [Message("user", "台北是不是台灣的首都？請依指定格式回覆。")],
                    options=ChatOptions(
                        response_format=_CapabilityProbe,
                        max_tokens=256,
                    ),
                ),
                timeout=_CAPABILITY_TIMEOUT_SECONDS,
            )
            structured_ok = isinstance(response.value, _CapabilityProbe)
            if not structured_ok:
                notes.append("回應無法解析為指定的 schema")
        except Exception as error:  # noqa: BLE001 - 同上
            notes.append(f"structured outputs 呼叫發生錯誤：{type(error).__name__}: {error}")

    return invoked["called"], structured_ok, "；".join(notes)
