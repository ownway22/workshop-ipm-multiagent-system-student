# 虛構材料：庫存同步平台架構快照（快照二）

> ⚠️ 這是 workshop 的**虛構**分析材料。所有訂閱、資源群組、資源名稱與識別碼皆為虛構，
> 不對應任何真實環境。用途是貼給架構健檢專家分析。

## 基本資訊

| 項目       | 值                                |
| ---------- | --------------------------------- |
| 環境       | Staging（與 Production 共用資源） |
| 訂閱       | `sub-invsync-shared`（虛構代號）  |
| 資源群組   | `rg-invsync-shared`               |
| 主要區域   | Southeast Asia                    |
| 快照取得日 | 第 1 天（虛構情境的相對日期）     |

## 資源清單

| 資源名稱            | 型別                     | SKU / 層級          | 備註                                         |
| ------------------- | ------------------------ | ------------------- | -------------------------------------------- |
| `aks-invsync`       | Azure Kubernetes Service | 3 節點 D2s_v3       | Staging 與 Production 同一個叢集不同命名空間 |
| `acr-invsync`       | Container Registry       | Basic               | 未啟用內容信任                               |
| `sb-invsync`        | Service Bus              | Standard            | 單一命名空間，佇列無死信處理                 |
| `cosmos-invsync`    | Cosmos DB                | 佈建輸送量 400 RU/s | 單一區域寫入                                 |
| `func-invreconcile` | Azure Functions          | Consumption         | 每 5 分鐘對帳                                |
| `st-invsnapshots`   | Storage Account          | Standard_GRS        | 保留全部歷史快照，無生命週期規則             |
| `appi-invsync`      | Application Insights     | —                   | 取樣率 100%                                  |

## 網路與身分

| 項目           | 現況                                            |
| -------------- | ----------------------------------------------- |
| 虛擬網路       | `vnet-invsync`，單一子網路                      |
| AKS 節點       | 具公開 IP                                       |
| Cosmos DB 存取 | 允許所有網路                                    |
| 應用程式身分   | AKS 工作負載使用 Kubernetes Secret 中的存取金鑰 |
| 秘密輪替       | 尚無流程                                        |

## 部署與環境隔離

| 項目     | 現況                                                      |
| -------- | --------------------------------------------------------- |
| 環境隔離 | Staging 與 Production 共用叢集、共用 Service Bus 命名空間 |
| 部署方式 | 由開發者本機執行腳本推送映像並套用 manifest               |
| 映像標籤 | 一律使用 `latest`                                         |
| 回復方式 | 手動重新套用前一版 manifest                               |

## 容量與成本

| 項目             | 現況                               |
| ---------------- | ---------------------------------- |
| Cosmos DB 輸送量 | 固定 400 RU/s，尖峰時段常見節流    |
| AKS 節點利用率   | 平均 CPU 18%                       |
| 儲存體成長       | 每月約成長 40 GB，無封存或刪除規則 |
| 成本檢視         | 未設定預算警示                     |
