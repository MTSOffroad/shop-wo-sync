#!/usr/bin/env python3
"""
Powdercoat Status Sync  (MTS Powdercoat Jobs -> Shop Work Orders)
-----------------------------------------------------------------
When a powdercoat card is marked done on the "MTS Powdercoat Jobs" board, flip
the matching Shop Work Order's Powdercoat column to "Powdercoat Ready".

Correlation: each powder card that our sync created carries a "Draft Order" link
(column link_mm5b171t) pointing at the Shopify draft. The Shop Work Order item
carries the same draft in its "Shopify Link" column. We match on the draft's
numeric id — so this NEVER touches the powder board's legacy items (they have no
Draft Order link).

Efficiency: we drive from the (small) Shop Work Orders board, looking only at
items currently flagged "Powdercoat Needed". For each, we do a targeted query of
the powder board filtered to the card with that draft link, and check its status.
Idempotent: an item already "Powdercoat Ready" is skipped; only "Powdercoat
Needed" -> "Powdercoat Ready" transitions are written.

Required environment variables (secret — never in code):
  MONDAY_API_TOKEN
Optional:
  SHOP_BOARD_ID     default 18422437311
  POWDER_BOARD_ID   default 7932059042
  DRY_RUN           "true" to log without writing
"""

import os
import re
import sys
import json
import datetime
import requests

MONDAY_TOKEN = os.environ.get("MONDAY_API_TOKEN", "").strip()
SHOP_BOARD_ID = os.environ.get("SHOP_BOARD_ID", "18422437311").strip()
POWDER_BOARD_ID = os.environ.get("POWDER_BOARD_ID", "7932059042").strip()
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

MONDAY_GQL = "https://api.monday.com/v2"

# Shop Work Orders columns
SHOP_PC_COL = "color_mm5bzr35"       # Powdercoat (None / Powdercoat Needed / Powdercoat Ready)
SHOP_WO_COL = "text_mm5athg0"        # WO # (e.g. "#D8303") — the correlation key

# Powdercoat Jobs columns
PC_STATUS_COL = "status"
PC_ORDER_NO_COL = "text7__1"         # "Order #" — holds the WO#; filterable (unlike the link)

NEEDED_LABEL = "Powdercoat Needed"
READY_LABEL = "Powdercoat Ready"
# Powder-board statuses that mean the powder work is done.
DONE_POWDER_STATUSES = {"Finished"}


def log(msg):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def die(msg, code=1):
    log(f"FATAL: {msg}")
    sys.exit(code)


def monday_gql(query, variables=None):
    r = requests.post(
        MONDAY_GQL,
        headers={"Authorization": MONDAY_TOKEN, "Content-Type": "application/json",
                 "API-Version": "2024-10"},
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"monday errors: {json.dumps(data['errors'])}")
    return data["data"]


def wo_key(wo):
    """Normalize a WO# for filtering (drop a leading '#' so contains_text is clean)."""
    return (wo or "").lstrip("#").strip()


def fetch_pending_shop_items():
    """Shop items currently flagged 'Powdercoat Needed', with their WO#."""
    query = """
    query ShopItems($board: ID!, $cursor: String) {
      boards(ids: [$board]) {
        items_page(limit: 100, cursor: $cursor) {
          cursor
          items {
            id
            column_values(ids: ["color_mm5bzr35", "text_mm5athg0"]) { id text value }
          }
        }
      }
    }
    """
    pending = []
    cursor = None
    while True:
        data = monday_gql(query, {"board": SHOP_BOARD_ID, "cursor": cursor})
        page = data["boards"][0]["items_page"]
        for it in page["items"]:
            cv = {c["id"]: c for c in it["column_values"]}
            pc_text = (cv.get(SHOP_PC_COL) or {}).get("text") or ""
            if pc_text != NEEDED_LABEL:
                continue
            wo = (cv.get(SHOP_WO_COL) or {}).get("text") or ""
            if wo.strip():
                pending.append({"item_id": it["id"], "wo": wo.strip()})
        cursor = page.get("cursor")
        if not cursor:
            break
    return pending


def powder_status_for_wo(wo):
    """Return the status text of the powder card for this WO#, or None.

    Matches on the powder card's "Order #" text column (which holds the WO#),
    because monday cannot filter the link column by text.
    """
    query = """
    query PowderByWO($board: ID!, $wo: String!) {
      boards(ids: [$board]) {
        items_page(limit: 5, query_params: {
          rules: [{column_id: "text7__1", compare_value: $wo, operator: contains_text}]
        }) {
          items { id column_values(ids: ["status"]) { id text } }
        }
      }
    }
    """
    data = monday_gql(query, {"board": POWDER_BOARD_ID, "wo": wo_key(wo)})
    items = data["boards"][0]["items_page"]["items"]
    if not items:
        return None
    cv = {c["id"]: c for c in items[0]["column_values"]}
    return (cv.get(PC_STATUS_COL) or {}).get("text") or ""


def set_powdercoat_ready(item_id):
    if DRY_RUN:
        log(f"    DRY_RUN: would set item {item_id} -> {READY_LABEL}")
        return
    mutation = """
    mutation SetReady($board: ID!, $item: ID!, $cols: JSON!) {
      change_multiple_column_values(board_id: $board, item_id: $item,
          column_values: $cols) { id }
    }
    """
    cols = {SHOP_PC_COL: {"label": READY_LABEL}}
    monday_gql(mutation, {"board": SHOP_BOARD_ID, "item": item_id,
                          "cols": json.dumps(cols)})


def main():
    if not MONDAY_TOKEN:
        die("Missing env var: MONDAY_API_TOKEN")

    mode = "DRY_RUN" if DRY_RUN else "LIVE"
    log(f"Powdercoat status sync starting ({mode}). "
        f"Shop {SHOP_BOARD_ID} <- Powder {POWDER_BOARD_ID}.")

    try:
        pending = fetch_pending_shop_items()
    except Exception as e:
        die(f"Could not read Shop Work Orders board: {e}")

    if not pending:
        log("No 'Powdercoat Needed' work orders. Nothing to do.")
        return

    log(f"{len(pending)} work order(s) awaiting powdercoat.")
    ready = waiting = failed = 0

    for item in pending:
        try:
            status = powder_status_for_wo(item["wo"])
            if status in DONE_POWDER_STATUSES:
                set_powdercoat_ready(item["item_id"])
                log(f"  ✓ item {item['item_id']} ({item['wo']}) "
                    f"-> {READY_LABEL} (powder '{status}')")
                ready += 1
            else:
                waiting += 1  # powder card not finished yet (or not found)
        except Exception as e:
            log(f"  ✗ item {item['item_id']} FAILED: {e}")
            failed += 1

    log(f"Done. Marked ready {ready}, still waiting {waiting}, failed {failed}.")
    if failed:
        sys.exit(2)


if __name__ == "__main__":
    main()
