#!/usr/bin/env python3
"""
Fulfillment Fork  (monday status -> Shopify fulfillment)
--------------------------------------------------------
Third piece of the Shop Work Order sync. Reads the monday "Shop Work Orders"
board and applies fulfillment actions to the linked Shopify ORDER based on
job type (Walk-in vs Ship-in) and board status.

Business rules (decided with the shop):
  * Job type comes from the board's "Car or Shocks?" column (color_mm5agxce),
    which mirrors the Shopify custom.car_or_shocks_ metafield. Values ending in
    "(Ship-in)" are ship-ins; "(Walk-in)" are walk-ins.
  * SHIP-IN, before QC Finished:  place a fulfillment HOLD on the order and tag
    it "ship-in-hold", so the shipping team can't send it out mid-service.
  * SHIP-IN, at QC Finished:       RELEASE the hold, swap the tag to
    "ready-to-ship". Shipping then packs, adds tracking, and fulfills MANUALLY
    (this script never fulfills a ship-in).
  * WALK-IN, at QC Finished:       auto-mark the order FULFILLED (customer picks
    up in person, nothing ships), tag "walk-in-fulfilled".

Everything is gated on a real paid ORDER existing (fulfillment lives on orders,
not drafts). If a work order is still an unpaid draft, there is nothing to hold
or fulfill and it is skipped — which is correct: a non-existent order can't be
shipped early. Pay-at-the-end jobs simply get handled once their order appears.

Idempotency is enforced with ORDER TAGS (ship-in-hold / ready-to-ship /
walk-in-fulfilled) plus the live fulfillment-order state, so running every
minute never double-holds, double-releases, or double-fulfills.

Required environment variables (secrets — never in code):
  SHOPIFY_STORE_DOMAIN, SHOPIFY_ACCESS_TOKEN, MONDAY_API_TOKEN
Optional:
  MONDAY_BOARD_ID   default 18422437311
  DRY_RUN           "true" to log intended actions without writing

NOTE ON SHOPIFY SCOPES: the Admin API token must have order + fulfillment-order
read/write scopes:
  read_orders, write_orders,
  read_merchant_managed_fulfillment_orders, write_merchant_managed_fulfillment_orders
  (and the assigned/third-party variants if you use external fulfillment).
If holds/fulfills fail with an access-scope error, add these to the custom app
and reinstall to mint a new token.
"""

import os
import re
import sys
import json
import datetime
import requests

SHOPIFY_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN", "").strip()
SHOPIFY_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "").strip()
MONDAY_TOKEN = os.environ.get("MONDAY_API_TOKEN", "").strip()
MONDAY_BOARD_ID = os.environ.get("MONDAY_BOARD_ID", "18422437311").strip()
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

SHOPIFY_API_VERSION = "2025-01"
SHOPIFY_GQL = f"https://{SHOPIFY_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
MONDAY_GQL = "https://api.monday.com/v2"

# Board column IDs (Shop Work Orders, 18422437311)
COL_STATUS = "status"
COL_CAR_OR_SHOCKS = "color_mm5agxce"
COL_LINK = "link_mm5ardz5"

# Order tags used as idempotency markers.
TAG_HOLD = "ship-in-hold"
TAG_READY = "ready-to-ship"
TAG_FULFILLED = "walk-in-fulfilled"

# Statuses that mean "QC is done / job complete" — the trigger point for
# releasing a ship-in hold or fulfilling a walk-in.
DONE_STATUSES = {"QC Finished", "Picked Up"}

# Hold identity (lets us find/parse our own holds if needed).
HOLD_HANDLE = "mts-shop-service"
HOLD_EXTERNAL_ID = "mts-shop-wo-sync"


def log(msg):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def die(msg, code=1):
    log(f"FATAL: {msg}")
    sys.exit(code)


# --------------------------- monday ---------------------------
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


def fetch_board_items():
    """Return board items with status text, job type, and linked draft id."""
    query = """
    query BoardItems($board: ID!, $cursor: String) {
      boards(ids: [$board]) {
        items_page(limit: 100, cursor: $cursor) {
          cursor
          items {
            id
            name
            column_values(ids: ["status", "color_mm5agxce", "link_mm5ardz5"]) {
              id
              text
              value
            }
          }
        }
      }
    }
    """
    items = []
    cursor = None
    while True:
        data = monday_gql(query, {"board": MONDAY_BOARD_ID, "cursor": cursor})
        page = data["boards"][0]["items_page"]
        for it in page["items"]:
            cv = {c["id"]: c for c in it["column_values"]}
            status = (cv.get(COL_STATUS) or {}).get("text") or ""
            job_type = (cv.get(COL_CAR_OR_SHOCKS) or {}).get("text") or ""
            link_val = (cv.get(COL_LINK) or {}).get("value")
            draft_id = None
            if link_val:
                try:
                    url = json.loads(link_val).get("url", "")
                except Exception:
                    url = ""
                m = re.search(r"/draft_orders/(\d+)", url)
                if m:
                    draft_id = m.group(1)
            if status and draft_id:
                items.append({"name": it["name"], "status": status,
                              "job_type": job_type, "draft_id": draft_id})
        cursor = page.get("cursor")
        if not cursor:
            break
    return items


# --------------------------- Shopify ---------------------------
def shopify_gql(query, variables=None):
    r = requests.post(
        SHOPIFY_GQL,
        headers={"X-Shopify-Access-Token": SHOPIFY_TOKEN, "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"Shopify errors: {json.dumps(data['errors'])}")
    return data["data"]


def get_order_for_draft(draft_id):
    """Given a draft's numeric id, return the linked Order's fulfillment state,
    or None if the draft has no completed order yet."""
    q = """
    query DraftOrder($id: ID!) {
      draftOrder(id: $id) {
        id
        name
        order {
          id
          name
          tags
          displayFulfillmentStatus
          fulfillmentOrders(first: 10) {
            edges {
              node {
                id
                status
                fulfillmentHolds { id reason }
                lineItems(first: 50) { edges { node { id remainingQuantity } } }
              }
            }
          }
        }
      }
    }
    """
    gid = f"gid://shopify/DraftOrder/{draft_id}"
    d = shopify_gql(q, {"id": gid}).get("draftOrder")
    if not d or not d.get("order"):
        return None
    o = d["order"]
    fos = [e["node"] for e in o["fulfillmentOrders"]["edges"]]
    return {
        "id": o["id"],
        "name": o["name"],
        "tags": o.get("tags") or [],
        "fulfillment_status": o.get("displayFulfillmentStatus") or "",
        "fulfillment_orders": fos,
    }


def order_tags_add(order_gid, tags):
    if DRY_RUN:
        log(f"    DRY_RUN: would add tags {tags} to {order_gid}")
        return
    m = """
    mutation Tag($id: ID!, $tags: [String!]!) {
      tagsAdd(id: $id, tags: $tags) { userErrors { field message } }
    }
    """
    errs = shopify_gql(m, {"id": order_gid, "tags": tags})["tagsAdd"]["userErrors"]
    if errs:
        raise RuntimeError(f"tagsAdd failed: {errs}")


def order_tags_remove(order_gid, tags):
    if DRY_RUN:
        log(f"    DRY_RUN: would remove tags {tags} from {order_gid}")
        return
    m = """
    mutation Untag($id: ID!, $tags: [String!]!) {
      tagsRemove(id: $id, tags: $tags) { userErrors { field message } }
    }
    """
    errs = shopify_gql(m, {"id": order_gid, "tags": tags})["tagsRemove"]["userErrors"]
    if errs:
        raise RuntimeError(f"tagsRemove failed: {errs}")


def hold_fulfillment_order(fo_id):
    if DRY_RUN:
        log(f"    DRY_RUN: would HOLD fulfillment order {fo_id}")
        return
    m = """
    mutation Hold($id: ID!, $hold: FulfillmentOrderHoldInput!) {
      fulfillmentOrderHold(id: $id, fulfillmentHold: $hold) {
        fulfillmentOrder { id status }
        userErrors { field message }
      }
    }
    """
    hold = {
        "reason": "OTHER",
        "reasonNotes": "In shop for service (MTS work order in progress)",
        "handle": HOLD_HANDLE,
        "externalId": HOLD_EXTERNAL_ID,
        "notifyMerchant": False,
    }
    errs = shopify_gql(m, {"id": fo_id, "hold": hold})["fulfillmentOrderHold"]["userErrors"]
    if errs:
        raise RuntimeError(f"hold failed: {errs}")


def release_fulfillment_order(fo_id, hold_ids):
    if DRY_RUN:
        log(f"    DRY_RUN: would RELEASE holds {hold_ids} on {fo_id}")
        return
    m = """
    mutation Release($id: ID!, $holdIds: [ID!]) {
      fulfillmentOrderReleaseHold(id: $id, holdIds: $holdIds) {
        fulfillmentOrder { id status }
        userErrors { field message }
      }
    }
    """
    errs = shopify_gql(m, {"id": fo_id, "holdIds": hold_ids})[
        "fulfillmentOrderReleaseHold"]["userErrors"]
    if errs:
        raise RuntimeError(f"release failed: {errs}")


def fulfill_fulfillment_order(fo_id):
    if DRY_RUN:
        log(f"    DRY_RUN: would FULFILL fulfillment order {fo_id}")
        return
    m = """
    mutation Fulfill($f: FulfillmentInput!) {
      fulfillmentCreate(fulfillment: $f) {
        fulfillment { id status }
        userErrors { field message }
      }
    }
    """
    f = {
        "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": fo_id}],
        "notifyCustomer": False,
    }
    errs = shopify_gql(m, {"f": f})["fulfillmentCreate"]["userErrors"]
    if errs:
        raise RuntimeError(f"fulfill failed: {errs}")


# --------------------------- decisions ---------------------------
def is_ship_in(job_type):
    return "ship-in" in job_type.lower()


def is_walk_in(job_type):
    return "walk-in" in job_type.lower()


def is_done(status):
    return status in DONE_STATUSES


def process_ship_in(item, order):
    """Hold before QC; release + retag at QC Finished."""
    order_gid = order["id"]
    tags = order["tags"]
    fos = order["fulfillment_orders"]
    # Fulfillment orders we can still act on (open / in progress / on hold).
    actionable = [fo for fo in fos if fo["status"] in ("OPEN", "IN_PROGRESS", "ON_HOLD", "SCHEDULED")]

    if is_done(item["status"]):
        # Release any of OUR holds and hand off to shipping.
        released = False
        for fo in actionable:
            hold_ids = [h["id"] for h in (fo.get("fulfillmentHolds") or [])]
            if hold_ids:
                release_fulfillment_order(fo["id"], hold_ids)
                released = True
        if released or TAG_HOLD in tags:
            if TAG_HOLD in tags:
                order_tags_remove(order_gid, [TAG_HOLD])
            if TAG_READY not in tags:
                order_tags_add(order_gid, [TAG_READY])
            return f"released ship-in hold -> ready-to-ship ({order['name']})"
        return None
    else:
        # Pre-QC: ensure a hold is in place.
        if TAG_HOLD in tags:
            return None  # already held (idempotent)
        held = False
        for fo in actionable:
            already = bool(fo.get("fulfillmentHolds"))
            if fo["status"] in ("OPEN", "SCHEDULED") and not already:
                hold_fulfillment_order(fo["id"])
                held = True
        if held:
            order_tags_add(order_gid, [TAG_HOLD])
            return f"placed ship-in hold ({order['name']})"
        return None


def process_walk_in(item, order):
    """At QC Finished, auto-fulfill (in-person pickup, nothing ships)."""
    if not is_done(item["status"]):
        return None
    order_gid = order["id"]
    tags = order["tags"]
    if TAG_FULFILLED in tags:
        return None
    if (order["fulfillment_status"] or "").upper() == "FULFILLED":
        # Already fulfilled by hand — just record it so we stop checking.
        order_tags_add(order_gid, [TAG_FULFILLED])
        return None
    fos = order["fulfillment_orders"]
    fulfilled = False
    for fo in fos:
        # Only fulfill open FOs that still have unfulfilled quantity.
        if fo["status"] in ("OPEN", "IN_PROGRESS"):
            remaining = sum(e["node"]["remainingQuantity"]
                            for e in fo["lineItems"]["edges"])
            if remaining > 0:
                fulfill_fulfillment_order(fo["id"])
                fulfilled = True
    if fulfilled:
        order_tags_add(order_gid, [TAG_FULFILLED])
        return f"auto-fulfilled walk-in ({order['name']})"
    return None


# --------------------------- main ---------------------------
def main():
    missing = [k for k, v in {
        "SHOPIFY_STORE_DOMAIN": SHOPIFY_DOMAIN,
        "SHOPIFY_ACCESS_TOKEN": SHOPIFY_TOKEN,
        "MONDAY_API_TOKEN": MONDAY_TOKEN,
    }.items() if not v]
    if missing:
        die(f"Missing env vars: {', '.join(missing)}")

    mode = "DRY_RUN" if DRY_RUN else "LIVE"
    log(f"Fulfillment fork starting ({mode}). Board {MONDAY_BOARD_ID}.")

    try:
        items = fetch_board_items()
    except Exception as e:
        die(f"Could not read monday board: {e}")

    log(f"{len(items)} board item(s) with a linked draft.")
    acted = skipped = no_order = failed = 0

    for it in items:
        job = it["job_type"]
        if not (is_ship_in(job) or is_walk_in(job)):
            skipped += 1
            continue
        try:
            order = get_order_for_draft(it["draft_id"])
            if not order:
                no_order += 1  # still an unpaid draft — nothing to fulfill/hold
                continue
            if is_ship_in(job):
                result = process_ship_in(it, order)
            else:
                result = process_walk_in(it, order)
            if result:
                log(f"  ✓ {it['name']}: {result}")
                acted += 1
            else:
                skipped += 1
        except Exception as e:
            log(f"  ✗ {it['name']} FAILED: {e}")
            failed += 1

    log(f"Done. Acted {acted}, unchanged {skipped}, no-order-yet {no_order}, failed {failed}.")
    if failed:
        sys.exit(2)


if __name__ == "__main__":
    main()
