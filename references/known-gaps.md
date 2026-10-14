# 已知限制

截至 2026-10-14，PR #6 已合併，正式 SDK 升級與既有 runtime 補強完成，
仍為 HandoffResponsesHost／Responses 2.0.0，**不是 native hosting 遷移**。
完整 regression 54 tests／117 subtests 及實際 source build／smoke 通過；
獨立 `qvn-ipm-review-sdk6-validation` regular version 6 有有限 Azure／Playground 證據，
原 `qvn-ipm-review` 路由未隨 merge 切換，不混稱兩個 agent 的版本。
操作入口見 [Lab 3](../docs/03-lab3-deploy.md)，日期、image、FAIL／skip 見
[升級報告](../docs/native-hosting-migration.md)。

## #6 接受限制後的 T01 合併門檻

- Canonical T01 1,596 字元的 API 三輪通過；GUI 指定流程用 1,646 字元 raw CSV 變體，
  不是同一輸入的 GUI／API 都通過。
- Canonical GUI 證據缺口及額外自由詢問短回覆由
  [#7](https://github.com/ownway22/workshop-ipm-multiagent-system-student/issues/7) 追蹤；
  不保證任意追問全部通過。
- 新 image T03／T10／T12 未重測、不標 PASS；T12 漏 Architect 的歷史限制保留。
- 完整 12 題、Lab 2 → Lab 3 → Lab 4 及 tracing 仍是課程目標，不是 #6 已全部驗收。
- 四位內部模型 history 皆 store=False，外層 Responses 保存仍開啟；首次升級新建
  session／conversation，不自動遷移或刪除舊 checkpoint，不保證任意舊 state 相容。

## Native hosting：遷移尚未通過可行性門檻

2026-10-11，以 `agent-framework-foundry-hosting==1.0.0b261008` 的 released API
及真正 HandoffBuilder 經 native Responses ASGI endpoint 實測：

- 第一輪保留完整專家輸出並等待 `HandoffAgentUserRequest`，第二輪普通文字「繼續」
  在 stream／nonstream 均失敗。`WorkflowTurn(input=...)` 不是 pending reply；
  `get_workflow_responses()` 只解碼 correlated function results／approvals，不能自行配對純文字。
- 官方 native lineage 是線性、CAS 與 consume-once 設計；第二次從同一 response 分岔被拒絕。
  這是已查證的設計限制，不直接判為 SDK bug。Maintainer 未批准取代 #4 的分岔契約。
- `store=False` 不允許可續接 pause；Primary 的 inner storage、named conversation 切換、
  resource ownership 及 error contract 也不能假設與現有 adapter 相同。

詳見 [可重現測試、版本與實測／未測邊界](../docs/native-hosting-migration.md)。
正式 [進入點](../src/main.py)、[host](../src/workflows/responses.py)、
[相依與 lock](../src/pyproject.toml)、[完成版](../solutions/lab2/) 均未遷移。

## 現有 adapter 的 SDK 相容性與隔離邊界

CreateResponse／input items／event 的 model → dict，以及候選移除 `atext_content()` 的
相容性已修復；baseline 與隔離候選皆 **43 tests、103 subtests 通過**，
包含真正 Handoff 三輪純文字、完整輸出、host 重建、分岔、原完整 regression 與 strict restore。
本階段正式新版也保留全部原斷言並通過；預設 host 不變，救援程式同步，
**不是 native hosting 已完成**。

不同使用者重用既有 conversation ID，但沒有自己的 checkpoint 時，
兩環境均明確拒絕：不回傳前位內容、不呼叫模型、不消耗 pending request、不改寫 checkpoint。
各自合法 conversation 的多輪續接已測試。Maintainer 同意保留此既有邊界，
不要求跨使用者共用 ID；這不是 SDK upgrade regression，也不是 Foundry 平台授權隔離驗收。

## 沿用的 workshop 限制

- 舊 DevUI 的 Handoff 多輪／新對話狀態問題仍以 [Lab 2 已知限制](../docs/02-lab2-multi-agent.md#devui-已知限制)
  為準；本次只核對候選 imports，沒有宣稱 DevUI 已修復。
- T12 跨領域完整性及 A365／exporter 問題沿用 [Lab 3 歷史驗證紀錄](../docs/03-lab3-deploy.md#短期-hotfix-的歷史驗證與範圍)。
  #6 的有限部署／驗收不將歷史失敗改判 PASS。Telemetry／Application Insights 問題未自動解決，
  也沒有直接因果證據可將它們當成 Handoff 根因。
