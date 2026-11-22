# 相依套件與機密掃描（2026-11-22）

對交付版本（`ee80d7a`）執行三項掃描。只掃描、不修改程式。

| 掃描 | 工具與指令 | 範圍 | 結果 |
| --- | --- | --- | --- |
| Python 相依套件弱點 | `uv export --all-packages --no-dev --no-hashes --format requirements-txt > requirements.txt`，再以 pip-audit 2.10.1 執行 `python -m pip_audit -r requirements.txt --disable-pip --no-deps` | 後端與 paymock 執行期相依套件（`uv.lock` 鎖定版本） | **0 個已知弱點**。本 repo 的兩個本地套件（`backend`、`paymock`）不在公開弱點資料庫內，pip-audit 略過 |
| 前端相依套件弱點 | `npm audit --omit=dev`（npm 10.9.9） | 會進入瀏覽器打包的套件 | **0 個弱點** |
| 前端開發工具 | `npm audit` | 含開發相依套件 | 7 個 moderate，全部來自 typescript-eslint 相依的 `minimatch`（[GHSA-g3hv-75wg-m249](https://github.com/advisories/GHSA-g3hv-75wg-m249)，特製 glob 樣式可造成過多運算）。只在開發與 CI 執行 ESLint 時使用，不會進入正式打包，輸入是 repo 內的設定檔 |
| 機密外洩 | `docker run --rm -v "$PWD":/repo zricethezav/gitleaks:v8.21.2 git /repo --no-banner --redact` | 本機 Git refs 可達的非合併提交差異（掃描回報 199 個 commit） | **未偵測到機密**（no leaks found）。合併提交的差異未納入本次掃描 |

## 處理

- 正式執行環境的 Python 與前端套件沒有已知弱點，不需處理。
- 開發工具的 moderate 弱點不影響執行中的服務；升級 typescript-eslint 屬於開發依賴更新，留待後續與其他工具一起更新並重跑 lint。
- 這些掃描尚未放進 CI；後續可將 `pip-audit`、`npm audit --omit=dev --audit-level=high` 與 gitleaks 加入 CI。
