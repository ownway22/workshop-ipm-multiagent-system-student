# 完成版程式碼（救援用）

**什麼時候用**：某一關卡住太久，跟不上進度的時候。

**怎麼用**：**複製檔案覆蓋你的工作目錄**。就這樣。

不需要切換 git 分支、不需要 stash、不需要處理合併衝突。
現場沒有時間處理 git 狀態，也不該讓 git 變成新的卡關點。

---

## Lab 1 卡住

Lab 1 是在 portal 手動操作，沒有檔案可以覆蓋。
打開 [lab1_prompt.md](lab1_prompt.md)，把裡面的內容照著貼進 portal。

三段內容：agent 名稱、instructions、response format。
名稱**必須逐字一致**（`qvn-coding-agent`），否則 Lab 2 找不到它。

---

## Lab 2 卡住

從 repository 根目錄執行：

```bash
cp -r solutions/lab2/* src/
```

覆蓋的內容：

```text
src/agents/      四個 agent 的定義檔
src/models/      資料模型
src/workflows/   Handoff 拓撲與 Responses 主機（續接用 checkpoint）
src/registry/    agent 建立腳本
src/main.py      DevUI 與 Responses 進入點
```

覆蓋後接著跑 [Lab 2 步驟一](../docs/02-lab2-multi-agent.md#步驟一建立另外三個-agent)：

```bash
cd src
uv run python -m registry.create_agents
uv run python main.py
```

> ⚠️ 這**不會**建立 `qvn-coding-agent`——那是 Lab 1 的產物。
> 若腳本報錯找不到它，先回 Lab 1 用 [lab1_prompt.md](lab1_prompt.md) 補上。

`src/.env` **不會**被覆蓋，你原本填的設定還在。

---

## Lab 3 卡住

從 repository 根目錄執行：

```bash
cp -r solutions/lab3/deploy/* deploy/
```

覆蓋的內容：

```text
deploy/Dockerfile     容器映像定義
deploy/azure.yaml     azd 服務宣告
deploy/agent.yaml     agent manifest
deploy/infra/         最小基礎設施（ACR 與授權）
deploy/scripts/       azd 環境設定、bootstrap request／result 與管理員 helper
```

覆蓋後接著跑 [Lab 3 步驟二](../docs/03-lab3-deploy.md#步驟二初始化-azd-環境)。

`deploy/.azure/`（azd 環境設定）**不會**被覆蓋。
保留 `.azure/` 不代表 project／agent 隔離；先確認選取的 environment、既有資產與路由授權。
若缺 ACR role assignment 權限，依 [管理員 bootstrap](../docs/03-lab3-deploy.md#路徑-b管理員-bootstrap)
交接原 environment；不要用覆蓋完成版或重建 environment 迴避 IAM／ABAC。
首次部署與既有升級的分支、版本／session 核對及非刪除式收尾以 [Lab 3](../docs/03-lab3-deploy.md) 為準。

---

## 這些檔案跟 `src/` 是什麼關係

**完全相同的內容。** 它們是同一份程式碼的副本，方便你一行指令救回來。

Native hosting 的候選環境只在 [tests/native_hosting/](../tests/native_hosting/)；
[native 可行性未通過](../docs/native-hosting-migration.md)，因此 `src/` 與 `solutions/lab2/`
都保留 #4 的 host 行為，並同步必要的 public mapping／text builder 相容修改。
PR #6 已合併，正式 pins／lock 已升級，專家回交／Primary 忠實呈現保護與本機模型 history
亦已同步；仍使用 HandoffResponsesHost，runtime 副本一致。這不是 native migration，
也不保證舊 service-managed history 自動相容。**不要**把 function result 對照組
複製成學員的 runtime；目前沒有已淘汰的正式 hosting 救援檔可移除。

`solutions/lab1_prompt.md` 更嚴格：它的內容取自 `src/agents/coding.py`，
逐字一致。若兩者出現差異，以 `src/agents/coding.py` 為準。

---

## 用了完成版之後

**不算作弊，也不影響你完成課程。**

課程的目標是理解這套東西怎麼運作，不是打字比賽。
用完成版銜接上進度，繼續往下做，比卡在同一個地方半小時有價值得多。

如果你想知道原本卡在哪，會後對照 `solutions/` 與你自己的版本即可。
