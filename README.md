# Shop Work Order Sync

Polls Shopify for draft orders flagged as shop jobs (`custom.shop_car_ = true`)
and creates a matching work order on the monday.com **Shop Work Orders** board
(ID `18422437311`). Dedup is tag-based: synced drafts get the `synced-to-monday`
tag and are never picked up again.

Runs as a single pass per invocation — deploy it as a **scheduled cron job**
(e.g. every 5 minutes on Render).

## Environment variables (set as secrets — never commit them)

| Variable | Required | Example / default |
|---|---|---|
| `SHOPIFY_STORE_DOMAIN` | yes | `mts-off-road-suspension-tuning.myshopify.com` |
| `SHOPIFY_ACCESS_TOKEN` | yes | `shpat_...` (Admin API token) |
| `MONDAY_API_TOKEN` | yes | your monday API v2 token |
| `MONDAY_BOARD_ID` | no | `18422437311` |
| `MONDAY_GROUP_ID` | no | `topics` (New In Queue) |
| `DRY_RUN` | no | `true` to log without writing |

## Test locally first (dry run — writes nothing)

```bash
pip install -r requirements.txt
export SHOPIFY_STORE_DOMAIN="mts-off-road-suspension-tuning.myshopify.com"
export SHOPIFY_ACCESS_TOKEN="shpat_xxx"
export MONDAY_API_TOKEN="xxx"
export DRY_RUN="true"
python sync.py
```

You should see it list any un-synced `shop_car_` drafts and print the monday
item it *would* create. When it looks right, set `DRY_RUN=false` (or unset it)
to go live.

## Deploy on Render (recommended)

1. Push this folder to a private GitHub repo (or use Render's "deploy from
   local" flow).
2. In Render: **New → Cron Job**.
3. Connect the repo. Settings:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Command:** `python sync.py`
   - **Schedule:** `*/5 * * * *` (every 5 minutes)
4. Under **Environment**, add the secrets from the table above.
5. Create the job. Watch the first run in the **Logs** tab.

Start with `DRY_RUN=true` for the first live deploy, confirm the logs look
right, then remove it.

## Safety notes

- The `synced-to-monday` tag is added only *after* the monday item is created,
  so a mid-run crash retries the draft next cycle — it never duplicates.
- The Shopify token can read/write orders. Keep it only in Render's secret
  store, never in the code or the repo.
- This script only creates board items. Status-change sync back to Shopify,
  fulfillment holds/releases, and draft→order completion are separate pieces
  (see the build spec).
