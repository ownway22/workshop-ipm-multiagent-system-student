# Lab 3：hosted agent 的容器映像
#
# 建置上下文（context）是 **repository 根目錄**，不是本檔所在目錄——因為要複製 `src/`。
# 上下文由 `azure.yaml` 的 `services.qvn-ipm-review.docker.context` 指定。
#
# ⚠️ **不要**在 FROM 這一行加 `--platform=linux/amd64`。ACR Tasks 的遠端建置會先做
# 相依掃描，而該掃描器看不懂這個語法，會直接以
#   「unable to understand line FROM --platform=linux/amd64 python:3.13-slim-bookworm
#     failed to run step ID: build: failed to scan dependencies」
# 失敗。平台改由 `azure.yaml` 的 `docker.platform: linux/amd64` 單點指定即可：
# ACR Tasks 本身就在 amd64 上建置，azd 的本機建置路徑則會依該設定傳 `--platform`。

FROM python:3.13-slim-bookworm

# uv 釘住版本，與開發環境一致。
COPY --from=ghcr.io/astral-sh/uv:0.9.25 /uv /usr/local/bin/uv

WORKDIR /app

# 先只複製相依定義，讓相依層在原始碼變動時仍能命中快取。
COPY src/pyproject.toml src/uv.lock ./

# `--locked` 確保用的是 uv.lock 內釘死的版本；解析結果與 lock 不符會直接失敗，
# 不會靜默升級。`--no-dev` 排除 pytest。
RUN uv sync --locked --no-dev

# 本專案的程式碼。`.dockerignore` 已排除 `.venv`、`__pycache__` 與 `.env`，
# 其中 `.venv` 尤其重要：開發機的 venv 可能是 arm64，混進映像會直接壞掉。
COPY src/ ./

# `ResponsesHostServer.run()` 未傳 port 時讀 `PORT`；託管平台會注入自己的值，
# 這裡的 8088 只是本機建置測試時的預設。
ENV PORT=8088
EXPOSE 8088

# 與本機 `uv run python main.py --responses` 是**同一支程式**，差別只有繫結位址：
# 本機綁 loopback（不把免驗證的端點暴露到區域網），容器內必須綁 0.0.0.0 才收得到流量。
#
# 不用 shell 形式，讓 PID 1 直接是 Python，SIGTERM 才會被
# ResponsesHostServer 註冊的 handler 收到並優雅關閉。
CMD ["uv", "run", "--no-sync", "python", "main.py", "--responses", "--host", "0.0.0.0"]
