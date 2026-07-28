# 虛構材料：訂單平台架構快照（快照一）

> ⚠️ 這是 workshop 的**虛構**分析材料。所有訂閱、資源群組、資源名稱與識別碼皆為虛構，
> 不對應任何真實環境。用途是貼給架構健檢專家分析。

## 基本資訊

| 項目       | 值                               |
| ---------- | -------------------------------- |
| 環境       | Production                       |
| 訂閱       | `sub-orderplat-prod`（虛構代號） |
| 資源群組   | `rg-orderplat-prod`              |
| 主要區域   | East Asia                        |
| 次要區域   | 無                               |
| 快照取得日 | 第 1 天（虛構情境的相對日期）    |

## 資源清單

| 資源名稱           | 型別                  | SKU / 層級       | 備註                           |
| ------------------ | --------------------- | ---------------- | ------------------------------ |
| `app-order-api`    | App Service           | S1（1 執行個體） | 對外 API，公開端點             |
| `plan-orderplat`   | App Service Plan      | S1               | 與背景工作共用同一個 plan      |
| `app-order-worker` | App Service           | 同上 plan        | 背景批次，與 API 共用運算資源  |
| `sql-orderplat`    | Azure SQL Database    | S2，單一資料庫   | 未設定異地複寫                 |
| `st-orderexports`  | Storage Account       | Standard_LRS     | 存放匯出的 CSV，容器為公開讀取 |
| `kv-orderplat`     | Key Vault             | Standard         | 存放連線字串                   |
| `redis-ordercache` | Azure Cache for Redis | Basic C0         | 單節點                         |
| `appi-orderplat`   | Application Insights  | —                | 僅 API 有埋設，worker 無       |

## 網路與身分

| 項目           | 現況                                                               |
| -------------- | ------------------------------------------------------------------ |
| 虛擬網路       | 未使用，所有服務走公開端點                                         |
| 私人端點       | 無                                                                 |
| SQL 防火牆     | 允許 `0.0.0.0` 至 `255.255.255.255`                                |
| 應用程式身分   | 使用儲存在 App Service 設定值中的連線字串，未使用 managed identity |
| Key Vault 存取 | 以存取原則授權給一組共用的服務主體                                 |

## 備份與復原

| 項目           | 現況         |
| -------------- | ------------ |
| SQL 備份保留   | 7 天（預設） |
| 異地備援       | 未啟用       |
| 還原演練       | 尚未執行過   |
| RTO / RPO 目標 | 文件中未定義 |

## 擴充與監控

| 項目     | 現況                               |
| -------- | ---------------------------------- |
| 自動調整 | 未設定，固定 1 個執行個體          |
| 健康檢查 | 未設定                             |
| 警示規則 | 僅有一則「App Service 停止」的通知 |
| 日誌保留 | 30 天                              |
