# 已知限制

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

## 沿用的 workshop 限制

- 舊 DevUI 的 Handoff 多輪／新對話狀態問題仍以 [Lab 2 已知限制](../docs/02-lab2-multi-agent.md#devui-已知限制)
  為準；本次只核對候選 imports，沒有宣稱 DevUI 已修復。
- T12 跨領域完整性及 A365／exporter 問題沿用 [Lab 3 歷史驗證紀錄](../docs/03-lab3-deploy.md#短期-hotfix-的歷史驗證與範圍)。
  本次沒有重新部署或 Azure 驗收，不將那些限制改判 PASS。
