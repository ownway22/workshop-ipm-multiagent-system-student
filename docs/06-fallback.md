# 如果你卡住了

**適用情境**：環境問題無法在現場修復，或某一關卡進度落後時。
**完成條件**：選擇合適的處理方式，並取得會後可重跑的材料與步驟。

---

## 步驟一：判斷問題類型

| 你的情況                                   | 走哪條路                                             |
| ------------------------------------------ | ---------------------------------------------------- |
| 環境沒問題，只是某一關做不出來             | 用 [完成版程式碼](../solutions/README.md) 覆蓋後繼續 |
| 環境本身不滿足前置條件（權限、租戶、配額） | 走**觀摩路徑**，見下方                               |

第一種可在現場處理，不必繼續閱讀。若無法判斷，回 [Lab 0 的環境檢查](00-lab0-env-check.md) 執行 preflight；六項都通過就是第一種。

## 步驟二：改走觀摩路徑

有些環境問題現場修不了：

- 訂閱權限需要別人核准
- Teams 帳號與 Azure 訂閱不在同一個 Entra 租戶
- 配額申請還在排隊
- 公司網路政策擋住必要的端點

這些不是操作問題，現場也無法繞過。

### 現場怎麼進行

講師會示範完整流程，你**看著做筆記**，不需要自己跑。

⚠️ **講師的驗證環境不會開放共用。** 那個環境裡有其他人的資源與設定，
共用會造成互相干擾，而且你在別人的環境裡做的事帶不回去。

觀摩仍可看到完整流程，也會拿到可在自己環境重跑的材料。

## 可用材料

### 1. 各 Lab 的完成版程式碼

[solutions/](../solutions/README.md)：

| 內容                                          | 用途                                               |
| --------------------------------------------- | -------------------------------------------------- |
| [lab1_prompt.md](../solutions/lab1_prompt.md) | Lab 1 的 agent 名稱、instructions、response format |
| [lab2/](../solutions/lab2/)                   | 四個 agent 的定義、Handoff 拓撲、建立腳本、進入點  |
| [lab3/](../solutions/lab3/)                   | Dockerfile、azd 設定、agent manifest、基礎設施     |

整個 repository 帶走即可，不需要另外打包。

### 2. 講師示範的關鍵步驟截圖

若講師已提供截圖，會存放在 `docs/assets/`，涵蓋 Lab 3 與 Lab 4 未親自操作的流程。

| 檔名                           | 對應步驟                                                             | 你該從中確認什麼                                    |
| ------------------------------ | -------------------------------------------------------------------- | --------------------------------------------------- |
| `lab3-01-azd-deploy.png`       | [Lab 3 步驟四](03-lab3-deploy.md#步驟四部署映像)                     | `azd deploy` 開始建置時的輸出長什麼樣               |
| `lab3-02-agent-active.png`     | [Lab 3 步驟五](03-lab3-deploy.md#步驟五輪詢到-active)                | `azd ai agent show` 顯示 `active` 的樣子            |
| `lab3-03-playground.png`       | [Lab 3 步驟六](03-lab3-deploy.md#步驟六在-playground-送-12-題)       | Playground 是與**整個系統**對話，不是選擇單一 agent |
| `lab3-04-traces.png`           | [Lab 3 步驟八](03-lab3-deploy.md#步驟八看-traces)                    | traces 長什麼樣，以及搜不到對話原文                 |
| `lab4-01-publish-metadata.png` | [Lab 4 步驟三](04-lab4-publish-teams.md#步驟三填必填中繼資料)        | 必填欄位有哪些、Developer name 的長度限制           |
| `lab4-02-direct-publish.png`   | [Lab 4 步驟四、五](04-lab4-publish-teams.md#步驟四選-direct-publish) | Direct publish 與「Just you」在畫面上的位置         |
| `lab4-03-teams-chat.png`       | [Lab 4 步驟七](04-lab4-publish-teams.md#步驟七完成一輪交接對話)      | Teams 裡完成一輪交接對話的實際樣子                  |

> ℹ️ 截圖由講師在現場示範時擷取。若沒有 `docs/assets/` 目錄或目錄為空，表示講師尚未提供截圖；其餘材料不受影響。

### 3. 會後自行重跑的步驟

就是下面這一段。

---

## 會後重跑

### 步驟一：先修好環境問題

回 [Lab 0](00-lab0-env-check.md) 逐項確認，特別是這幾個現場常卡的：

| 項目                                                | 怎麼確認                                  |
| --------------------------------------------------- | ----------------------------------------- |
| 專案範圍的 `Foundry Project Manager`                | 訂閱 Owner **不包含**這個角色，要另外指派 |
| 資源群組範圍的 `Azure Bot Service Contributor Role` | Lab 4 發布到 Teams 需要                   |
| Teams 帳號與訂閱同一個 Entra 租戶                   | 不同租戶時「Just you」發布不會出現        |
| 區域在 hosted agents 支援清單內                     | East Asia **不在**清單內                  |
| 模型部署配額                                        | 太低會在多代理對話中頻繁觸發 429          |

不確定是否修好，跑一次就知道：

```bash
cd src
uv run python -m preflight
```

**六項全過再往下。** 沒過就先解決，往下做只會卡在更難懂的地方。

### 步驟二：Lab 1

打開 [solutions/lab1_prompt.md](../solutions/lab1_prompt.md)，
依 [Lab 1 講義](01-lab1-single-agent.md)在 Foundry portal 建立 `qvn-coding-agent`。

名稱**必須逐字一致**，Lab 2 的腳本靠它尋找。

### 步驟三：Lab 2

```bash
cp -r solutions/lab2/* src/
cd src
uv run python -m registry.create_agents
uv run python main.py
```

開 `http://localhost:8080`，依 [Lab 2 講義](02-lab2-multi-agent.md)送 12 題。

### 步驟四：Lab 3

```bash
cp -r solutions/lab3/deploy/* deploy/
cd deploy
azd env new ipm-workshop
azd env set AZURE_RESOURCE_GROUP <你的資源群組>
azd env set AZURE_AI_ACCOUNT_NAME <你的 Foundry 帳戶>
azd env set AZURE_AI_PROJECT_NAME <你的 Foundry 專案>
azd env set MODEL_DEPLOYMENT_NAME <你的模型部署名稱>
azd provision
azd deploy
azd ai agent show
```

等 `status` 變成 `active`，其餘依 [Lab 3 講義](03-lab3-deploy.md)。

### 步驟五：Lab 4

依 [Lab 4 講義](04-lab4-publish-teams.md)在 Foundry portal 發布。
重點兩件事：Developer name **≤ 32 字元**、Publish options 選 **Direct publish**、
範圍選 **「Just you」**。

### 步驟六：清理

驗證完就把課程建立的資源刪掉，避免持續計費：

```bash
cd src
uv run python -m registry.cleanup
```

---

## 問題排除：先看 traces

會後自己重跑時沒有講師在旁邊，所以先學會這個判斷：

### 判斷原則

對話沒反應時，第一件事不是改 instructions，而是問「請求到底有沒有抵達 agent？」

| 你看到的                        | 判讀               | 該查什麼                                         |
| ------------------------------- | ------------------ | ------------------------------------------------ |
| traces 裡**沒有**對應時間的請求 | 請求沒抵達         | 發布設定、Teams 通道、網路、session 是否已被回收 |
| traces 裡**有**請求但回應不對   | agent 回應不如預期 | instructions、路由拓撲、模型能力                 |

查法見 [Lab 3 步驟八](03-lab3-deploy.md#步驟八看-traces)。

---

## 其他資源

- 各 Lab 講義末尾的「常見問題排除」表
- [疑難排解指南](../references/troubleshooting_guide.md)
