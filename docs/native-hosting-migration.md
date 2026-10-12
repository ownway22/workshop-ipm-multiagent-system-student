# 正式 SDK 升級：保留 HandoffResponsesHost

## 目前階段：正式 pins 已升級，native host 未接入

2026-10-12，PR #5 已以一般 merge commit
`f880de981017d9995845f0735e915851c1bb4e15` 合併。
本階段從該最新 `main` 建立獨立 branch／worktree；沒有修改或刪除 #5 的 branch／worktree。
新 [Draft PR #6](https://github.com/ownway22/workshop-ipm-multiagent-system-student/pull/6)
尚未合併，也沒有部署。
正式 [manifest](../src/pyproject.toml) 與 [lock](../src/uv.lock) 採用 #5 已驗證的組合，
預設仍是 [HandoffResponsesHost](../src/workflows/responses.py)，不接入 native host。
以下是 source 的 dependency 升級，**不是 Azure 已部署、模型／Playground／Teams 已驗收**。

| 套件 | 舊正式版 → 新正式版 | 必要原因 |
| ---- | ------------------ | -------- |
| agent-framework-core | 1.12.1 → 1.21.0 | Foundry 1.14.1 要求的最低核心 |
| agent-framework-foundry | 1.10.3 → 1.14.1 | #5 已驗證的 Foundry client 組合 |
| agent-framework-orchestrations | 1.0.1 → 1.3.1 | 同組合的真正 HandoffBuilder |
| agent-framework-devui | 1.0.0b260721 → 1.0.0b261008 | 已驗證的 Lab 2 public API |
| agent-framework-foundry-hosting | 1.0.0b260722 → 1.0.0b261008 | 同組合相依；不改 host 選擇 |
| azure-ai-agentserver-core | 2.0.0b8 → 2.1.0 | Hosting／Responses 的最低相容需求 |
| azure-ai-agentserver-responses | 1.0.0b9 → 2.2.0b1 | 現有 adapter 的公開 response handler API |
| agent-framework-openai（間接） | 1.11.0 → 1.15.0 | Foundry 的最低需求 |
| azure-ai-agentserver-invocations（間接） | 1.0.0b7 → 1.1.0 | Hosting 的最低需求，不開 Invocations |
| msgspec（新增間接） | 無 → 0.22.0 | 新 core 必要相依 |
| regex（新增間接） | 無 → 2026.9.29 | 新 core 必要相依 |

逐項比較正式與 #5 候選 lock：除專案名稱外，**所有 resolved versions 完全一致**；
其他原 lock 版本都保留，沒有重新選 latest／無差別 upgrade。
Python minimum、uv、模型、prompts、拓撲、Bicep／RBAC 與 Teams 配置未改。
部署 manifest 只更新註解中的套件版本，Responses protocol **2.0.0** 值不變。
原 runtime 與 16 份 `src`／`solutions/lab2` Python 副本不需修改，仍完全一致。

### 正式新版驗證

```bash
cd src
uv lock --upgrade-package agent-framework-openai==1.15.0 \
  --upgrade-package azure-ai-agentserver-invocations==1.1.0 \
  --upgrade-package msgspec==0.22.0 --upgrade-package regex==2026.9.29
uv sync --locked --no-dev --python 3.13
uv sync --locked --group dev --python 3.13
uv lock --check
uv run --no-sync pytest -q --tb=short
uv run --no-sync python main.py --help
```

Locked prod／dev sync、CLI 成功；Linux Python **3.13.11**／uv **0.9.25**。
完整 regression：**45 passed、105 subtests passed**，保留原 43 tests／103 subtests，
新增離線 main credential 選擇、DevUI 實體建構及 public `serve` signature tests。
原測試的真正 Handoff 三輪、完整輸出、分岔、隔離、strict restore、
降級／timeout、取消與 metadata-only diagnostics 斷言不變，沒有 skip／xfail。
DevUI API 通過不等於真實模型下的 DevUI UX 已驗收；新版的實際對話行為仍須人工確認。

新版正式 image 的
**[GitHub-hosted run 38206422208](https://github.com/ownway22/workshop-ipm-multiagent-system-student/actions/runs/38206422208)
全部成功**，不是 #5 的舊 image／skipped run：

| 新版 container 證據 | 結果 |
| ------------------ | ---- |
| 實際受測 head SHA／image revision | `5c0f935093813b336cebddfd9c53ffa62c70f10e` |
| Image ID／platform | `sha256:8748803dc7a98f94e61040e10190277ada90fc24c24972de9b60927072fad157`；`linux/amd64` |
| 正式 build | 原 `deploy/Dockerfile`／新版正式 lock，`uv sync --locked --no-dev`；解析 119／安裝 112 packages |
| Runtime | base tag 的 Python 3.13.16、uv 0.9.25；build log 核對全部新版 SDK pins |
| Container entry | 原 `main.py --responses --host 0.0.0.0`；合成 env、mounts=[]、僅 `127.0.0.1:32768` |
| readiness | HTTP 200，`{"status":"healthy"}` |
| 空白 input／stream=false | HTTP 200，`status=failed`、`error.code=invalid_prompt`，不是 `server_error` |
| Logs／cleanup | input rejection log 已核對；僅本次 container ID stop／rm 並確認不存在 |
| Artifact | [handoff-container-smoke-38206422208-1](https://github.com/ownway22/workshop-ipm-multiagent-system-student/actions/runs/38206422208/artifacts/11727332609) |

SDK log 的 experimental／「resilience disabled」警告是未驗收的 crash-recovery 邊界，
不把成功 smoke 宣稱為 process hard-kill 後的可恢復性保證。
本次沒有送有效模型 prompt、registry login／image push、Azure secrets 或部署。
最後的文件回填不更改正式 build inputs，若 workflow 明確 skip，不能當成新的 image 驗證。
現有最小權限 PR workflow 未修改；native gate 留在隔離實驗、不混入正式 runner，
也沒有重跑已完成的 upstream 調查。

### Checkpoint 版本邊界、新 session 與 rollback

離線合成 probe 使用真正 Handoff 與現有 host，先在隔離的 #5 舊正式 lock
（core 1.12.1／orchestrations 1.0.1／Responses 1.0.0b9）建立第一輪 checkpoint，
再以本次正式新版環境及 fresh workflow 還原、續接第二／第三輪。
三輪皆 completed，完整專家文字／code fence 無遺失，原材料沒有重複 replay，
原 checkpoint 檔案 bytes 不變。這是**實際限定案例的跨版本相容證據**，不是只測新 checkpoint。

這不涵蓋真實 Foundry `service_session_id`、SDK 原始 provider payload、
實驗版 ledger、所有舊 checkpoint 型別或平台 session binding。
不保證任意舊 checkpoint 相容；不做自動轉換、清除 ID、退回較舊 checkpoint 或重開對話。
損毀／錯誤 checkpoint 仍走既有明確失敗路徑，原 strict restore tests 保留。

將來部署此 SDK 組合的第一次人工驗收**必須新 session／conversation**，
舊對話資料保留不動，不假設既有 sandbox 會自動切到新 image。
Rollback 應人工選擇 #5 合併版的程式／精確 lock／已驗證舊 image，
使用新 session／conversation 再驗收；不把新版 state 自動匯回舊版，也不刪除舊資料。
本次未部署／rollback、未變更 credentials，亦未認證 Azure／Playground／Teams／12 題，
不能把本機 fake regression 或容器空白輸入 smoke 宣稱為模型與業務流程驗收。

---

## PR #5 歷史：相容性準備與 native 隔離實驗

以下完整保留 #5 的查證／review／舊 image 證據，描述的是**升級前的歷史狀態**，
不是本階段重新執行，也不是說目前正式 manifest 仍未升級。#5 已合併；
本次新的 SDK upgrade PR 保持 Draft、不 merge。

版本查證日期：2026-10-11；收斂 review：2026-10-12。
正式 baseline：`origin/main` 的 `1e1cb78`（已合併 PR #4）。

**PR #5 僅交付現有 adapter 的 SDK 相容性準備，正式 SDK 尚未升級。** 正式 [進入點](../src/main.py)、
[相依定義](../src/pyproject.toml)、[lock](../src/uv.lock)、預設 host 與部署設定維持 baseline；
[HandoffResponsesHost](../src/workflows/responses.py) 僅加入下述最小 API 相容修改，
[完成版程式](../solutions/lab2/) 已同步。
沒有移除任何自訂 plumbing，也沒有新增另一套 checkpoint、binding、projection 或鎖。
Manual permissions 不變；沒有 provision、deploy、變更 credentials、刪除 Azure 資源或 merge。

## Review 與合併驗證邊界

已直接 review 相對 `main` 的 runtime／測試差異，未發現需要改變 runtime 契約的
高信心問題。補強測試 cleanup（setup 失敗仍回收 state）、拒絕時所有既有 clients
的 call count，以及所有模型完整 history 不含其他使用者材料；沒有刪除或放寬原斷言。
正式與候選的 adapter regression 重驗皆為 **43 passed、103 subtests passed**。

相容性準備的兩套 adapter regression 與 GitHub-hosted 正式容器 build／入口 smoke
已通過；**尚待 maintainer 人工 review，保持 Draft、不 merge**。
容器驗證的精確 source SHA／image ID／run URL 見下節；不把它寫成 Azure 授權／模型、
Playground／Teams 或 native migration 驗收。
下方隔離的 native gate 是未來 migration 的證據，**不是正式 adapter regression failures，
也不是要求本 PR 強行解決的合併條件**。沒有重做既有版本調查或新增 hosting 機制。

### Docker 阻擋與使用者設定

先前 daemon socket 不存在的阻擋已由使用者啟動 Docker Desktop／Ubuntu WSL Integration 解決。
2026-10-12 本次實際執行：

```bash
docker info --format '{{.ServerVersion}} {{.OSType}}/{{.Architecture}}'
```

成功回傳 **`29.8.2 linux/aarch64`**。Worktree 乾淨，待建置 source commit：
**`dde72ec64c5234f79a946277d64c4cd13b89f62e`**。
使用該 commit 的 tracked-only archive，排除本機真實 `.env`／credentials／checkpoints；
唯一 tracked env 檔是非秘密 `.env.template`。正式 [Dockerfile](../deploy/Dockerfile)、
[manifest](../src/pyproject.toml) 與 [lock](../src/uv.lock) 未改。

```bash
set -o pipefail
git archive --format=tar dde72ec64c5234f79a946277d64c4cd13b89f62e |
  docker build --platform linux/amd64 --progress plain \
    -f deploy/Dockerfile \
    --label org.opencontainers.image.revision=dde72ec64c5234f79a946277d64c4cd13b89f62e \
    --label workshop.pr=5 \
    -t ipm-handoff-compat:pr5-dde72ec-d9efd9f3 -
```

同一 command 執行兩次，**兩次 exit code 1，image build 未完成**：

| 嘗試 | 失敗階段 | 錯誤摘要 |
| ---- | -------- | -------- |
| 第一次 | Dockerfile 的 `RUN uv sync --locked --no-dev`，下載 `websockets==16.1.1` wheel | 3 次 download retries 後，`client error (Connect)`／`received fatal alert: HandshakeFailure` |
| 相同參數重試一次 | 同一 `RUN`，下載 `httpx==0.28.1` wheel | 相同 TLS `HandshakeFailure` |

Build 已解析正式 lock 的 **117 packages**，使用 uv **0.9.25**、
正式 base tag 所提供的 Linux CPython **3.13.16**；target 明確為 **linux/amd64**。
這些值來自 build output，不代表修改正式 Python／SDK pins。
`docker image inspect ipm-handoff-compat:pr5-dde72ec-d9efd9f3` 回報 **No such image**；
因此沒有完成的 image ID／digest，也沒有可驗收的 application container。

只做有限的標準 TLS 診斷：同一公開 `httpx` wheel URL 在 WSL 的 `urllib` 回傳 **HTTP 200**
（73,517 bytes）；本次短命 `python:3.13-slim-bookworm` probe 在 Docker default network
及單一 command 的 `--network host` 均得到 `SSLV3_ALERT_HANDSHAKE_FAILURE`。
**這僅為環境差異證據，根本原因未確認；沒有證據將其歸為程式 regression。**
沒有降低 TLS 驗證、使用 insecure host／mirror、匯入憑證或修改全域 Docker／proxy 設定。

兩個 probe 都使用 `--rm`，名稱僅為 `ipm-pr5-netcheck-d9efd9f3`、
`ipm-pr5-netcheck-host-d9efd9f3`。清理核對：

```bash
docker ps -a --filter 'name=^/ipm-pr5-netcheck(-host)?-d9efd9f3$' \
  --format '{{.ID}} {{.Names}} {{.Status}}'
```

結果為空。本次沒有建立 application smoke container，不清理其他容器、不執行 prune。
**`/readiness`、空白 `/responses` 的 failed／invalid_prompt、application 啟動／請求 logs
及 application 停止 smoke 全部未執行**，不能寫成通過。
使用者已要求停止重試，正式 Dockerfile／pins／lock 不改。
本次只更新文件，未重跑既有 SDK／native migration 調查或已完成的 regression。

### GitHub-hosted runner 實際驗證（已授權）

使用者已明確同意新增 workflow 與 commit／push 觸發。
查無可重用的 repository 容器 workflow／smoke script；已有的 Copilot、Dependabot、
Dependency Graph 是平台工作，不重複建置其中的任務。
新增單一 [Handoff container smoke workflow](../.github/workflows/handoff-container-smoke.yml)：

- `ubuntu-24.04` x64、只有 `contents: read`，`pull_request`＋相關 paths，
  Draft 可執行，沒有 `pull_request_target`／merge／Azure／registry login／image push。
- Checkout 明確 PR head SHA、`persist-credentials: false`；
  官方 release tag 已查證並完整釘 SHA：
  `actions/checkout` v7.0.1 → `3d3c42e5aac5ba805825da76410c181273ba90b1`，
  `actions/upload-artifact` v7.0.2 → `cf430e030ddbb5b0abf93d22962f4752f3646cd9`。
- Job timeout 25 分鐘，build／smoke／cleanup 有各自 timeout。
  成功／失敗均收集診斷、上傳 artifact；容器先保留 logs，
  再核對精確 ID／名稱，只 stop／rm 本次容器，沒有 prune。
- PR path filters 是累積 diff，因此另比對 synchronize 的前一 head；
  僅回填文件時明確 skip build／smoke，不重複建置、不宣稱該新 SHA 已重新驗證。

2026-10-12 **[run 38197154016](https://github.com/ownway22/workshop-ipm-multiagent-system-student/actions/runs/38197154016)
成功**；build、smoke、diagnostics、cleanup、artifact steps 全部成功。

| 實際證據 | 結果 |
| -------- | ---- |
| Checkout／image revision／被測 head SHA | `f63e33eaebe1feb8c1ddb1b307ba37e67833fb55` |
| Image ID | `sha256:015d062134f7140265c1c1cee64cc6fd1a542003486ba0664a82b570739eeaa3` |
| Image／原始 CMD | `linux/amd64`；`uv run --no-sync python main.py --responses --host 0.0.0.0` |
| 正式 lock 安裝 | `uv sync --locked --no-dev`；解析 117、安裝 110 packages，Python 3.13.16／uv 0.9.25 |
| 未升級的 SDK（build log 核對） | core 1.12.1、foundry 1.10.3、foundry-hosting b260722、agentserver core 2.0.0b8／responses 1.0.0b9 |
| Container ports／mounts | `127.0.0.1:32768 → 8088/tcp`；mounts 為空 |
| `GET /readiness` | HTTP 200，`{"status":"healthy"}` |
| `POST /responses`，`{"input":" ","stream":false}` | HTTP 200；`status=failed`、`error.code=invalid_prompt`，訊息「請輸入非空白的訊息。」；不是 `server_error` |
| Startup／request logs | 合成 offline 設定、正式 protocol、8088 listening；`拒絕 Handoff 請求` 明確出現，未送有效模型 prompt |
| Cleanup | 僅本次 container ID 的 stop／rm 成功，精確 ID 再查為空 |
| Diagnostics artifact | `handoff-container-smoke-38197154016-1`，ID `11723603803`；[下載頁](https://github.com/ownway22/workshop-ipm-multiagent-system-student/actions/runs/38197154016/artifacts/11723603803) |

Run 使用 tracked-only archive 與正式 Dockerfile／manifest／lock；合成非秘密 env、
無 `.env`／credentials mounts。Readiness 和 response JSON 均有實際斷言，
不是只看 HTTP 200；空白輸入在進入 workflow／model 前被 adapter 拒絕。
Artifact 已下載核對 image metadata、JSON、啟動／請求及 cleanup logs。

**本機 Docker 的 TLS build failure 仍是獨立歷史紀錄，根因未確認；GitHub runner 的成功
不能解釋或修好該本機環境。** 本節只證明明確 head 的正式 container smoke；
正式 SDK／runtime 不變，沒有 Azure／Playground／Teams 驗收或 native migration 完成宣稱。

## 本階段：現有 adapter 的 SDK 相容性

在 PR #5 接續修正候選相依下的 failures，沒有重跑下方已證實的 release／native capability 調查：

- `CreateResponse`、input items：舊 Azure model 與新版 dict 都使用 public `Mapping`／`get`。
  保留只接受 user text 的限制，完整保存文字、JSON、code fences 與多段換行；
  不接受 assistant／system、tool results、附件或錯誤型別。
- Event：測試以舊版 public `as_dict()` 或新版 `dict()` 取得相同 wire payload，
  **原 event 順序、內容、錯誤、routing、history 與 checkpoint 斷言全部保留**。
- 候選版移除 `atext_content()`：改用兩版共通的 public text content builder，
  由 SDK 累積 chunks 與產生 done events；沒有版本猜測、私有 API 或額外 projection 系統。
- 測試使用各案例自己的 `AGENTSERVER_STATE_ROOT`，避免新版 file-backed outer history
  殘留影響重跑；offline 測試明確傳 `configure_observability=None`，
  不初始化 SDK exporter／IMDS 探測。正式 observability 與 metadata-only diagnostics 未改。

| 本階段驗證 | 結果 |
| ---------- | ---- |
| `cd src && uv lock --check && uv run --no-sync pytest -q --tb=short` | **43 passed、103 subtests passed** |
| `cd tests/native_hosting && uv lock --check && uv run --no-sync pytest -q -c ../../src/pyproject.toml ../unit/test_handoff_responses.py --tb=short` | **43 passed、103 subtests passed** |
| Pylance syntax／`git diff --check` | 通過 |
| `src`／`solutions/lab2` 的 Python 副本 | 16 份逐檔完全一致 |
| 正式與候選 pins／lock、預設 host、credential、拓撲／prompts、部署配置、native gate tests | 全部未變 |

新增真正 Handoff 的 **12 個 ASGI 情境**：stream／nonstream × 同 host／重建 host ×
conversation／previous_response_id chain／同 response 分岔；每個情境都執行三輪普通文字，
核對完整專家 schema JSON、中文與 code fences 只出現一次，SSE delta／done／final text 相同，
原材料沒有重複 replay、pending Handoff 與 history 正確，原 response 的 checkpoint bytes 不變。
另保留全部原 regression，補上 model／dict input guards 及實際 HTTP／SSE `invalid_prompt` 契約。

### 使用者隔離的既有邊界

Maintainer 確認：**本次不要求不同使用者共用同一 conversation ID**。
重用另一使用者已有 outer history 的 ID，但沒有自己的 checkpoint 時，
baseline 與候選均維持明確 `invalid_prompt` 拒絕，不默默建立新對話。
這是兩套環境一致的既有契約，**不是 SDK upgrade regression**。

真正 Handoff 的 ASGI 測試核對拒絕時沒有 model call、不回傳前位使用者文字、
pending request IDs／payload 與所有既有 checkpoint bytes 不變；
使用者各自的合法 conversation 可多輪續接，拒絕後原使用者仍能續接。
**此處只驗證本機 handler／storage 的行為，不代表 Foundry 平台授權隔離已驗收。**

本階段沒有接入 native host，也沒有移除 #4 的 checkpoint／鎖／續接機制。
下方 native 純文字、線性 lineage、`store=False`、ownership 與 error contract blockers
仍維持；原 native gate 不使用 skip／xfail，既有調查與四個 gate 本階段不重跑。

## 隔離實驗：官方來源與實際版本

以 [PyPI 的精確版本 metadata](https://pypi.org/pypi/agent-framework-foundry-hosting/1.0.0b261008/json)
及 released wheel 為準，不以 GitHub `main` 推測 API。Hosting wheel 的 SHA256：

```text
31933b04f7ab6191e9eecf0244eeed743110bd9e67c76810e5378f421801c0b7
```

對應官方 [python-1.21.0 release](https://github.com/microsoft/agent-framework/releases/tag/python-1.21.0)，
固定 source commit 為 `91ab44faa4824a30247d00c341f3498ba46b1ca3`。
已核對該 commit 的 native Responses adapter 與 wheel source 內容相同。

- [release README：public API、線性 lineage 與 ownership](https://github.com/microsoft/agent-framework/blob/91ab44faa4824a30247d00c341f3498ba46b1ca3/python/packages/foundry_hosting/README.md#native-responses-workflows)
- [public request API：WorkflowTurn／get_workflow_responses](https://github.com/microsoft/agent-framework/blob/91ab44faa4824a30247d00c341f3498ba46b1ca3/python/packages/foundry_hosting/agent_framework_foundry_hosting/_request.py)
- [released native Responses 實作：reply decoder 與 request_info projection](https://github.com/microsoft/agent-framework/blob/91ab44faa4824a30247d00c341f3498ba46b1ca3/python/packages/foundry_hosting/agent_framework_foundry_hosting/_workflow_responses.py)
- [released continuation 驗證與 consume-once lineage](https://github.com/microsoft/agent-framework/blob/91ab44faa4824a30247d00c341f3498ba46b1ca3/python/packages/foundry_hosting/agent_framework_foundry_hosting/_workflow_state.py)
- [official typed workflow sample](https://github.com/microsoft/agent-framework/blob/91ab44faa4824a30247d00c341f3498ba46b1ca3/python/samples/04-hosting/foundry-hosted-agents/responses/workflows/main.py)
- [official approval sample](https://github.com/microsoft/agent-framework/blob/91ab44faa4824a30247d00c341f3498ba46b1ca3/python/samples/04-hosting/foundry-hosted-agents/responses/workflows/approval.py)
- [Microsoft Learn：native workflow hosting 與 storage](https://learn.microsoft.com/en-us/agent-framework/hosting/foundry-hosted-agent)

上述 sample 是 typed input／correlated approval 示範，**不是** Handoff 純文字續接的證據。
讀取官方 source 是為了查證；重現程式不 import 私有模組、不 monkey patch，也不複製 SDK 實作。

候選環境在 [tests/native_hosting/](../tests/native_hosting/)，自己的
[pyproject.toml](../tests/native_hosting/pyproject.toml) 與 [uv.lock](../tests/native_hosting/uv.lock)
不會取代正式環境。所有直接使用的相依都精確釘版。
相較 baseline，完整版本差異如下；其餘套件（含 Python、uv、模型設定）不變：

| 套件 | baseline → 候選 | 原因 |
| ---- | --------------- | ---- |
| agent-framework-foundry-hosting | 1.0.0b260722 → 1.0.0b261008 | 查證指定的 native workflow hosting |
| agent-framework-core | 1.12.1 → 1.21.0 | Hosting 最低 1.20.0；同 release 的 Foundry 1.14.1 最低 **1.21.0** |
| agent-framework-foundry | 1.10.3 → 1.14.1 | 固定到已查證的 10/8 release cohort，核對 client／ownership API |
| agent-framework-orchestrations | 1.0.1 → 1.3.1 | 同 release 的真正 HandoffBuilder；仍重用正式拓撲與 routing middleware |
| agent-framework-devui | 1.0.0b260721 → 1.0.0b261008 | 同 release，確認 Lab 2 的 imports、serve signature 及相依相容性 |
| azure-ai-agentserver-core | 2.0.0b8 → 2.1.0 | Hosting 最低需求；public Foundry State Store／conditional writes |
| azure-ai-agentserver-responses | 1.0.0b9 → 2.2.0b1 | Hosting 最低需求；要求 core >=2.1.0,<2.2.0 |
| agent-framework-openai（間接） | 1.11.0 → 1.15.0 | Foundry 最低需求；保留原 OpenAI SDK 2.48.0 |
| azure-ai-agentserver-invocations（間接） | 1.0.0b7 → 1.1.0 | Hosting 最低需求；不啟用 Invocations 端點 |
| msgspec（新增間接） | 無 → 0.22.0 | 新 core 的必要相依 |
| regex（新增間接） | 無 → 2026.9.29 | 新 core 的必要相依 |

仍固定 azure-ai-projects **2.3.0**、azure-identity **1.25.3**、pydantic **2.13.4**、
python-dotenv **1.2.2**、pytest **9.1.1**、httpx **0.28.1**。
這是實際測試的選定組合，不宣稱所有其他排列均不可用。
Lock 限制發行截止日，並保留所有不需改變的 baseline transitive pins；不是全量 upgrade。

## 最小重現與主要 blocker

以下是 **native host 的隔離實驗**，不是本 PR 正式 adapter 的 regression runner。

WSL 內使用 Dockerfile 同版 **uv 0.9.25**、Linux **Python 3.13.11**：

```bash
cd tests/native_hosting
uv lock
uv sync --locked --no-dev
uv sync --locked --group dev
uv run --no-sync pytest -q -m gate --tb=short
```

[重現程式](../tests/native_hosting/test_native_handoff.py) 呼叫真正的 `HandoffBuilder`
（透過正式 `build_workflow`／`build_participants`），模型才替換成 deterministic fake。
四位 agent 的名稱、instructions、SpecialistReview schema、Primary ↔ specialists
拓撲、回交 guard 與降級 middleware 都沿用正式程式。Factory 為每個 request 建立 fresh graph、
agent、client；host 自己管理 checkpoint、scope、event lifecycle 與資源生命週期。
測試 state 僅寫入目前 worktree 的 `.cache/`，每個案例結束即回收。

第一輪已實測：

1. 真正完成 `Primary → Coding → Primary`。
2. 專家 schema JSON、中文、code fence 與結尾標記完整出現且只出現一次。
3. Public checkpoint store 中有唯一、真正的 `HandoffAgentUserRequest`，等待來源是 Primary。
4. Native host 對外另輸出 `request_info` function call；這不是 Playground 能直接回答的文字等待。

第二輪保留相同 `agent_session_id`，提交 `previous_response_id` 與普通 `input: "繼續"`，
**沒有**提交 `function_call_output`。四個 gate 案例（兩個 parser × stream／nonstream）
都失敗於第二輪，未能到達第三輪成功續接：

| parser | 實際 HTTP／SSE 結果 |
| ------ | ------------------- |
| `WorkflowTurn(input=await response_input_messages(request))` | HTTP 200；JSON `status=failed` 或 SSE `response.failed`；`server_error`；`Native workflow input validation failed. Validate the typed input and current scoped continuation, or start a fresh workflow lineage.` |
| 第二輪呼叫 `get_workflow_responses()` 再回傳 `WorkflowTurn(responses=...)` | HTTP 200；JSON／SSE failed；`server_error`；`Pending workflow replies cannot be mixed with new input.` |

第一種不是回覆 pending request，而是新的 start input；released validator 明確拒絕
`This workflow is awaiting replies; do not replace them with new input.`，wire 訊息則如上表。
第二種 loader 只解碼具有 correlated wire ID 的 function／computer results 或 approval replies，
普通 user message 會直接被拒絕。它不是「把目前文字交給等待中的 Handoff」的 helper。

`HostedResponseRequest` 的 public surface 有 input readers 與上述 decoder，
**沒有**把 host 已選定的 scoped pending requests 提供給純文字 reply mapper 的介面。
`WorkflowTurn.responses` 必須使用精確的內部 pending request IDs；不能猜 wire call ID、
自行選 latest checkpoint，或另維護 response-to-checkpoint 表來繞過 host。
目前也沒有已查證的 public hook 可把 Handoff 的 `request_info` projection 保持為純文字 UX。

診斷測試再次提交「再繼續」，確認仍是明確失敗、pending request 未被消耗、
模型沒有被再次呼叫，也沒有意外重新開始 workflow。

**對照組不是解法：** 只有回傳實際 `function_call_output`（以 native 回傳的 call ID 配對）時，
三輪續接、同 host／重建 host、conversation／previous_response_id、stream／nonstream 才通過，
並核對原材料沒有重複 replay、第二輪文字保留在第三輪 history。
這證明 checkpoint 與 fake 可正常續接，不能拿來宣稱 Playground 純文字門檻通過。
本次沒有改學員 UX，也沒有把 gate 標為 xfail／skip 或放寬 completed／文字斷言。

## 分岔：官方設計限制，不直接判定為 SDK bug

原 [分岔 regression](../tests/unit/test_handoff_responses.py) 的完整斷言保留，在 baseline 與候選均通過：
同一 `previous_response_id` 可分岔，各自續接，原 checkpoint 維持唯讀。

Native 對照組第一個 correlated reply 通過；第二次由同一 response 分岔被拒絕：

```text
HTTP 200 / response.failed / server_error
This workflow continuation is stale or forked; use its current response.
```

[microsoft/agent-framework#8947](https://github.com/microsoft/agent-framework/pull/8947)
明列「one CAS-protected lineage or conversation head」及 consume-once authority，
刻意拒絕 stale、forked、replayed replies；release README 也寫明這項 contract。
因此分岔拒絕是**已查證的官方設計**，不是本次認定的新 SDK bug，亦不繞過。

Maintainer 已選擇：**保留 #4 的分岔契約，本次不批准改為線性 continuation。**
即使 upstream 補好純文字 hook，這項差異仍是正式遷移的獨立 blocker。

線性設計的優點是 CAS fencing、consume-once reply 與較清楚的 side-effect／replay 邊界；
代價是不能比較同一 response 的替代追問，舊 response 重送不等於另一個合法分岔，
併發追問也可能被拒絕而非全部排隊。它不保證外部副作用 exactly-once。
未來若重新考慮，需另外決定 UI／client 重試、錯誤提示、相容策略與 regression，
不能只刪掉原分岔測試。

## 其他實測與邊界

| 項目 | baseline／候選差異與證據 |
| ---- | ----------------------- |
| conversation → previous_response_id | baseline 原測試允許；native 實測拒絕 named conversation 的切換，未執行模型；source 明確禁止此種 fork |
| checkpoint binding | native 以 exact response/checkpoint、graph、user+sandbox scope 與 lineage 配對；非挑最新檔。刪除精確 checkpoint 後，即使較舊 checkpoint 仍存在也明確失敗；更換 bound checkpoint 的 workflow name 同樣失敗，不重開 |
| reply 不匹配 | forged call ID 明確 failed：`A reply must match its wire ID in the exact pending workflow checkpoint.`；無額外 model call |
| `store=False` | native 真正 Handoff 第一輪執行至等待使用者時 failed，不取得 checkpoint store；不能回傳可續接的 pause。原 adapter 沒有以此旗標關閉自己的 checkpoint；不代表其 outer response 仍可查回 |
| history／private service state | native public contract 使用 checkpoint history，對 storing clients 強制 `store=False`。Fake 中 Primary 與 Coding 均驗證此值；與 baseline 保留 Primary 的服務端 storage 選擇不同。真實 Foundry history／Token 成本尚未驗收 |
| resource ownership | 成功與拒絕的 requests 均實測 fresh clients 各 enter／exit 一次、穩定 graph／executor identity；原共用單一 FoundryChatClient factory 不可直接搬用。真正 Azure project client／credential／transport cleanup 尚未接線或驗收 |
| HTTP／SSE | 本重現的 application validation／continuation errors：nonstream HTTP 200 + failed JSON；stream HTTP 200 + created → in_progress → failed SSE，code 均為 `server_error`。原 adapter 的使用者錯誤是 `invalid_prompt`；不能假設新版自動 HTTP 400 |
| 舊 adapter 的 SDK 相容性 | 第一階段未修改 adapter 時為 `74 failed, 13 passed, 11 subtests passed`。本階段已修 public mapping／event／text builder 相容性，兩套環境皆 **43 passed、103 subtests passed**；不是 native migration 通過 |

Checkpoint allowlist 只加入 Handoff request、`GenericAlias`（`list[Message]`）與 Azure role enum；
沒有停用反序列化限制。測試只透過 public store／checkpoint methods 製造刪除或不匹配，
不 import 私有 encoding、修改內部 host 欄位或實作第二套 hosting。

尚未證實：malformed checkpoint bytes／任意錯誤型別、跨使用者／sandbox 的完整黑箱矩陣、
同對話併發與獨立對話效能、取消／process hard-kill 的狀態與真實 transport cleanup、
client 重送同一 trusted call ID 的 contract、telemetry、實際模型的 Token usage。
本次沒有把來源中的保證寫成上述案例已通過。

## 第一階段歷史驗證紀錄

以下保留最初 blocker 調查的結果，不是本階段重新執行；相容性 failures 的修正結果見上節。

| 環境與 command | 結果 |
| -------------- | ---- |
| baseline：`cd src && uv run --no-sync pytest -q` | 初始 **38 passed、60 subtests passed**；再次驗證相同 |
| baseline：`uv lock`、`uv sync --locked --no-dev`、`uv sync --locked --group dev` | 全數成功；正式 manifest／lock 不變 |
| 候選：`uv lock`／`uv lock --check`、兩種 locked sync | 全數成功；選定組合 imports／public signatures 查證成功 |
| 候選：`uv run --no-sync pytest -q -m gate --tb=short` | **4 failed**；真實純文字門檻不通過，非 baseline failure |
| 候選：`uv run --no-sync pytest -q -m 'not gate' --tb=short` | **24 passed**；診斷／function result 對照組，不是遷移驗收 |
| 候選：`uv run --no-sync pytest -q --tb=short` | 最終 **4 failed、24 passed**；只有四個純文字 gate 失敗 |
| 候選：`uv run --no-sync pytest -q -c ../../src/pyproject.toml ../../tests/unit/test_handoff_responses.py --tb=short` | candidate-only compatibility failures，如上；原測試與正式相依未變 |
| baseline：`uv run --no-sync python main.py --help` | CLI 入口成功；不等於容器／Azure smoke test |
| Pylance syntax check | 重現程式無 syntax errors |
| lint／type checks | Repository 沒有既有 runner/config；未另裝工具，不宣稱 type-check 完成 |
| Docker build／容器入口 smoke | 未執行：Docker daemon socket 不存在；未啟動 daemon、未改用 Azure remote build |
| Azure／Playground／Teams | 未執行；沒有 end-to-end 完成的宣稱 |

第一次用 baseline runner 指定 `src/` 專案外的測試路徑時未讀到專案 pytest config，
造成 `ModuleNotFoundError: agents`；改用既有標準 runner 即全過，沒有修改程式修復此 runner 問題。
實驗測試的共享暫存路徑也曾造成平行執行的 setup error；已改為每案例唯一暫存目錄並重驗，
不將這兩種 runner 問題誤列為 SDK blocker。

## 最小可行替代方案與後續門檻

### 後續正式 SDK upgrade

必須另行明確升級正式 pins／lock，逐項查證必要相依與 protocol／API 相容性，
使用實際升級的 image 完成 build／入口 smoke，並以新 session／conversation
人工驗收 Lab 2 → Lab 3 → Lab 4、12 題、多輪、完整輸出與 Token／history。
本 PR 的 candidate 假模型測試不能取代那些驗收；不得假定 checkpoint 跨版本相容。

### 後續 native migration（獨立工作）

目前不改 UX、不破壞分岔契約的路徑是**維持 #4 的 adapter 行為與正式 lock**，
並使用上節的最小 SDK 相容修改；不能把它描述為 native hosting 已接線。
必要的薄型 input/reply adapter 可以接受，但必須依賴 upstream 的 public extension point：

1. 讓 `parse_response` 取得 host 已驗證的 scoped pending Handoff request／response type，
   或提供接收 plain user text 的 public reply mapper；由 host 完成完整 batch、reply authority 驗證。
2. 提供可測試的 Handoff user-input projection policy，保持 Playground 純文字 UX，
   不要求 caller 送 tool results；host 仍管理 event lifecycle。
3. 另行處理保留分岔 contract 的官方支援路徑，或由 maintainer 明確批准相容策略。
4. 補齊上述未測矩陣並保留全部原 regression，才可重新考慮正式接線。

不採用的替代方式：猜 pending ID、私有欄位／monkey patch、複製 SDK hosting、
自行維護另一套 binding／鎖、每輪重啟、移除等待步驟、function results 學員 UX。

### 舊 checkpoint、新 session 與 rollback

此次正式 pins、預設 host 與 runtime 契約不變，**沒有新增**既有學員必須換 session 的要求。
實驗只讀自己的隔離 state，不匯入 `.handoff-checkpoints/`。

未來 native 上線不能假設舊 checkpoint 相容；官方不讀 legacy unscoped state。
必須使用新 session／conversation／Responses chain，沒有未驗證的自動遷移。
Rollback 應回到 baseline 程式、原精確 lock 與經人工選定的舊 image，
再以新 session／conversation 驗證；不把 native state 當成舊 host state，
不自動刪除舊 state、session 或 Azure 資源。本次沒有執行任何 rollback／部署操作。

### 未來人工 Azure 驗收清單

以下仍是**未執行**的 release gate；只能在維持 Manual permissions 且另行批准部署後進行：

- [ ] Lab 2 → Lab 3 → Lab 4 逐站比對，DevUI／Playground／Teams 的純文字追問 UX 不變。
- [ ] 完整 12 題，保留 T12 的歷史 FAIL／已知限制，不用 protocol completed 取代業務判準。
- [ ] 每題至少三輪普通 user text；核對路由、waiting source、完整專家 JSON／code fences，
      不遺失、不重複、不意外重開。
- [ ] session 閒置回收後續接；新 session 與跨使用者／conversation 隔離。
- [ ] 舊 response、分岔、replay、並行追問、取消、checkpoint 缺失／損毀／錯型別的明確契約。
- [ ] Token usage、history replay／cache 與 Primary storage 改變是否異常。
- [ ] logs 只有 metadata；不含對話本文、tokens、credential 或使用者識別資料。
- [ ] 真實 clients／agents／project transports 正常及取消後均回收；不關閉 shared resource。
- [ ] A365／exporter 問題另行判讀；沒有直接因果證據，不拿 telemetry 錯誤解釋路由失敗。
