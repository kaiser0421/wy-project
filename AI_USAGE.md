# AI_USAGE.md — AI 工具使用說明

> 狀態：**實作前骨架（Pre-coding skeleton）**。本文件在開發過程中持續更新；第 4 節「AI 產生的程式碼錯誤」只收錄實作期間**實際發生並被發現**的錯誤（來源：`docs/ai-defects.log`），目前尚無條目。

---

## 1. 使用的 AI 工具

| 工具 | 角色 | 使用方式 |
|---|---|---|
| **Claude Code**（模型：Claude Opus 系列；實際版本於交付時依 session 紀錄填入 `TBD(T9.6)`） | 主要實作者與規劃者 | 需求分析、設計研究、撰寫 `PLAN.md`、程式碼與測試實作、文件撰寫（`DESIGN.md`、`LOADTEST.md`、本文件）。 |
| **OpenAI Codex CLI** | 審查者 / 紅隊（reviewer / red-team） | 以非互動、唯讀模式（`codex exec -s read-only`）讀取 repo 檔案，對設計草案與每個 Phase 的 diff 做批判、找反例與故障情境。不直接修改程式碼。 |

人類（專案負責人）負責：需求解讀的最終裁決、PLAN 開工前確認事項（PLAN §20）、合併前審閱、所有「是否接受 AI 修改」的決定。

---

## 2. AI 用在哪些部分

### 2.1 規劃階段（已完成）

規劃流程刻意設計為「一個 AI 提案、另一個 AI 攻擊」，以降低單一模型的盲點：

1. **3 個平行研究 session**（Claude Code），各自負責一個領域：
   - S1：訂票核心（防超賣、限購、冪等、付款、逾時）
   - S2：即時推送與監控（SSE、事件順序、遙測、告警）
   - S3：安全與平台（認證、授權、匯入、匯出、部署）
2. **每個 session 與 Codex 進行 3 輪辯論**：草案批判 → 反駁與故障情境 → 收斂與紅隊。原始紀錄目前在規劃期暫存目錄（`research/{s1,s2,s3}/`），將整理進 repo 的 `docs/ai-reviews/`。
3. **跨 session 裁決**：三份結論合併時處理衝突（例如推送傳輸 SSE vs WebSocket、事件 seq 來源、session 是否快取），裁決理由記錄於 `PLAN.md` §19。
4. **整份計畫的 Codex 審查（v1.0 → v1.1）**：Codex 對完整 `PLAN.md` 做完整度/品質審查，結論為「需要先修正才能開工」，找出 **4 個 P0**、5 個 P1、1 個 P2，全數修正後成為 v1.1（`PLAN.md` §19.3）。
5. **第二次複查（v1.1 → v1.2）**：Codex 判定 10 項皆「部分修正」並列出 13 處內部不一致，修正後成為 v1.2（`PLAN.md` §19.3 末段）。
6. **DESIGN.md / AI_USAGE.md 初稿**：由 Claude Code 撰寫、Codex 2 輪審查（對照 requirement 第八節與二.7：每個子題是否有回答、是否與 PLAN 一致、是否有誇大或臆造的證據）。第 1 輪指出的問題包括：Q4.2 續傳游標可能跳過缺口、Q2.2 的「約 30 秒」只是理想輪詢預算而非退款收斂上界、Q1.3 部分破壞版無法隔離單一防護層；均已修正。審查紀錄目前在規劃期暫存目錄，`git init` 後的第一批 commit 會整理進 repo 的 `docs/ai-reviews/`（PLAN §21.3）。

### 2.2 實作階段（規劃中；實作開始後逐 Phase 更新）

| Phase | AI 用途 | 人類驗證重點 |
|---|---|---|
| P0 地基 | compose、CI、migration、測試基礎設施樣板 | `docker compose up -d --build` 與 `make test` 實際執行 |
| P1 認證授權 | `SecureRouter`、session、授權矩陣測試產生器 | 「刻意漏 policy 會紅」的驗證 |
| P2 訂票核心 | 訂票交易 SQL、`release_order`、併發與破壞測試 | 破壞版必須紅、不變式 SQL |
| P3 金流 | payment-mock、回呼 handler、reconciler | 回呼重放/亂序/kill -9 測試 |
| P4 即時推送 | relay、reader/ring、SSE | 故障注入與最終 hash 比對 |
| P5–P8 | 匯入、報表、監控、前端 | 各 Phase DoD |
| P9 | 壓測腳本、文件 | 實測數字只來自實際執行輸出 |

（各 Phase 實際使用情況與比例於完成時補充：`TBD`）

### 2.3 每個 Phase 的 Codex diff review

（規劃，尚未執行）每個 Phase 結束時：跑 `make test` + 該 Phase DoD，再請 Codex 對該 Phase 的 diff 做一次審查（重點：併發、授權、錯誤處理）。審查發現的 AI 錯誤記入 `docs/ai-defects.log`，修正以獨立 `fix:` commit 提交。

---

## 3. 人類驗證政策

1. **AI 的輸出一律視為未驗證的草稿**；以測試、實際執行輸出或人工審閱證明後才合併。
2. **正確性主張必須有可執行的證據**：每個並行/一致性保證對應一個自動化測試；關鍵保護必須通過「把保護拿掉 → 同一測試失敗」的破壞驗證（`DESIGN.md` Q1.3）。
3. **數字只來自實際量測**：`LOADTEST.md`、`DESIGN.md` 中的效能與收斂時間只能貼實際執行輸出；未量測者標示 `TBD`，不得由 AI 估算後當作結果。
4. **雙模型交叉審查**：Claude 產生、Codex 審查；兩者意見衝突時由人類裁決並記錄理由。
5. **AI 錯誤紀錄即時化**：發現 AI 產生的錯誤時立即寫入 `docs/ai-defects.log`（工具、commit、發現方式、修正 commit），不事後回憶補寫。
6. **測試掛鉤隔離**：AI 產生的破壞版/測試時鐘等掛鉤只存在於 test image，由 config validator 確保不進入生產。

---

## 4. AI 產生的程式碼錯誤（requirement 二.7：至少 3 處）

> **收錄規則**：只收錄實作期間**實際發生**、由 AI 產生且已被發現並修正的程式碼錯誤；每筆都必須能對應到 `docs/ai-defects.log` 的紀錄與 GitHub 上的 commit。**不寫假設性範例**（例如 `PLAN.md` §5.5 提到的 Python `round()` 銀行家捨入陷阱，只有在實作時真的發生才會列入）。
>
> 目前狀態：**尚未開始（實作未開始）**——尚無條目。

### 條目範本

```markdown
### D-<編號>：<一句話描述>

- **位置**：`<檔案路徑>:<行號>`（commit <短 SHA，連結>）
- **產生者**：<Claude Code / Codex>，Phase <Px> / Task <Tx.y>
- **錯在哪裡**：<AI 產生的程式碼做了什麼、正確行為應該是什麼>
- **影響**：<若未被發現會造成什麼後果，例如超賣、配額漂移、授權繞過>
- **如何發現**：<哪個測試 / Codex review / 壓測 / 人工審閱；附失敗輸出摘錄>
- **如何修正**：<修正方式>；修正 commit <短 SHA，連結>
- **防止再發**：<新增的測試或規則>
```

### D-1：`TBD`（實作期間依實際發生填入）

### D-2：`TBD`（實作期間依實際發生填入）

### D-3：`TBD`（實作期間依實際發生填入）

---

## 5. 規劃階段 AI 設計錯誤（由交叉審查發現）

> **注意**：以下是**設計層級**的錯誤——由 Claude 在規劃草案中提出、被 Codex 交叉審查推翻，並在寫任何程式碼之前修正。它們**不是程式碼錯誤，不計入第 4 節要求的 ≥ 3 處**，列出只是為了完整呈現 AI 在本專案中的實際使用情況與審查價值。來源：`PLAN.md` §19、§19.3。

| # | 草案中的錯誤 | 被指出的後果 | 修正（PLAN v1.1） |
|---|---|---|---|
| P-1 | 座位快照不帶每座位 version | 快照已是 `available/v2`、但 `held/v1` 尚未指派 seq 之後才送達 → 客戶端畫面倒退 | 快照與 delta 都帶 per-seat version，客戶端只套用較大 version（§6.1–6.3） |
| P-2 | 付款建立結果不明（逾時）時將 attempt 標為 `abandoned` | 釋出唯一 slot 後可建立第二個可扣款 intent → 重複扣款 | 結果不明時一律以同一 merchant id 冪等重試；只有向金流確認 intent 不可扣款後才 `abandoned`（§5.4） |
| P-3 | 期限後回呼的座位釋放與配額處理散落在多條路徑 | 期限後 capture 走通用退款路徑會重複扣減配額 | 唯一釋放路徑 `release_order()`，以 `WHERE status=:expected RETURNING` 為前提（§5.1） |
| P-4 | 匯入 admission 與活動發布沒有互斥 | 可能先回 202 再被 finalize 拒絕，或在已發布活動上寫入座位 | 活動閘門交易：發布/admission/finalize/刪除都先 `events FOR UPDATE` 並重檢（§5.6） |
| P-5 | 付款期限以交易開始時間判斷 | 回呼在鎖上等待跨過期限仍被判為期限前 → 對已釋放/轉售的座位做出錯誤的「已付款」決策（若座位更新又缺少歸屬 guard 則會雙賣） | 取鎖後以 `clock_timestamp()` 取樣 + 期限後分支（§5.6） |
| P-6 | 以 xid/id 游標推進 outbox | 晚提交的較小 id 會被跳過 → 漏事件 | relay 輪詢 `seq IS NULL` 並在 PG 指派連續 seq（§6.1） |
| P-7 | Redis session 快取 | 兩種設計都會在撤銷後繼續放行（DEL 後回填；PG 提交後寫 Redis 前崩潰） | 不快取，每請求 PG 查詢（§7.1） |
| P-8 | 匯入 staging 用 UNLOGGED 表 | crash 後被清空，與 fence/lease 接手設計衝突 | 改為 LOGGED（§4、§8） |
| P-9 | 檔內重複只標第一筆以外的列 | 不符合「重複列視為錯誤列」 | 重複群組每一列都標錯（§8） |
| P-10 | watermark 分頁 | 擋不住頁間編輯/成員變化/同值排序 | 第一頁物化快照（§11） |
| P-11 | 遙測以 producer 時鐘/接收時間分桶 | 補送擠在同一秒形成假尖峰；skew 與佇列延遲混淆 | `interval_no` + collector offset 分桶（§10.2–10.3） |

---

## 6. 附錄：證據位置

| 項目 | 位置 |
|---|---|
| 規劃階段 Codex 審查原始紀錄 | `docs/ai-reviews/`（自規劃期暫存目錄的 `research/s1–s3`、`plan_review.md`、`plan_reverify.md`、`docs/d2/` 搬入；`git init` 後第一批 commit） |
| 實作期 AI 錯誤日誌 | `docs/ai-defects.log`（`TBD`，P0 建立） |
| 每 Phase Codex diff review 紀錄 | `docs/ai-reviews/<phase>.md`（`TBD`） |
| AI 錯誤修正 commit | GitHub commit 歷史（`fix:` 前綴，於第 4 節逐筆連結） |
