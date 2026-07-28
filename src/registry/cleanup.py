"""刪除本 workshop 建立的 agent（課後清理）。

執行方式（cwd 必須是 `src`）：

    cd src && uv run python -m registry.cleanup            # 先看要刪什麼
    cd src && uv run python -m registry.cleanup --confirm  # 真的刪

## 安全設計

**只刪 `qvn-` 前置詞的 agent。** 沒有這個前置詞的一律不碰——學員的 Foundry 專案裡
很可能有其他人或其他專案的 agent，誤刪是不可逆的。

**預設是乾跑（dry run）。** 不帶 `--confirm` 只會列出清單，不會刪任何東西。
刪除是不可逆操作，不可讓「手滑執行一次指令」就造成損失。

**不刪除 Foundry 專案或模型部署。** 那兩者是學員的既有資產，
不是本 workshop 建立的。

## 這支腳本刪不到的東西

Lab 3、Lab 4 會建立**計費資源**，它們不是 agent，本腳本刪不到：

- Azure Container Registry（`azd down` 或手動刪除）
- Bot Service（Teams 發布時建立）

執行完會提示，請自行到 portal 確認並移除，否則會持續計費。
"""

import argparse
import asyncio
import sys

from agents import get_agent_name_prefix
from config import load_settings_or_exit

#: 刪除單一 agent 的逾時（秒）。
_OPERATION_TIMEOUT_SECONDS = 60


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="刪除本 workshop 建立的 agent（預設只列出，不刪除）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="不帶 --confirm 時只會列出清單，不會刪除任何東西。",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="真的執行刪除。省略時只列出待刪清單（乾跑）",
    )
    return parser.parse_args()


def _build_credential():
    from azure.identity.aio import AzureCliCredential

    return AzureCliCredential()


async def _collect(project_client, prefix: str) -> tuple[list[str], list[str]]:
    """回傳 `(待刪清單, 保留清單)`。

    兩份清單都要回傳並印出：只印待刪清單的話，看不出「有沒有誤判成保留」。
    課後清理最怕的兩種錯都在這一步顯形——該刪的沒列到、不該刪的列進去了。
    """
    to_delete: list[str] = []
    to_keep: list[str] = []
    async for agent in project_client.agents.list():
        (to_delete if agent.name.startswith(prefix) else to_keep).append(agent.name)
    return sorted(to_delete), sorted(to_keep)


async def run(confirm: bool) -> int:
    settings = load_settings_or_exit()
    prefix = get_agent_name_prefix()

    from azure.ai.projects.aio import AIProjectClient

    async with _build_credential() as credential:
        async with AIProjectClient(
            endpoint=settings.foundry_project_endpoint,
            credential=credential,
        ) as project_client:
            to_delete, to_keep = await _collect(project_client, prefix)

            print(f"Foundry 專案：{settings.foundry_project_endpoint}")
            print()

            if to_keep:
                print(f"保留（不帶 {prefix} 前置詞，本腳本一律不碰）：")
                for name in to_keep:
                    print(f"    · {name}")
                print()

            if not to_delete:
                print(f"沒有找到任何 {prefix} agent，無需清理。")
                return 0

            print(f"待刪除（{len(to_delete)} 個）：")
            for name in to_delete:
                print(f"    ✗ {name}")
            print()

            if not confirm:
                print("這是乾跑，什麼都沒有刪除。確認清單無誤後執行：")
                print("    cd src && uv run python -m registry.cleanup --confirm")
                return 0

            for name in to_delete:
                await asyncio.wait_for(
                    project_client.agents.delete(name),
                    timeout=_OPERATION_TIMEOUT_SECONDS,
                )
                print(f"    ✅ 已刪除 {name}")

    print()
    print(f"完成：已刪除 {len(to_delete)} 個 agent。")
    print()
    print("⚠️ 下列資源**不是** agent，本腳本刪不到，請自行確認：")
    print("    · Azure Container Registry（Lab 3 建立）")
    print("      cd deploy && azd down")
    print("    · Bot Service（Lab 4 發布到 Teams 時建立）")
    print("      到 Azure portal 的資源群組中確認並刪除")
    print("    兩者都會**持續計費**，不清理會一直算錢。")
    return 0


def main() -> None:
    args = _parse_args()
    try:
        raise SystemExit(asyncio.run(run(args.confirm)))
    except KeyboardInterrupt:
        # 刪除進行到一半被中斷時，已刪的無法復原；明確告知而非靜默結束。
        sys.stdout.flush()
        print("\n已中斷。已刪除的 agent 無法復原；重跑本指令可檢視剩餘項目。", file=sys.stderr)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
