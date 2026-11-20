## 5. 本地開發（不經 Docker 跑 API）

> **建議的本地運行方式就是 §4 的 `docker compose up -d --build`。** 以下「API 在 host 上以 `--reload` 執行」的流程僅供開發迭代；整合測試一律在 compose 內、使用與正式環境相同的 PostgreSQL 16 image。<!-- TODO(T0.1, T0.2): 以下 override 檔、變數名稱與模組路徑於實作時確認 -->

預設 compose 不對 host 開放 PostgreSQL / Redis，host 上的 API 需要一個開發用 override 檔把它們發佈到 localhost：

```bash
# 1. 工具：Python 3.12+、uv、Node.js 20+、Docker
uv sync                                                        # 安裝 Python workspace 依賴
docker compose -f compose.yaml -f compose.dev.yaml up -d postgres redis   # compose.dev.yaml 發佈 5432 / 6379 到 127.0.0.1（暫定）

# 2. 讓 host 程序連到 localhost 的 PG / Redis（變數名稱以 .env.example 為準，暫定）
export DATABASE_URL=postgresql+asyncpg://<user>:<password>@127.0.0.1:5432/<db>
export REDIS_URL=redis://127.0.0.1:6379/0
export COOKIE_SECURE=false

uv run alembic upgrade head                                    # 套用 migration
uv run python -m ticketing.seed                                # 建立 §7 的測試帳號（冪等）
uv run uvicorn ticketing.main:app --reload --port 8000

# 3. 前端（Vite dev server，proxy /api 至 8000，同源 cookie）
cd apps/frontend && npm ci && npm run dev
```

金流回呼：payment-mock 會把 HMAC 簽章回呼送到設定的回呼 URL（變數名稱暫定 `PAYMENT_CALLBACK_URL`）。API 在 host 上執行時，若 payment-mock 仍在 compose 內，需把回呼 URL 設為 `http://host.docker.internal:8000/api/v1/payments/mockpay/callbacks`；其餘背景程序（worker、outbox-relay、telemetry-collector）同樣需指向 host 可達的 PG / Redis。最簡單的做法是只把 API 拿出來跑，其他照常：

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d worker outbox-relay telemetry-collector payment-mock
```

`make` 目標速查：`make lint`、`make test`、`make test-fast`、`make verify-all`（見 §8）。<!-- TODO(T0.1, T0.6) -->

---
