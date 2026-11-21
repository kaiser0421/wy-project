# 告警演練：停掉 relay

- 時間：2026-11-21 21:25（台北），開發筆電上的完整 Compose 堆疊。
- 步驟：`docker stop` relay 容器並送出一次 E2E 流程產生座位事件；約 30 秒後截圖 Admin 監控頁；再 `docker start` relay，約 25 秒後查詢 `alert_events`。
- 結果：
  - 21:25:05 停止 relay。`SERVICE_DOWN`（scope `relay`）於 21:25:10 進入 pending（心跳逾時），持續 5 秒後轉為 firing。
  - 座位事件無法編序，`SERVICE_DOWN`（scope `outbox`）於 21:25:14 進入 pending：最舊待編序事件 5.36 秒，門檻 5 秒。
  - `alert_events.ts` 記錄的是進入 pending 的時間；約 21:26 的截圖中兩筆皆為 firing（畫面將 5.36 四捨五入為 5.4）。
  - Admin 監控頁的 Firing alerts 即時顯示兩筆告警並可 Acknowledge；下方「Recent warnings and errors」列出同時段的服務警告。
  - relay 重新啟動後兩筆告警都轉為 `resolved`。

| 檔案 | 內容 |
|---|---|
| [admin-alerts-firing.png](admin-alerts-firing.png) | 告警觸發時的 Admin 監控頁（整頁） |
| [alert-events.txt](alert-events.txt) | 恢復後查詢 `alert_events` 的原始輸出（firing → resolved） |
