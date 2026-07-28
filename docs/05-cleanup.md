# 收尾與清理

**適用情境**：完成 Lab 4 後，離開前清理本課程建立的資源。
**完成條件**：刪除本課程建立的 agent、Container Registry 與 Azure Bot，並保留既有資產。

---

## 目標

課程建立的資源可能持續產生費用。刪除 agent 後，還要移除 Lab 3 建立的 Container Registry 與 Lab 4 建立的 Azure Bot。

---

## 步驟一：先看要刪什麼

```bash
cd src
uv run python -m registry.cleanup
```

這是**乾跑**，什麼都不會刪。輸出長這樣：

```text
Foundry 專案：https://<你的專案>.services.ai.azure.com/api/projects/<專案名稱>

保留（不帶 qvn- 前置詞，本腳本一律不碰）：
    · my-existing-agent

待刪除（5 個）：
    ✗ qvn-architect-agent
    ✗ qvn-coding-agent
    ✗ qvn-ipm-review
    ✗ qvn-primary-agent
    ✗ qvn-spec-agent

這是乾跑，什麼都沒有刪除。確認清單無誤後執行：
    cd src && uv run python -m registry.cleanup --confirm
```

**兩份清單都要看。**

「保留」是你原本就有的 agent——確認它們**沒有**被列進待刪清單。
「待刪除」應該只有 `qvn-` 開頭的，數量是 4 個（Lab 1–2）或 5 個（做完 Lab 3 會多一個）。

腳本只認 `qvn-` 前置詞，不帶前置詞的一律不碰。
但**你自己確認一次**還是值得——刪除不可逆。

---

## 步驟二：真的刪除

```bash
uv run python -m registry.cleanup --confirm
```

```text
    ✅ 已刪除 qvn-architect-agent
    ✅ 已刪除 qvn-coding-agent
    ✅ 已刪除 qvn-ipm-review
    ✅ 已刪除 qvn-primary-agent
    ✅ 已刪除 qvn-spec-agent

完成：已刪除 5 個 agent。
```

---

## 步驟三：刪掉計費資源

agent 刪掉不代表錢停了。下面兩項是**真正在計費**的資源，腳本刪不到：

### Container Registry（Lab 3 建立）

```bash
cd deploy
azd down
```

會問你要不要刪除，確認即可。

### Bot Service（Lab 4 發布到 Teams 時建立）

這一項由 Foundry portal 的發布流程自動建立，`azd down` 無法刪除。

到 Azure portal → 你的資源群組 → 找名稱含 `ipm-review` 的 **Azure Bot** 資源 → 刪除。

---

## 驗收

回 Azure portal 的資源群組，確認：

- [ ] 沒有 `qvn-` 開頭的 agent（Foundry 專案的 Agents 清單）
- [ ] 沒有本課程建立的 Container Registry
- [ ] 沒有本課程建立的 Azure Bot
- [ ] **你原本就有的 Foundry 專案、模型部署與 agent 都還在**

最後一項最重要。清理**不應該**動到你的既有資產——
如果發現有東西不見了，那是出問題了，不是預期行為。

---

## 正式環境前的注意事項

課程示範的是如何讓系統運作。部署到正式環境前，必須先評估下列四點。

### 1. 可觀測性：預設會記錄對話內容

託管環境**預設**把使用者訊息與 agent 回覆逐字送進 Application Insights。
本課程用 `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT: "false"` 關掉了它。

**本機不會有這個行為**——只在自己機器上測，永遠不會發現。

要放到工作環境前，先問：誰有權限看那些 traces？裡面會出現什麼內容？
留存多久？符不符合你們的資料處理規範？

### 2. 成本：依 active session 累計，不是依 replica

hosted agent 的計費模型與一般 web 應用**不同**：
它依**同時存在的 session 數**擴展，不是依副本數。

所以 `agent.yaml` 裡那個 `cpu: 0.5 / memory: 1Gi` 描述的是**單一 session**，
會被併發數倍乘。三十個人同時用，就是三十份。

過度配置在這個模型下的代價，比一般服務放大得多。

### 3. 治理與身分：課程用的是最寬鬆的設定

課程裡：

- DevUI 的驗證是**關掉**的（loopback + 教學場景的權宜做法）
- Teams 發布用的是 **「Just you」**，沒有經過任何核准流程
- 權限是直接指派給你個人

正式環境要處理的是完全不同的問題：誰可以部署、誰可以呼叫、
用什麼身分呼叫、以及組織範圍發布需要的管理員核准流程。

### 4. 網路：課程完全沒有碰

課程用的是公開端點。實際的企業環境通常需要私人端點、
輸出流量控管、以及與既有網路架構的整合——這些本課程**一項都沒有示範**。

---

## 常見問題排除

| 現象                                  | 原因                                       | 處理                                                               |
| ------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------ |
| 乾跑列出的 agent 比預期少             | `.env` 指向的專案與你建立 agent 的專案不同 | 比對輸出第一行的專案端點是否正確                                   |
| 乾跑把**你自己的** agent 列進待刪清單 | 那個 agent 名稱剛好以 `qvn-` 開頭          | **不要**執行 `--confirm`；改到 Foundry portal 手動逐一刪除         |
| 刪除時回權限錯誤                      | 缺專案範圍的 `Foundry Project Manager`     | 請管理員指派後重試，或到 Foundry portal 手動刪除                   |
| `azd down` 找不到環境                 | 不在 `deploy/` 目錄，或 azd 環境已被刪     | `cd deploy`；環境不在就到 Azure portal 手動刪除 Container Registry |
| 刪完了但帳單還在跑                    | Bot Service 不會被 `azd down` 刪到         | 到資源群組找名稱含 `ipm-review` 的 **Azure Bot** 手動刪除          |
| 中途按下 Ctrl+C                       | 已刪的無法復原                             | 重跑乾跑檢視剩餘項目，再次 `--confirm`                             |

---

## 這套東西目前還做不到什麼

離開前值得花幾分鐘把這件事想清楚：**你剛剛跑完的鏈路，不是每一層都是穩定版**。
服務本身已 GA、框架核心是穩定版，但把它接到託管與本機工具的那幾層
（`agent-framework-devui`、`agent-framework-foundry-hosting`、`azure-ai-agentserver-*`、
`azd ai agent` 擴充）還在 prerelease、beta 或 preview。分級見 [README](../README.md#版本)。

另外三件本場刻意簡化、**不是生產建議**的事：

- DevUI 以 `auth_enabled=False` 啟動（避免全場卡在 Bearer token）
- Teams 發布取「Just you」範圍（組織範圍需 M365 管理員核准）
- 未做負載測試、成本推估、高可用、最小權限收斂與 RAI policy

---

## 下一步

[如果你卡住了](06-fallback.md#會後重跑)有完整的重跑步驟，
以及 [solutions/](../solutions/README.md) 的完成版程式碼。
