#!/usr/bin/env python3
"""
Shop Work Order Sync
--------------------
Polls Shopify for draft orders flagged as shop jobs (custom.shop_car_ = true)
and creates a matching work order on the monday.com "Shop Work Orders" board.

Dedup is tag-based: once a draft is synced it gets the SYNCED_TAG in Shopify,
so it is never picked up again. State lives on the draft itself, so host
restarts / redeploys can never cause duplicates.

Designed to run on a schedule (e.g. a Render Cron Job every 5 minutes).
Each run is a single pass: query un-synced drafts, create items, tag them, exit.

Required environment variables (set these as secrets on the host — never in code):
  SHOPIFY_STORE_DOMAIN   e.g. mts-off-road-suspension-tuning.myshopify.com
  SHOPIFY_ACCESS_TOKEN   the shpat_... Admin API token
  MONDAY_API_TOKEN       your monday.com API v2 token

Optional (have sensible defaults):
  MONDAY_BOARD_ID        default 18422437311 (Shop Work Orders)
  MONDAY_GROUP_ID        default "topics" (New In Queue)
  DRY_RUN                "true" to log actions without writing anything
"""

import os
import sys
import json
import datetime
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SHOPIFY_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN", "").strip()
SHOPIFY_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "").strip()
MONDAY_TOKEN = os.environ.get("MONDAY_API_TOKEN", "").strip()

MONDAY_BOARD_ID = os.environ.get("MONDAY_BOARD_ID", "18422437311").strip()
MONDAY_GROUP_ID = os.environ.get("MONDAY_GROUP_ID", "topics").strip()
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

SHOPIFY_API_VERSION = "2025-01"
SYNCED_TAG = "synced-to-monday"
SHOP_ADMIN_HANDLE = "mts-off-road-suspension-tuning"  # for building the draft link

# monday column IDs on the Shop Work Orders board (18422437311)
COL_WO = "text_mm5athg0"            # WO #
COL_LINK = "link_mm5ardz5"          # Shopify Link
COL_STATUS = "status"              # Status
COL_CAR_OR_SHOCKS = "color_mm5agxce"  # Car or Shocks?
COL_DUE = "date5"                  # Due on
COL_TURNAROUND = "timerange_mm52a7b2"  # Turn Around Time
COL_SVC_NOTES = "text3"            # Service Writer Notes
COL_HOURS = "numeric_mm0mfy8z"     # Hours (REQUIRED column — must be set on create)

# Shopify custom metafield the service writer fills in with the job's hours.
# Its value is pushed to the monday "Hours" column so the two stay aligned.
MF_HOURS = "job_hours_"

SHOPIFY_GQL = f"https://{SHOPIFY_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
MONDAY_GQL = "https://api.monday.com/v2"


def log(msg):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def die(msg, code=1):
    log(f"FATAL: {msg}")
    sys.exit(code)


# ---------------------------------------------------------------------------
# Shopify helpers
# ---------------------------------------------------------------------------
def shopify_gql(query, variables=None):
    r = requests.post(
        SHOPIFY_GQL,
        headers={
            "X-Shopify-Access-Token": SHOPIFY_TOKEN,
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"Shopify GraphQL errors: {json.dumps(data['errors'])}")
    return data["data"]


def fetch_unsynced_shop_drafts():
    """Return open draft orders where shop_car_ = true and not yet synced.

    We filter server-side on status + tag, then check the shop_car_ metafield
    client-side (metafields aren't reliably filterable in the draftOrders query).
    """
    # We scan BOTH open and completed drafts. "Pay up front" jobs get their
    # draft completed into a real Order quickly; if we only scanned open drafts
    # we could miss one that completed before this cycle ran. Completed drafts
    # still expose their metafields and a link to the created `order`.
    query = """
    query UnsyncedShopDrafts($cursor: String) {
      draftOrders(first: 25, after: $cursor, sortKey: UPDATED_AT, reverse: true,
                  query: "status:open OR status:completed") {
        pageInfo { hasNextPage endCursor }
        edges {
          node {
            id
            legacyResourceId
            name
            createdAt
            tags
            email
            status
            order { id legacyResourceId tags }
            customer { displayName }
            metafields(first: 30) {
              edges { node { namespace key value type } }
            }
          }
        }
      }
    }
    """
    results = []
    cursor = None
    # Only scan a bounded number of pages; un-synced shop drafts are always recent.
    for _ in range(6):
        data = shopify_gql(query, {"cursor": cursor})
        conn = data["draftOrders"]
        for edge in conn["edges"]:
            node = edge["node"]
            draft_tags = node.get("tags") or []
            order = node.get("order") or {}
            order_tags = order.get("tags") or []
            # Dedup marker lives on the draft (open drafts) OR the order
            # (completed drafts, which may not be tag-updatable). Check both.
            if SYNCED_TAG in draft_tags or SYNCED_TAG in order_tags:
                continue  # already synced
            mf = {
                e["node"]["key"]: e["node"]["value"]
                for e in node["metafields"]["edges"]
                if e["node"]["namespace"] == "custom"
            }
            if str(mf.get("shop_car_", "")).lower() != "true":
                continue  # not a shop work order
            node["_mf"] = mf
            results.append(node)
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return results


def mark_synced(draft):
    """Add SYNCED_TAG so this work order is never picked up again.

    If the draft has already been completed into a real Order (pay-up-front),
    we tag the ORDER — completed drafts are not reliably tag-updatable, and
    orders always are. Otherwise we tag the open draft. The read side checks
    both places, so dedup holds either way.
    """
    order = draft.get("order") or {}
    if order.get("id"):
        target_gid = order["id"]
        existing = order.get("tags") or []
        where = f"order {order.get('legacyResourceId')}"
    else:
        target_gid = draft["id"]
        existing = draft.get("tags") or []
        where = f"draft {draft['name']}"

    if SYNCED_TAG in existing:
        return  # already tagged (belt and suspenders)

    if DRY_RUN:
        log(f"  DRY_RUN: would tag {where} with '{SYNCED_TAG}'")
        return

    # tagsAdd works for both Order and DraftOrder GIDs.
    mutation = """
    mutation MarkSynced($id: ID!, $tags: [String!]!) {
      tagsAdd(id: $id, tags: $tags) {
        node { id }
        userErrors { field message }
      }
    }
    """
    data = shopify_gql(mutation, {"id": target_gid, "tags": [SYNCED_TAG]})
    errs = data["tagsAdd"]["userErrors"]
    if errs:
        raise RuntimeError(f"Failed to tag {where}: {errs}")


# ---------------------------------------------------------------------------
# monday helpers
# ---------------------------------------------------------------------------
def monday_gql(query, variables=None):
    r = requests.post(
        MONDAY_GQL,
        headers={
            "Authorization": MONDAY_TOKEN,
            "Content-Type": "application/json",
            "API-Version": "2024-10",
        },
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"monday GraphQL errors: {json.dumps(data['errors'])}")
    return data["data"]


def build_column_values(draft):
    mf = draft["_mf"]
    name = draft["name"]  # e.g. #D8297
    legacy_id = draft["legacyResourceId"]
    link = f"https://admin.shopify.com/store/{SHOP_ADMIN_HANDLE}/draft_orders/{legacy_id}"

    same_day = str(mf.get("same_day_", "")).lower() == "true"
    car_or_shocks = mf.get("car_or_shocks_")  # e.g. "Set of Shocks (Walk-in)"
    due = mf.get("due_date")  # "YYYY-MM-DD" or None
    svc_notes = mf.get("service_notes", "")

    # Hours from the service writer's custom.job_hours_ metafield. The board's
    # Hours column is required, so we always send a value — the metafield if
    # present and numeric, otherwise 0 (a tech can fill it in later).
    hours_raw = mf.get(MF_HOURS)
    try:
        hours_val = str(float(hours_raw)) if hours_raw not in (None, "") else "0"
    except (TypeError, ValueError):
        hours_val = "0"

    cols = {
        COL_WO: name,
        COL_LINK: {"url": link, "text": f"Open Draft {name}"},
        COL_STATUS: {"label": "Same Day (Not Built)" if same_day else "New"},
        COL_HOURS: hours_val,  # from custom.job_hours_; 0 if unset
    }
    if car_or_shocks:
        cols[COL_CAR_OR_SHOCKS] = {"label": car_or_shocks}
    if svc_notes:
        cols[COL_SVC_NOTES] = svc_notes[:1900]  # keep it sane
    if due:
        cols[COL_DUE] = {"date": due}

    # Turnaround rules:
    #   same-day  -> start = end = due_date (if due set)
    #   otherwise -> start = creation date (end filled at pickup, handled elsewhere)
    if same_day and due:
        cols[COL_TURNAROUND] = {"from": due, "to": due}
    elif not same_day:
        created_date = draft["createdAt"][:10]  # YYYY-MM-DD
        # provisional end = due date if we have one, else leave end == start
        end = due if due else created_date
        cols[COL_TURNAROUND] = {"from": created_date, "to": end}

    return cols


def item_name_for(draft):
    """Item name = customer / dealer only (no vehicle model)."""
    cust = (draft.get("customer") or {}).get("displayName")
    if cust:
        return cust
    if draft.get("email"):
        return draft["email"]
    return f"Work Order {draft['name']}"


def create_monday_item(draft):
    cols = build_column_values(draft)
    name = item_name_for(draft)
    mutation = """
    mutation CreateWO($board: ID!, $group: String!, $name: String!, $cols: JSON!) {
      create_item(board_id: $board, group_id: $group, item_name: $name,
                  column_values: $cols, create_labels_if_missing: false) {
        id
        name
      }
    }
    """
    variables = {
        "board": MONDAY_BOARD_ID,
        "group": MONDAY_GROUP_ID,
        "name": name,
        "cols": json.dumps(cols),
    }
    if DRY_RUN:
        log(f"  DRY_RUN: would create monday item '{name}' cols={json.dumps(cols)}")
        return "dry-run-id"
    data = monday_gql(mutation, variables)
    return data["create_item"]["id"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    missing = [k for k, v in {
        "SHOPIFY_STORE_DOMAIN": SHOPIFY_DOMAIN,
        "SHOPIFY_ACCESS_TOKEN": SHOPIFY_TOKEN,
        "MONDAY_API_TOKEN": MONDAY_TOKEN,
    }.items() if not v]
    if missing:
        die(f"Missing required env vars: {', '.join(missing)}")

    mode = "DRY_RUN" if DRY_RUN else "LIVE"
    log(f"Shop WO sync starting ({mode}). Board {MONDAY_BOARD_ID}, group {MONDAY_GROUP_ID}.")

    try:
        drafts = fetch_unsynced_shop_drafts()
    except Exception as e:
        die(f"Could not fetch Shopify drafts: {e}")

    if not drafts:
        log("No un-synced shop_car_ drafts found. Nothing to do.")
        return

    log(f"Found {len(drafts)} un-synced shop work order draft(s).")
    created, failed = 0, 0

    for draft in drafts:
        wo = draft["name"]
        try:
            item_id = create_monday_item(draft)
            # Only mark synced AFTER the item is created — so a crash retries,
            # never duplicates.
            mark_synced(draft)
            log(f"  ✓ {wo} -> monday item {item_id} (synced)")
            created += 1
        except Exception as e:
            log(f"  ✗ {wo} FAILED: {e}")
            failed += 1

    log(f"Done. Created {created}, failed {failed}.")
    if failed:
        # Non-zero exit so the host's cron surfaces the failure in logs/alerts.
        sys.exit(2)


if __name__ == "__main__":
    main()
