# AI 使用紀錄

## 工具與分工

| 工具 | 實際產出與角色 |
| --- | --- |
| Claude Code | 規劃需求與交易邊界、撰寫 `PLAN.md` 和交付文件初稿。實作 P0／P1 的 Compose、migration、共用 runtime、驗證與授權基礎。整合平行 workstream 的共用介面、服務入口與部署，審查 bundle、修正整合問題並合併。見 `1914243`、`5c6d46e`、`107d916`、`40cb535`。 |
| OpenAI Codex CLI | 在獨立 Git worktree 平行實作 A 訂票與金流、B 活動／匯入／報表、C 座位即時推送、D 監控，以及後續 E 前端與測試。也扮演交叉審查與 red-team，檢查設計反例、交易競爭、權限與故障恢復。見 `853b184`、`d05450e`、`4235115`、`74e43f6`、`5e0151f`。 |

## 整合流程

1. Claude 先訂共用 contracts，包含路由政策、session fence、outbox 寫入、背景 `tick(ctx)`、migration 順序與檔案所有權。[平行開發約定](docs/development/workstreams.md)
2. Codex 依 workstream 在獨立 worktree 開發，以 Git bundle 交回各分支的 commits。Claude 匯入並審查，處理 app factory、服務入口、Compose 與 volumes 的整合。
3. Codex 交叉審查實作與整合 diff，回報風險與驗證建議。整合者依實際問題修正，用 pytest、Compose 整合檢查與端到端流程確認後合併。合併紀錄包括 `1a00f7d`、`c41c2b8`、`5f974b0`、`514ffa8`、`9d19e86`。[實作審查](docs/ai-reviews/implementation/) · [端到端流程](scripts/e2e_flow.py)
4. 後續專項審查發現的問題各自進入修正分支，再合併後端／安全與前端／測試改動。見 `d27af81`、`a7ed2aa`。[專項審查](docs/ai-reviews/expert/)

## 實作中的 AI 程式碼缺陷

以下取自 [AI 缺陷日誌](docs/ai-defects.log)，提交 ID 可用 `git show <commit>` 查閱。

| 缺陷與產生者 | 產生的程式碼及影響 | 發現方式 | 修正與 commit |
| --- | --- | --- | --- |
| D01 · Claude Code | P0 將 bootstrap superuser 的連線字串共用給 API 與背景服務。應用連線因此持有 schema owner 的權限。 | Codex P0 diff review #1 對照 Compose 連線配置與 PostgreSQL 角色。 | 初始化時建立低權限 `ticketing_app`。Compose 將 runtime 與 migration 的 DSN 拆開，migration 以 schema owner 執行並授予 runtime 所需權限。見 `5c6d46e`、`fb2c04c`、`compose.yaml:5`。 |
| D02 · Claude Code | `orders` 金額的 CHECK 只判斷總額為 NULL 或等於小計加服務費。當總額已填且其他欄位為 NULL，等式得到 UNKNOWN，PostgreSQL 仍接受該列。 | Codex P0 diff review #6 以部分 NULL 的資料反推 SQL 三值邏輯。 | 明確要求三欄全 NULL，或三欄皆有值且加總相符。加入 `test_order_amounts_are_all_or_nothing_and_balanced` 實際插入錯誤金額。見 `fb2c04c`、`83df8cd`、`backend/tests/integration/test_constraints.py:96`。 |
| D03 · Claude Code | `payment_captures` 的欄位各自指向有效 attempt 與 order 時，仍可把 attempt 接到另一筆訂單。 | Codex P0 diff review #7 檢查跨欄位資料關聯，找出兩個單欄外鍵都能通過的反例。 | 以 `(attempt_id, order_id)` 複合外鍵連至 `payment_attempts(id, order_id)`，並用 `test_capture_must_belong_to_the_attempts_order` 驗證錯配會遭拒。見 `fb2c04c`、`83df8cd`、`backend/migrations/versions/0001_baseline.py:242`。 |
| D15 · OpenAI Codex | 報表路由在授權前先取得 export slot。無權讀取報表的呼叫仍可能占用有限的匯出容量，與合法下載競爭。 | Workstream B 的 Codex review #6 追蹤報表 handler 的呼叫順序。 | 先檢查活動與報表存取權，再進入匯出流程取得 slot。見 `76e6a21`、`backend/src/ticketing/api/routes/reports.py:169`。 |
| D16 · OpenAI Codex | 活動列表 cursor 的 HMAC 未納入角色。Admin 降權後重新登入仍可能用舊 cursor 繼續翻閱先前的高權限快照。 | Workstream B 的 Codex review #3 比對 cursor 簽章輸入與降權後的身分。 | 將角色納入簽章，角色變更後舊 cursor 驗證失敗。測試另行隔離 TTL 與角色變更兩種失效原因。見 `76e6a21`、`5ec31ca`、`backend/src/ticketing/events/pagination.py:94`。 |

審查原文見 [P0 review](docs/ai-reviews/implementation/p0-codex-review.md) 與 [Workstream B review](docs/ai-reviews/implementation/ws-b-codex-review.md)。

## 設計審查紀錄

| 草案問題 | 後續決策與實作 |
| --- | --- |
| 用 watermark 保護活動翻頁，頁間編輯可能改變排序與成員。 | 第一頁物化清單快照，後續透過簽章 cursor 取出快照內的頁次。見 `d232fa4`。 |
| 以應用程序 wall clock 或 collector 收件時間對監控分桶，補送與時鐘偏移可能造成假尖峰。 | 保存 producer boot UUID、interval number 與單調時間，用 Redis stream 接收時間估算偏移並分桶。見 `81b82db`。 |
| 推送草案設計了 Redis Stream、generation 與 epoch fencing，增加狀態同步成本。 | 範圍決策將座位序號、歷史與回補集中到 PostgreSQL，API 以程序內 ring 派送 SSE。見 `4235115`。[範圍決策](docs/development/workstreams.md#4-範圍決策相對-plan-v12) |

規劃期原文見 [v1.0 審查](docs/ai-reviews/planning/plan-review-v1.0.md)、[v1.1 複查](docs/ai-reviews/planning/plan-review-v1.1.md) 與 [架構草案審查](docs/ai-reviews/planning/architecture-review.md)。
