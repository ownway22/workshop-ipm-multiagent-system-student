# Lab 2：多代理與 Handoff

**前置**：[Lab 1](01-lab1-single-agent.md) 完成，`qvn-coding-agent` 已存在

---

## 目標

用程式碼建立另外三個 agent，把四個接成單層星狀的 Handoff 拓撲，
在 DevUI 觀察控制權如何在協調者與專家之間移交。

做完你會得到：

- Foundry 專案中共 4 個 `qvn-` agent（1 個 portal 建的 + 3 個程式碼建的）
- 一個可對話的多代理系統，跑在你自己的機器上
- 對「複合需求為什麼是逐次交接、而不是同時分派」的實際體感

---

## 步驟一：建立另外三個 agent

```bash
cd src
uv run python -m registry.create_agents
```

預期輸出：

```text
[qvn-coding-agent]    ✅ 已存在（Lab 1 建立）— 其專家角色已依定義檔加入 Handoff
[qvn-primary-agent]   ✅ 已建立（version 1）
[qvn-architect-agent] ✅ 已建立（version 1）
[qvn-spec-agent]      ✅ 已建立（version 1）

結果：Foundry 專案中共有 4 個 qvn- agent。
下一步：啟動 DevUI 觀察交接
    cd src && uv run python main.py
```

**第一行是刻意的**：這支腳本**不會**幫你補建 `qvn-coding-agent`。
它只確認你在 Lab 1 建的那一個還在，然後把它的專家角色接進 Handoff。
沿用而不是重建，才看得出「portal 手動建立」與「程式碼建立」在這裡是**同一種東西**。

回 portal 的 Agents 清單看一眼，四個都在。

---

## 步驟二：再執行一次（驗證冪等性）

```bash
uv run python -m registry.create_agents
```

```text
[qvn-coding-agent]    ✅ 已存在（Lab 1 建立）— 其專家角色已依定義檔加入 Handoff
[qvn-primary-agent]   ⏭️  已是最新，略過（version 1）
[qvn-architect-agent] ⏭️  已是最新，略過（version 1）
[qvn-spec-agent]      ⏭️  已是最新，略過（version 1）

結果：Foundry 專案中共有 4 個 qvn- agent。
```

**agent 總數沒有變。**

這件事值得停一下：agent 的**名稱**是穩定識別碼，重複執行只會在同一個 agent 底下
建立新版本，不會冒出第二個同名 agent。所以你可以放心改 `src/agents/` 的 instructions
再重跑——這正是等一下要做的。

改一個字試試（例如在 `src/agents/architect.py` 的 `INSTRUCTIONS` 加一句話），
再執行一次，你會看到那一個變成 `✅ 已更新（version 2）`，其餘仍是 `⏭️`。

---

## 步驟三：啟動 DevUI

```bash
uv run python main.py
```

```text
Foundry 專案：https://<你的專案>.services.ai.azure.com/api/projects/<專案名稱>
模型部署　　：<你的部署名稱>
DevUI　　　 ：http://localhost:8080
實體　　　　：qvn-ipm-review（agent 類別）
　　　　　　　qvn-ipm-review-workflow（workflow 類別）
存取驗證　　：已關閉（workshop 權宜做法，不是生產建議）
```

用瀏覽器開 `http://localhost:8080`。

### 同一套系統，兩種呈現

點頂端的實體選擇器，你會看到它分成兩組：

```text
Agents (1)
  qvn-ipm-review
Workflows (1)
  qvn-ipm-review-workflow
```

**這兩個是同一套四代理系統的兩種呈現**，不是兩套不同的系統。
兩者都來自 `src/agents/` 同一份定義檔，拓撲完全相同，差別只在**對外包成什麼**：

| 實體                      | 畫面長什麼樣                      | 對應到               |
| ------------------------- | --------------------------------- | -------------------- |
| `qvn-ipm-review`          | 一個聊天框                        | Lab 3 部署出去的樣子 |
| `qvn-ipm-review-workflow` | 一張拓撲圖加上 Execution Timeline | 工作流本體的樣子     |

選取 `qvn-ipm-review-workflow`，畫面會換成**完全不同的一套**：左側是五個節點的圖
（`user-input` 加上四位 agent），右側是 Execution Timeline，下方是 **Configure & Run**
而不是聊天輸入框。連 session 選單也從「Conversation」變成「Checkpoint Storage」。

`user-input` 是一個只負責「把你打的字交給 Primary」的入口節點，不是 agent。
它存在的理由很實際：沒有它的話，DevUI 會要你填 `role`、`contents`、`author_name`
等六個欄位才能送出一題。

這就是這一段要你看的事：**DevUI 同時支援 agent 與 workflow 兩種類別**，
而且兩種類別的操作介面本來就不一樣。

### 在 workflow 實體送一題

1. 點 **Configure & Run**（會看到 `Input Type: Simple Text`）。
2. 在 **Input** 欄貼上題組的 **T01**（程式碼題）。
3. 點 **Run Workflow**。

你會在 Execution Timeline 看到 Primary 先說「我先交給程式碼健檢專家」，
接著 `qvn-coding-agent` 執行，最後 Primary 彙整出完整回覆。
**路由結果與 agent 實體完全一致**——因為本來就是同一套拓撲。

### 圖上看得到拓撲，但時間軸要看仔細

把圖放大，你會看到 `user-input → Primary`，以及 Primary 與三位專家之間的雙向線，
**專家之間沒有任何直接線**。這與下面「拓撲長什麼樣」一節的指令輸出是一致的。

不過 Execution Timeline 會把四個 agent 都列成 `completed`——那只是每一輪廣播到
每個節點的結果。**真的做了事的那幾個才會帶「Output」**，看這個就不會誤判。

### ⚠️ workflow 實體的兩個限制

這兩項是**已實測確認的套件行為**，不是你操作錯誤：

1. **每次送出都是新的一輪**，不是接續上一輪的交接。它看起來像在跟你對話，
   是因為讀到了上一輪的殘留狀態。
2. **按「新對話」也清不掉那些殘留狀態**。想要乾淨的環境，**請重新啟動服務**（`Ctrl+C` 後重跑），
   或直接改用 agent 實體。

所以這一段只送**一題**就好。接下來的 12 題題組一律在 `qvn-ipm-review`（agent 實體）進行。

> 另外提醒：兩種類別只存在於**本機開發**。Lab 3 部署出去之後，對外只會有
> **一個** `qvn-ipm-review`——不要帶著「應該有兩個」的期待進 Lab 3。

### ⚠️ 關於 `auth_enabled=False`

DevUI 的套件預設是**開啟** Bearer token 驗證，未給 token 時會自動產生一組，
你得回終端機把它找出來才能送出第一則訊息。

本 workshop 一律以 `auth_enabled=False` 啟動——**這是教學場景的權宜做法，不是生產建議**。
它成立的前提有兩個：服務只綁在你自己機器的 loopback 位址上，而且這是一場三十個人
同時操作的課。正式環境 **MUST** 保留驗證。

順帶一提，這兩件事在框架裡是綁在一起的：只有綁 loopback 才允許關閉驗證，
綁 `0.0.0.0` 又關掉驗證會在啟動當下直接失敗。

### 如果瀏覽器顯示 400 Bad Request

代表你的請求經過了連接埠轉發（例如 Codespaces），送來的 `Host` 標頭不在 loopback
允許清單裡。改用備援路徑：

```bash
uv run python main.py --forward
```

這會改綁 `0.0.0.0` 並開啟 token 驗證，終端機會印出一組權杖。
**先開頁面**（首頁不需要 token），再把權杖貼進 DevUI 設定對話框的 `devui_auth_token` 欄位。

> 誠實說明：備援路徑本身已完整實測可用；「Codespaces 的轉發一定會送出不合格的
> `Host` 標頭」則是**推測**，尚未在真實 Codespace 內確認。
> 兩條路徑都準備好了，所以不論哪一種都不會卡住你。

---

## 步驟四：送出 12 題題組，觀察交接

這一段全部在 **`qvn-ipm-review`（agent 實體）** 進行。
不需要在 workflow 實體再跑一次——兩邊是同一套拓撲，路由結果相同。

題組在 `src/fixtures/test_items.py`。要看某一題的完整內容：

```bash
uv run python -c "from fixtures.test_items import TEST_ITEMS, render_prompt; print(render_prompt(TEST_ITEMS[0]))"
```

或者直接開 `src/fixtures/materials/test_items.csv`——十二題的完整提示（含已內嵌的
材料）都在「提示」那一欄，選取儲存格複製就能貼進 DevUI。

依序送出，每題觀察三件事：**交給了誰**、**問了什麼**、**有沒有跳過 Primary**。

| 題號    | 你會看到                                                        |
| ------- | --------------------------------------------------------------- |
| T01–T02 | 程式碼題，交給 `qvn-coding-agent`，再交回 Primary               |
| T03–T04 | 架構題，交給 `qvn-architect-agent`，再交回 Primary              |
| T05–T06 | 規格題，交給 `qvn-spec-agent`，再交回 Primary                   |
| T07–T08 | 意圖含糊，Primary **先反問**，不急著交接                        |
| T09–T10 | 完全超出三位專家的職責，Primary 說明能力邊界，**不交接**        |
| T11–T12 | 跨領域，**逐次**交接：專家 A → 回 Primary → 專家 B → 回 Primary |

**跨領域題（T11–T12）是這一關的重點。** 送出後，如果 agent 停下來等你，
就回「請繼續。」——刻意不提任何專家名稱或領域，否則就變成你在替它做路由。
你會看到它一位一位處理完，中間每次都回到 Primary。

### 驗收

- [ ] portal 的 Agents 區段共有 **4 個** `qvn-` agent
- [ ] 重複執行建立腳本**不會**產生重複 agent
- [ ] 建立腳本在 `qvn-coding-agent` 不存在時**明確報錯並指向 Lab 1**
- [ ] DevUI 可開啟（連接埠與驗證都是明確指定的，不依賴套件預設值）
- [ ] 實體清單上**同時看得到 agent 與 workflow 兩種類別**
- [ ] 已在 **workflow 類別實體上完成一題交接**
- [ ] 我能說出**這兩個項目是同一套系統的兩種呈現**
- [ ] 可在介面上**觀察到控制權從 Primary 轉移至專家**
- [ ] 12 題的交接路由符合上表預期
- [ ] 跨領域題是**逐次**交接，過程中**沒有**同時分派給多位專家

---

## ⚠️ 你會在事件流裡看到工具呼叫

DevUI 的事件流中會出現類似 `transfer_to_qvn-coding-agent` 的工具呼叫。

課程一開始說過「所有 agent 都不掛任何工具」，這裡**不是自相矛盾**。

Handoff 的實作機制**就是**注入交接工具——框架用「呼叫一個工具」來表達「把控制權交給誰」。
`HandoffBuilder` 的官方說明明確寫了它依賴 cloning、**tool injection** 與 middleware，
而且參與者必須支援本地工具呼叫。

|                | 業務工具（本 workshop 禁止） | 交接工具（框架內建） |
| -------------- | ---------------------------- | -------------------- |
| 誰定義         | 我們自己                     | 框架自動注入         |
| 做什麼         | 對外部系統做事               | 只表達「換人主導」   |
| 有外部副作用嗎 | 有                           | **沒有**             |

我們禁止的是「讓 agent 能對外部系統做事」——這樣它才不會宣稱
「我掃描了你的 repository」。禁止的不是「框架用工具機制實作交接」。

---

## 拓撲長什麼樣

```text
              ┌──▶ qvn-coding-agent ────┐
              │                         │
使用者 ──▶ qvn-primary-agent ◀──────────┤
              │                         │
              ├──▶ qvn-architect-agent ─┤
              │                         │
              └──▶ qvn-spec-agent ──────┘
```

程式碼在 [src/workflows/handoff.py](../src/workflows/handoff.py)。關鍵是那幾行
`add_handoff`：Primary 連到三位專家，每位專家只連回 Primary。

**專家之間沒有任何直接邊。** 所以「同時分派給兩位專家」不是靠 instructions
拜託模型別做，而是圖上根本沒有那條路。想確認的話，把每位 agent 允許交接的對象列出來：

```bash
cd src
uv run python -c "
import asyncio
from azure.identity.aio import AzureCliCredential
from config import load_settings_or_exit
from workflows.handoff import create_workflow

async def main():
    s = load_settings_or_exit()
    async with AzureCliCredential() as cred:
        wf = create_workflow(s.foundry_project_endpoint, s.model_deployment_name, cred)
        for e in wf.get_executors_list():
            print(f'{e.id:22} -> {sorted(e._handoff_targets)}')
asyncio.run(main())
"
```

你會看到：

```text
qvn-primary-agent      -> ['qvn-architect-agent', 'qvn-coding-agent', 'qvn-spec-agent']
qvn-coding-agent       -> ['qvn-primary-agent']
qvn-architect-agent    -> ['qvn-primary-agent']
qvn-spec-agent         -> ['qvn-primary-agent']
```

三位專家都只能交回 Primary。把 `src/workflows/handoff.py` 裡的 `add_handoff` 全部註解掉
再跑一次，你會看到它變成全連通——那就是 FR-016 失效的樣子。

---

## 常見狀況

| 現象                                    | 原因                                     | 處理                                                                 |
| --------------------------------------- | ---------------------------------------- | -------------------------------------------------------------------- |
| 建立腳本說找不到 `qvn-coding-agent`     | `.env` 指向的專案與 portal 上的不同      | 照錯誤訊息比對「該專案現有的 agent」清單；不符就是專案選錯           |
| DevUI 開不起來，顯示連接埠被占用        | 8080 已被其他程式使用                    | `uv run python main.py --port 8090`                                  |
| 瀏覽器 400 Bad Request                  | 經過連接埠轉發                           | 改用 `uv run python main.py --forward`                               |
| 實體清單只看到一個項目                  | 服務是舊版程式碼啟動的                   | 回終端機確認啟動訊息有列出兩個名稱；沒有就 `Ctrl+C` 重跑             |
| workflow 類別實體送出後沒反應           | 前一輪還在跑，或模型配額壓力             | 等一分鐘；持續發生就改用 agent 實體，兩者是同一套拓撲                |
| 對話跑到一半沒反應                      | 模型部署配額用盡或服務端壅塞             | 等一分鐘重試；持續發生就調高部署的 TPM 配額                          |
| 出現「觸發流量限制（HTTP 429）」        | 短時間送太多題，或全班同時操作同一個部署 | 等一分鐘再送；這是降級回覆而非崩潰                                   |
| 連續快速送出多則，agent 只回答第一段    | 都落在同一 session，但處理順序不保證     | 貼材料**一次貼完**，不要拆成多則連發                                 |
| 出現「⚠️ 我原本要把這一段交給…」        | 該專家目前不可用                         | **這是正常的降級行為**，訊息裡有修復步驟；系統不會崩潰               |
| 改了 `src/agents/` 但行為沒變           | 沒重跑建立腳本，或 DevUI 沒重啟          | 重跑 `uv run python -m registry.create_agents`，再重啟 DevUI         |
| 專家回覆不是 JSON                       | 該 agent 的 `response_format` 沒吃到     | 重跑建立腳本；若仍如此，回 portal 確認 Lab 1 的 response format 還在 |
| 回覆旁出現「Unable to process request」 | 套件的已知缺陷（見下一節）               | 重新啟動服務再送下一題；這不是你的環境問題                           |

---

## 「新對話」沒你想的那麼乾淨（誠實揭露）

Handoff 工作流的狀態（誰在主導、交接歷史）本來就該綁在單一對話上。
但這一版的套件**做不到**，下面兩件事都是 2026-07-28 實測確認的：

1. **workflow 實體**：按「新對話」後，它仍讀得到上一段對話的內容。
2. **agent 實體**：第一輪交接結束後，繼續追問或開新對話都可能回
   `Unexpected content type while awaiting request info responses.`

**想要乾淨的環境，唯一可靠的做法是重新啟動服務**（`Ctrl+C` 後重跑 `uv run python main.py`）。
這不是你的環境有問題，也不是你操作錯——是 `agent-framework-devui`（仍為 prerelease）
目前的行為。講師手上有完整的根因與現場替代方案，收尾時會說明。

---

## 下一步

[Lab 3：部署到 Azure](03-lab3-deploy.md)——同一套拓撲，怎麼變成別人也能用的服務。
