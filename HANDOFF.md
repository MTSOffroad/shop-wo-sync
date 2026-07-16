# Handoff — Shop Work Order Sync (MTS Off-Road)

Paste this into a fresh Claude Cowork session (with the `shop-wo-sync` folder
connected) to continue. It summarizes a build done in a separate chat session.

## What this project is

An integration so shop work orders flow between **Shopify** (where they
originate as draft orders) and a **monday.com** board called **Shop Work
Orders** (board ID `18422437311`), where techs run the job.

- Work orders start in Shopify as **draft orders**.
- A draft flagged `custom.shop_car_ = true` should appear on the monday board.
- Status changes on the board sync back to the Shopify order as a log.
- At job completion, fulfillment behavior forks by job type (see below).

## Files in this folder

- `sync.py` — **Shopify → monday.** Polls Shopify for `shop_car_ = true` drafts
  not yet tagged `synced-to-monday`, creates a monday work order for each, then
  tags the draft so it's never duplicated. VALIDATED against live data.
- `status_sync.py` — **monday → Shopify.** For each board item, appends the
  current monday status to the linked Shopify draft's Note field as a log
  (newest on top, no duplicates, read-then-append). VALIDATED.
- `requirements.txt` — one dependency (`requests`).
- `README.md` — deploy instructions.
- (this file) `HANDOFF.md`

## Real metafield keys (Shopify, `custom` namespace)

Note the trailing underscores — these are the ACTUAL keys on the store:
- `shop_car_` (boolean) — THE TRIGGER. true = put on board.
- `same_day_` (boolean) — true = board status "Same Day (Not Built)".
- `car_or_shocks_` (choice) — routing + fulfillment switch. Values:
  Car (Walk-in), Set of Shocks (Walk-in), Partial Set of Shocks (Walk-in),
  Set of Shocks (Ship-in), Partial Set of Shocks (Ship-in).
- `year`, `vehicle_model`, `type_of_shocks`, `seats`, `services` (list),
  `service_notes`, `service_writer`, `due_date` (date), `pictures` (files).

## monday board column IDs (board 18422437311)

- Status = `status`
- Car or Shocks? = `color_mm5agxce`
- Due on = `date5`
- Turn Around Time (timeline) = `timerange_mm52a7b2`
- Service Writer Notes = `text3`
- Tech Notes = `text__1`
- WO # = `text_mm5athg0`
- Shopify Link = `link_mm5ardz5`
- Groups: New In Queue `topics`, In Progress `new_group`,
  Needs Manager QC `group_mm54qfb3`, Ship-Out `new_group28763`,
  Finished Cars Still In Shop `group_title`, Picked Up `new_group14165`.

## Key business rules (decided)

- **Trigger:** `shop_car_ = true` (metafield only; no tag needed).
- **Item name** on the board = customer/dealer name ONLY (no vehicle model).
- **same_day_ = true** → status "Same Day (Not Built)".
- **Turnaround timeline:** same-day = due_date→due_date; non-same-day = starts
  at creation, end set to actual pickup date when status → Picked Up.
- **Status log format:** `[M/D HH:MM MST] <status>`, newest first, appended to
  the Shopify order Note (NOT the timeline — timeline isn't API-writable).
- **Payment is never a trigger** (varies job to job).
- **Fulfillment fork (still to code):** at "QC Finished",
  Walk-in jobs → auto-fulfill (no shipping); Ship-in jobs → release fulfillment
  hold so shipping can pack & ship. Ship-in orders get a hold + tag on creation
  to protect the shipping team.
- **Draft → order completion stays manual** (a person "marks paid" in Shopify).

## What's DONE

- Board built (Shop Work Orders, 18422437311) — lean column set, matched colors.
- `sync.py` and `status_sync.py` written and validated against live data.
- Status→group monday automations (at least "New→New In Queue" and
  "Working on it→In Progress" created; user indicated the rest exist —
  CHECK FOR DUPLICATES before go-live).

## What's LEFT

1. **Deploy** both scripts as scheduled jobs (every ~5 min). Render was the plan
   (as public-repo cron jobs); a GitHub outage blocked it mid-deploy on 7/16.
   Env vars needed: `SHOPIFY_STORE_DOMAIN`
   (`mts-off-road-suspension-tuning.myshopify.com`), `SHOPIFY_ACCESS_TOKEN`
   (shpat_...), `MONDAY_API_TOKEN`. Use `DRY_RUN=true` for first runs.
2. **Write the fulfillment-fork script** (the QC-Finished walk-in/ship-in logic).
3. **Cancel Shopify order #18003** (created in error during testing; must be
   done in Shopify admin — API blocks order cancel).
4. Confirm no duplicate status→group automations on the board.
5. Eventually migrate the team from the live "Shop Cars" board (3334823324) to
   the new "Shop Work Orders" board. The live board was intentionally NOT
   modified.

## GitHub / deploy notes

- Repo: `github.com/MTSOffroad/shop-wo-sync` (was made public to sidestep a
  Render↔GitHub auth problem; contains no secrets — tokens live only in the
  host's env vars).
- If continuing in Cowork: Cowork can write files directly into this folder,
  then use GitHub Desktop (commit → push) to update the repo.
