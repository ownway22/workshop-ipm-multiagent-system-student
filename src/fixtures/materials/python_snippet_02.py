"""虛構教學材料：庫存同步服務（片段二）。

⚠️ 這是 workshop 的**虛構**分析材料，刻意保留可被發現的品質問題，MUST NOT 當成範例程式碼
   模仿。所有名稱、端點與識別碼皆為虛構，不對應任何真實系統。

用途：Lab 1 至 Lab 4 的檢查點中，貼給程式碼健檢專家分析。
"""

import threading
import time

import requests

CACHE = {}
API_BASE = "http://inventory-internal.example.invalid/api"
RETRY = 5


class InventorySync:
    def __init__(self, warehouse_id, token):
        self.warehouse_id = warehouse_id
        self.token = token
        self.session = requests.Session()

    def get_stock(self, sku):
        if sku in CACHE:
            return CACHE[sku]

        for i in range(RETRY):
            r = self.session.get(
                API_BASE + "/stock/" + sku,
                headers={"Authorization": "Bearer " + self.token},
                verify=False,
            )
            if r.status_code == 200:
                CACHE[sku] = r.json()["quantity"]
                return CACHE[sku]
            time.sleep(1)

        return 0

    def sync_all(self, skus):
        threads = []
        for sku in skus:
            t = threading.Thread(target=self.get_stock, args=(sku,))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        return CACHE

    def push_adjustment(self, sku, delta):
        payload = {"sku": sku, "delta": delta, "warehouse": self.warehouse_id}
        r = self.session.post(API_BASE + "/adjustments", json=payload, verify=False)
        print("adjustment response:", r.status_code, r.text)
        return r.status_code == 200
