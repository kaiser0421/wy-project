# AI 使用說明

本專案以兩個 AI 編碼工具協作開發，由作者負責需求解讀、架構決策、取捨與最終驗收。以下說明使用了哪些工具、用在哪些部分，並列出 AI 產生的程式碼中實際發生的錯誤，以及如何發現與修正。

## 1. 使用的 AI 工具與用途

| 工具 | 主要用途 |
| --- | --- |
| **Claude Code**（Anthropic，Claude Opus） | 需求拆解與整體規劃（[PLAN.md](PLAN.md)）、架構與交易邊界設計、模組間介面約定；基礎設施與共用程式（Docker Compose、nginx、migration、設定、日誌、驗證與授權框架）；整合各模組、審查所有變更、執行測試與壓測並修正問題；撰寫交付文件初稿。 |
| **OpenAI Codex CLI** | 依模組在獨立的 Git worktree 平行實作功能；對每批變更做交叉審查與紅隊檢查（併發競爭、權限、故障恢復）；依審查結果修正；核對文件中的數字與程式行號。 |

**各部分由誰產出**

| 部分 | 主要實作 | 審查 |
| --- | --- | --- |
| 訂票、限購、冪等、金流、退款 | Codex | Claude + Codex |
| 活動、座位匯入、報表（Excel） | Codex | Claude + Codex |
| 座位即時推送（outbox、relay、SSE） | Codex | Claude + Codex |
| 監控（指標、告警、日誌查詢） | Codex | Claude + Codex |
| 前端（座位圖、監控頁、深色主題） | Codex | Claude + Codex |
| Compose、nginx、基礎 schema（migration 0001）、驗證與授權、共用框架 | Claude | Codex |
| 各功能的 migration（0002–0006） | Codex | Claude + Codex |
| 測試、壓測工具、文件 | 兩者 | 兩者交叉 |

## 2. 使用方式與把關

1. **先定約定再平行開發**：Claude 先寫好模組邊界、路由權限、交易與事件寫入方式等約定（[平行開發約定](docs/development/workstreams.md)），Codex 再分模組平行實作，避免各自發明介面。
2. **每次變更都經雙重審查**：Claude 審查程式碼後，再由 Codex 以另一個角度審查；審查結論為「修正後才能推送」時，修正後再審一輪，直到通過才推送。審查原文保存在 [docs/ai-reviews/](docs/ai-reviews/)。
3. **以實際執行驗證，不只看程式碼**：單元與整合測試（PostgreSQL 16）、併發測試、移除防線的破壞驗證、端到端流程、瀏覽器截圖，以及兩台機器上的壓測。下列多數錯誤都是在「實際跑起來」時才被發現。
4. **人工決策**：範圍取捨（例如即時推送不用 Redis Streams、匯出維持同步下載）、接受或拒絕審查意見、交付時程，由作者決定。

完整錯誤清單見 [AI 缺陷日誌](docs/ai-defects.log)（共 23 筆）。

## 3. AI 產生的程式碼錯誤（節錄 6 例）

### 例 1：金額約束因 SQL 三值邏輯而失效（Claude Code）
- **錯誤的程式碼**：Claude 產生的初版 schema（送審版本）中，訂單表的檢查條件寫成 `CHECK (total_twd IS NULL OR total_twd = subtotal_twd + fee_twd)`。
- **問題**：小計或服務費為 NULL 時，等式結果是 UNKNOWN，而 PostgreSQL 的 CHECK 會放行 UNKNOWN，於是「總額有值、明細卻是 NULL」的錯誤訂單可以寫入。
- **如何發現**：Codex 審查時用部分欄位為 NULL 的資料推演出反例。
- **如何修正**：合併前改成「三欄全為 NULL，或三欄都有值且加總相符」，並新增測試實際寫入錯誤金額、確認被資料庫拒絕（`backend/tests/integration/test_constraints.py`）。

### 例 2：nginx 在過載時把所有 API 副本判為故障（Claude Code）
- **錯誤的程式碼**：upstream 寫成 `server api:8000 resolve;`，沿用 nginx 預設的 `max_fails=1`。
- **問題**：過載時只要一次轉送失敗，副本就被暫時剔除；3 個副本先後被剔除後，nginx 在約 18 秒間對請求直接回 502，完全不轉送。
- **如何發現**：正式壓測第 1 輪出現 104,136 個瞬間 502；逐秒分析 access log 後發現這些請求的 upstream 欄位只有群組名稱、沒有任何副本位址。
- **如何修正**：設定 `max_fails=0`（過載由 API 快速回 503 處理，不剔除副本），並讓 nginx 的閒置連線早於 Uvicorn 關閉（60 秒對 75 秒）。第 2 輪壓測 502 為 0（[壓測報告](LOADTEST.md)）。

### 例 3：SSE 連線在忙碌時被整批關閉，造成重連風暴（Codex）
- **錯誤的程式碼**：每個 API 每秒重驗所有 SSE 連線的 session，取連線與查詢時限只有 0.2 秒與 0.3 秒，且連續失敗 2 次就關閉該副本全部串流。
- **問題**：2,000 條連線加上持續寫入時，API 一忙就重驗逾時，所有連線同時被切斷並一起重新抓 1 萬席的座位快照，形成風暴：2,000 個客戶端共嘗試連線 9,673 次，快照中位數 64 秒。
- **如何發現**：正式壓測第 1 輪的情境 B，加上本機 smoke 中只有 27／50 條連線同時在線；從 API 日誌的 `seat_stream.recheck_failed` 追到這段程式。
- **如何修正**：時限放寬為 1 秒與 1 秒，連續失敗 5 次才整批關閉（撤銷仍由 Pub/Sub 立即生效），並同步更新 DESIGN Q5 的失效時間說明。第 2 輪 2,000／2,000 個客戶端最終與資料庫一致，快照中位數 0.98 秒。
- **附帶的第二個錯誤**：修正時 Claude 為歸還連線加了逾時並在逾時後呼叫 `terminate()`，但 asyncpg 此時已自行終止並交還連線，導致 `InterfaceError`；由 Codex 第二輪審查與 smoke 發現，已移除。

### 例 4：資料庫逾時被回成 500（Claude Code）
- **錯誤的程式碼**：資料庫的鎖逾時與查詢逾時只在交易包裝層轉成 503；在包裝層之外（例如驗證 session 時）發生的逾時落入通用例外處理器。
- **問題**：過載時使用者收到 `500 internal-error`（看似系統故障），而不是可重試的 `503 server-busy`。
- **如何發現**：從全新 clone 跑壓測 smoke，1,018 個請求中有 13 個 500；API 日誌顯示為 `QueryCanceledError`。
- **如何修正**：在應用程式層的例外處理器（`api/problems.py`）把鎖逾時、查詢逾時、死鎖統一轉成 503 並附 `Retry-After`，加上單元測試；另外在資料庫連線層把持有連線時的客戶端逾時也轉成 503，並以整合測試讓資料庫實際卡住驗證。

### 例 5：前端畫面一片空白（Claude Code）
- **錯誤的程式碼**：nginx 設定沒有 `include mime.types`。
- **問題**：建置後的 JS 與 CSS 以 `text/plain` 回傳，瀏覽器依嚴格 MIME 規則拒絕執行，整個介面空白；用 `curl` 檢查首頁只看到 HTML 回 200，所以沒有發現。
- **如何發現**：用真實瀏覽器截圖時頁面空白，主控台顯示 MIME 錯誤。
- **如何修正**：加入 `include mime.types` 與 `default_type`；之後的瀏覽器截圖與經 nginx 執行的 Playwright 測試都會涵蓋這一點。

### 例 6：新增 migration 卻沒有更新程式要求的資料庫版本（Codex）
- **錯誤的程式碼**：為日誌面板新增了 `0006_recent_logs` migration，但程式中記錄所需版本的 `SCHEMA_HEAD` 仍是 `0005_monitoring`。
- **問題**：migration 執行後，seed 與健康檢查會判定版本不符，整套服務無法啟動。
- **如何發現**：合併時 Claude 審查 diff 發現；Codex 審查時另指出 OpenAPI 文件中的版本範例也未更新。
- **如何修正**：更新版本並新增單元測試，自動比對 `SCHEMA_HEAD` 與 Alembic 的最新版本；之後再漏改會直接讓測試失敗。實際重建整套服務確認 migration 與 seed 正常。

## 4. 心得

- AI 生成的程式碼在**單元測試層級多半正確**，錯誤集中在「元件之間的預設值與邊界條件」：nginx 預設值、SQL 的 NULL 語意、逾時與重試的交互。這類問題要靠**另一個模型審查**加上**實際執行**（瀏覽器、smoke、壓測）才會現形。
- 兩個 AI 交叉審查能找到單一模型的盲點，但也會不斷提出邊際問題；最後仍需要人依風險決定哪些要修、哪些接受並記錄。
