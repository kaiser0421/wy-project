金流回呼與 API 在 host 上執行時的路由（皆為暫定，TODO(T3.1)）：
- 回呼 URL 設定在 **payment-mock** 服務上（變數名稱暫定 `PAYMENT_CALLBACK_URL`），改成 `http://host.docker.internal:8000/api/v1/payments/mockpay/callbacks`；Linux 需在 `compose.dev.yaml` 為 payment-mock 加上 `extra_hosts: ["host.docker.internal:host-gateway"]`。
- host 上的 API 呼叫 payment-mock 需要它的 port 也由 `compose.dev.yaml` 發佈到 127.0.0.1（API 端變數名稱暫定 `PAYMENT_PROVIDER_URL`）。
- compose 內的 worker / outbox-relay / telemetry-collector **維持原本的 compose 內部 DSN**（`postgres:5432`、`redis:6379`），不需要改。

最簡單的做法是只把 API 拿出來跑，其他照常：
