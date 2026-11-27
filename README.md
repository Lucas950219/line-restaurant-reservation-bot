# LINE 餐廳訂位助理

顧客在 LINE 官方帳號的私訊中送出完整訂位資料。助理驗證營業時間、分配空桌，並回覆訂位編號。5 人以上先選每桌人數。輸入「訂位」可取得填寫範例。

## 顧客怎麼訂位

同一則訊息填齊四項資料。日期可填「今天」「明天」「後天」「大後天」「下週一」「11月30日」或完整日期。相對日期以訊息送出時的台灣日期換算，只填月日時採該年的日期。

```text
日期：明天
時間：11:30
人數：4
聯絡方式：0912345678
```

一行就能訂位，例如 `後天 18:00 2 0912345678`、`明天晚上六點八個人guest@example.com`，或 `我要訂位八位明天晚上6:30，電話：0912-345-678`。時間接受 `18:00`、`晚上六點`、`晚上六點半` 等寫法；寫「六點」時請補上時段，例如「晚上六點」。聯絡方式接受電話或電子郵件。資料漏填或格式有誤時，助理會提供範例，請顧客重傳完整訊息。

如果輸入 `明天 18:00 8 guest@example.com`，助理會在 LINE 顯示當下可用的拆桌按鈕，例如 **4＋4** 或 **2＋2＋4**。顧客須在 10 分鐘內、且在訂位時間前點選。機器人會在點選時重新確認空桌，再一次保留所有桌並回傳一組訂位編號。如果原拆法已滿，會顯示其他可用拆法。 [LINE 快速回覆按鈕](https://developers.line.biz/en/docs/messaging-api/using-quick-reply/)

## 初版訂位規則

| 項目 | 設定 |
| --- | --- |
| 營業時間 | 每天 11:30–14:30、17:00–20:00，採台灣時間 |
| 桌位 | 6 張雙人桌、6 張四人桌 |
| 分桌 | 1–2 人優先安排雙人桌，雙人桌不足時可安排四人桌。3–4 人安排四人桌。5–36 人按當下空桌選擇拆法；超過 36 人請洽餐廳。 |
| 用餐時間 | 預設 90 分鐘，同一張桌的訂位時段不可重疊。午餐最晚可訂 13:00，晚餐最晚可訂 18:30。 |
| 訂位資料 | 儲存於本機 `.runtime/reservations.sqlite3`，服務重啟後仍會保留。 |

要調整用餐時間，可在本機 `.env` 設定 `RESERVATION_DURATION_MINUTES`，單位為分鐘。已成立訂位保留原先的結束時間。新訂位及桌位檢查都在本機完成，聯絡方式保存在本機訂位資料庫。

## 1. 安裝與啟動

在 Windows PowerShell 進入專案資料夾。需要 Python 3.11 或以上。如果已建立專案的虛擬環境，可直接從「設定 LINE」開始。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
```

## 2. 設定 LINE

已有 LINE 官方帳號的 Messaging API Channel，可沿用同一組憑證。初次設定時，在 LINE Official Account Manager 建立官方帳號並啟用 Messaging API，再用同一個 Business ID 打開 LINE Developers Console 的 Channel。 [官方帳號管理後台](https://manager.line.biz/) · [LINE Developers Console](https://developers.line.biz/console/) · [開通指南](https://developers.line.biz/en/docs/messaging-api/getting-started/)

| `.env` 欄位 | 後台位置 | 用途 |
| --- | --- | --- |
| `LINE_CHANNEL_SECRET` | Basic settings → Channel secret | 驗證收到的 LINE Webhook |
| `LINE_CHANNEL_ACCESS_TOKEN` | Messaging API → Channel access token | 用官方帳號回覆顧客 |

把兩項資料填入本機的 `.env`。啟動訂位服務後，任何傳訊息給官方帳號的顧客都能在一對一聊天室訂位。

## 3. 啟動後端與 Webhook

在專案資料夾啟動服務。

```powershell
.\.venv\Scripts\python.exe -X utf8 bot.py serve
```

本機健康檢查可使用 `Invoke-RestMethod http://127.0.0.1:8000/health`。要從手機 LINE 測試，可用 cloudflared 建立臨時 HTTPS 網址。已安裝的專案本機版本位於 `.runtime/cloudflared.exe`。 [Cloudflare Quick Tunnel](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)

```powershell
cloudflared tunnel --url http://127.0.0.1:8000
```

將 HTTPS 網址加上 `/webhook`，填到 LINE Developers Console 的 **Messaging API → Webhook settings**，按 **Verify** 並開啟 **Use webhook**。若收到預設罐頭回覆，可從 **Auto-reply messages → Edit** 前往管理後台關閉自動回覆。臨時通道重開後會換網址，屆時也要更新 LINE Webhook。 [LINE Webhook 和自動回覆設定](https://developers.line.biz/en/docs/messaging-api/building-bot/)

`bot.py ask` 可繼續用來測試本機 Codex。訂位訊息由本機程式直接解析，因此訂位回覆使用 LINE Messaging API 和本機 SQLite。

## 店家訂位與備餐後台

開啟 `.runtime/dashboard-view-link.txt` 裡的店家專屬連結，即可在 GitHub Pages 查看唯讀看板。專屬連結包含查看權限，請只傳給有權查看訂位的人。顧客電話和電子郵件保存在本機訂位資料庫。

選擇營業日期可查看已成立的訂位、訂位時間、總人數和拆桌。備餐估算以每個餐期的已訂位人數乘上每人份數，再加上備量比例，最後向上取整。預設每位一份、備量 10%，可在頁面調整。

看板會依實際分配的雙人桌與四人桌顯示各餐期的人數和桌次，明細也按桌型分欄。桌次是所選日期或餐期內的使用次數。

顧客在 LINE 聊天室輸入「功能」即可開啟快速選單，點選「我要訂位」、「我的訂位」、「改期訂位」或「取消訂位」。選單只顯示該 LINE 帳號即將到來的訂位。改期時用 LINE 的日期時間選擇器選擇台灣時間，取消前會顯示訂位內容並要求確認。也可輸入「取消 B000001」或「改期 B000001 明天晚上六點」，並換成自己的訂位編號。取消後會釋放桌位。新時段有足夠桌位時，改期會保留原編號與分桌人數。

店家可開啟 `.runtime/dashboard-manage-link.txt` 裡的專屬管理連結，直接取消或改期，也可以補登電話和現場訂位。店家補登的聯絡方式可留空。

店家管理連結使用 `.env` 中獨立的 `DASHBOARD_MANAGEMENT_TOKEN`。原本的檢視連結仍是唯讀，管理連結只交給能調整訂位的人。

GitHub Pages 的 `gh-pages` 分支發布 `dashboard/` 裡的 HTML、CSS 和 JavaScript。發布時把 HTML 的 `dashboard-api` 設成公開 HTTPS 通道加上 `/dashboard/reservations`，並在本機 `.env` 設定 `DASHBOARD_SITE_ORIGIN` 為 Pages 的來源網域（目前為 `https://lucas950219.github.io`）。後端用 `.env` 的 `DASHBOARD_ACCESS_TOKEN` 核對專屬連結的 `#view=` 值，金鑰與訂位明細留在後端。臨時通道網址改變後，需一併更新 LINE Webhook 和 Pages 的 API 網址。 [GitHub Pages 發布來源](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)

## 後續可討論的細節

用餐時間、相鄰桌安排、特殊公休日與可提前預訂天數。
