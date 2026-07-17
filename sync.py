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
import re
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

# The board's canonical "Car or Shocks?" labels. The Shopify metafield sometimes
# comes through with different casing (e.g. "Ship-In" vs the board's "Ship-in"),
# which — with create_labels_if_missing=false — makes monday reject the WHOLE
# item. So we normalize the metafield value to the board label case-insensitively.
CAR_OR_SHOCKS_LABELS = [
    "Car (Walk-in)",
    "Set of Shocks (Walk-in)",
    "Partial Set of Shocks (Walk-in)",
    "Set of Shocks (Ship-in)",
    "Partial Set of Shocks (Ship-in)",
]
_COS_BY_LOWER = {lbl.lower(): lbl for lbl in CAR_OR_SHOCKS_LABELS}


def canonical_car_or_shocks(value):
    """Map a raw car_or_shocks_ value to the board's exact label, or None."""
    if not value:
        return None
    return _COS_BY_LOWER.get(str(value).strip().lower())


def order_admin_url(order_legacy_id):
    return f"https://admin.shopify.com/store/{SHOP_ADMIN_HANDLE}/orders/{order_legacy_id}"


def draft_admin_url(draft_legacy_id):
    return f"https://admin.shopify.com/store/{SHOP_ADMIN_HANDLE}/draft_orders/{draft_legacy_id}"


def shopify_link_value(draft):
    """The Shopify Link column value: point at the ORDER once the draft has been
    paid/completed into one, otherwise the draft."""
    order = draft.get("order") or {}
    if order.get("legacyResourceId"):
        return {"url": order_admin_url(order["legacyResourceId"]),
                "text": f"Open Order {order.get('name') or ''}".strip()}
    return {"url": draft_admin_url(draft["legacyResourceId"]),
            "text": f"Open Draft {draft['name']}"}


def parse_shopify_link(value):
    """Return (kind, numeric_id) from a monday link column JSON value.
    kind is 'draft', 'order', or None."""
    if not value:
        return (None, None)
    try:
        url = json.loads(value).get("url", "")
    except Exception:
        return (None, None)
    m = re.search(r"/draft_orders/(\d+)", url)
    if m:
        return ("draft", m.group(1))
    m = re.search(r"/orders/(\d+)", url)
    if m:
        return ("order", m.group(1))
    return (None, None)

COL_TURNAROUND = "timerange_mm52a7b2"  # Turn Around Time
COL_SVC_NOTES = "text3"            # Service Writer Notes
COL_HOURS = "numeric_mm0mfy8z"     # Hours (REQUIRED column — must be set on create)

# Shopify custom metafield the service writer fills in with the job's hours.
# Its value is pushed to the monday "Hours" column so the two stay aligned.
MF_HOURS = "job_hours_"

# --- Powdercoat -------------------------------------------------------------
# custom.powdercoat is a free-text metafield; ANY non-empty value flags the
# work order as a powdercoat job. Its text (often the powder color) is carried
# into the powdercoat card's notes.
MF_POWDERCOAT = "powdercoat"
COL_POWDERCOAT = "color_mm5bzr35"    # Shop board: Powdercoat status (None / Powdercoat Needed)

# MTS Powdercoat Jobs board (7932059042) — the powdercoater's board.
PC_BOARD_ID = "7932059042"
PC_GROUP_NEW = "group_title"         # "New Orders" group
PC_COL_STATUS = "status"             # powder status
PC_COL_NOTES = "text__1"             # "Notes" — holds the custom.powdercoat text
PC_COL_ORDER_NO = "text7__1"         # "Order #" — WO# (plain text, so it's filterable)
PC_COL_DRAFT_LINK = "link_mm5b171t"  # "Draft Order" — link back to the Shopify draft

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
            order { id legacyResourceId name tags }
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

    same_day = str(mf.get("same_day_", "")).lower() == "true"
    car_or_shocks = canonical_car_or_shocks(mf.get("car_or_shocks_"))
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
        COL_LINK: shopify_link_value(draft),
        COL_STATUS: {"label": "Same Day (Not Built)" if same_day else "New"},
        COL_HOURS: hours_val,  # from custom.job_hours_; 0 if unset
        # Powdercoat flag: "Powdercoat Needed" if the metafield is filled, else "None".
        COL_POWDERCOAT: {"label": "Powdercoat Needed"
                         if str(mf.get(MF_POWDERCOAT, "")).strip() else "None"},
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


def fetch_existing_wo_numbers():
    """Return the set of WO#s (e.g. '#D8303') already on the monday board.

    This is the authoritative dedup: the board itself is the source of truth for
    'already synced'. Relying only on the Shopify 'synced-to-monday' tag is not
    enough — that tag can lag behind writes (Shopify draft search is eventually
    consistent) or get cleared when a draft is edited, either of which would
    otherwise create a duplicate board item.
    """
    query = """
    query BoardWOs($board: ID!, $cursor: String) {
      boards(ids: [$board]) {
        items_page(limit: 100, cursor: $cursor) {
          cursor
          items { column_values(ids: ["text_mm5athg0"]) { id text } }
        }
      }
    }
    """
    wos = set()
    cursor = None
    while True:
        data = monday_gql(query, {"board": MONDAY_BOARD_ID, "cursor": cursor})
        page = data["boards"][0]["items_page"]
        for it in page["items"]:
            for c in it["column_values"]:
                if c["id"] == "text_mm5athg0" and (c.get("text") or "").strip():
                    wos.add(c["text"].strip())
        cursor = page.get("cursor")
        if not cursor:
            break
    return wos


# ---------------------------------------------------------------------------
# Powdercoat: flag the shop item + create a card on the powdercoater's board
# ---------------------------------------------------------------------------
def is_powdercoat(draft):
    return bool(str(draft["_mf"].get(MF_POWDERCOAT, "")).strip())


def handle_powdercoat(draft):
    """If this is a powdercoat job, create a task on the MTS Powdercoat Jobs
    board with a link back to the Shopify draft order and the custom.powdercoat
    text as the notes. Best-effort — the caller wraps this so a failure here
    never blocks or duplicates the main work-order sync. The shop-side flag
    (None / Powdercoat Needed) is set on the work-order item itself, in
    build_column_values, so it is always in sync."""
    if not is_powdercoat(draft):
        return
    mf = draft["_mf"]
    name = item_name_for(draft)
    legacy_id = draft["legacyResourceId"]
    draft_link = (f"https://admin.shopify.com/store/{SHOP_ADMIN_HANDLE}"
                  f"/draft_orders/{legacy_id}")
    powder_txt = str(mf.get(MF_POWDERCOAT, "")).strip()

    cols = {
        PC_COL_STATUS: {"label": "New Orders"},
        PC_COL_NOTES: powder_txt[:1900],
        # WO# in a plain-text column so the powder-status sync can find this card
        # by filtering on it (monday can't filter the link column by text).
        PC_COL_ORDER_NO: draft["name"],
        PC_COL_DRAFT_LINK: {"url": draft_link, "text": f"Draft {draft['name']}"},
    }
    if DRY_RUN:
        log(f"    DRY_RUN: would create powdercoat card '{name}' notes='{powder_txt}'")
        return
    mutation = """
    mutation CreatePC($board: ID!, $group: String!, $name: String!, $cols: JSON!) {
      create_item(board_id: $board, group_id: $group, item_name: $name,
                  column_values: $cols, create_labels_if_missing: false) { id }
    }
    """
    data = monday_gql(mutation, {"board": PC_BOARD_ID, "group": PC_GROUP_NEW,
                                 "name": name, "cols": json.dumps(cols)})
    log(f"    ↳ powdercoat card created for {draft['name']} "
        f"(monday item {data['create_item']['id']})")


# ---------------------------------------------------------------------------
# Update existing items when the Shopify draft/order changes
# ---------------------------------------------------------------------------
# We keep these SHOPIFY-OWNED fields in sync onto the existing board item:
#   name (customer/dealer), Due date, Service Writer Notes, Car or Shocks,
#   Hours (always from job_hours_), Powdercoat (Needed/None only).
# We NEVER touch tech-owned fields (Status, Tech, Tech Notes) and NEVER revert a
# "Powdercoat Ready" set by the powder board.
def set_item_columns(item_id, cols):
    if DRY_RUN:
        log(f"    DRY_RUN: would update item {item_id}: {json.dumps(cols)}")
        return
    mutation = """
    mutation SetCols($board: ID!, $item: ID!, $cols: JSON!) {
      change_multiple_column_values(board_id: $board, item_id: $item,
          column_values: $cols, create_labels_if_missing: false) { id }
    }
    """
    monday_gql(mutation, {"board": MONDAY_BOARD_ID, "item": item_id,
                          "cols": json.dumps(cols)})


def fetch_board_items_full():
    """Board items with the columns we may update + the linked draft id."""
    query = """
    query BoardItems($board: ID!, $cursor: String) {
      boards(ids: [$board]) {
        items_page(limit: 100, cursor: $cursor) {
          cursor
          items {
            id
            name
            column_values(ids: ["date5","text3","color_mm5agxce",
              "numeric_mm0mfy8z","color_mm5bzr35","link_mm5ardz5"]) { id text value }
          }
        }
      }
    }
    """
    items, cursor = [], None
    while True:
        data = monday_gql(query, {"board": MONDAY_BOARD_ID, "cursor": cursor})
        page = data["boards"][0]["items_page"]
        for it in page["items"]:
            cv = {c["id"]: c for c in it["column_values"]}
            link_val = (cv.get("link_mm5ardz5") or {}).get("value")
            kind, ref_id = parse_shopify_link(link_val)
            link_url = ""
            if link_val:
                try:
                    link_url = json.loads(link_val).get("url", "")
                except Exception:
                    link_url = ""
            items.append({
                "item_id": it["id"], "name": it["name"],
                "kind": kind, "ref_id": ref_id, "link_url": link_url,
                "date": (cv.get("date5") or {}).get("text") or "",
                "svc_notes": (cv.get("text3") or {}).get("text") or "",
                "car_or_shocks": (cv.get("color_mm5agxce") or {}).get("text") or "",
                "hours": (cv.get("numeric_mm0mfy8z") or {}).get("text") or "",
                "powdercoat": (cv.get("color_mm5bzr35") or {}).get("text") or "",
            })
        cursor = page.get("cursor")
        if not cursor:
            break
    return items


def _entity_from_node(node, order):
    mf = {e["node"]["key"]: e["node"]["value"]
          for e in node["metafields"]["edges"]
          if e["node"]["namespace"] == "custom"}
    return {"name": node["name"], "email": node.get("email"),
            "customer": node.get("customer"), "_mf": mf, "order": order}


def fetch_entities(items):
    """Batch-fetch current Shopify data for board items, keyed by (kind, id).
    Both drafts and orders carry the same custom metafields; draft entities also
    carry their linked `order` (once paid) so the link can be upgraded."""
    draft_ids = sorted({it["ref_id"] for it in items if it["kind"] == "draft" and it["ref_id"]})
    order_ids = sorted({it["ref_id"] for it in items if it["kind"] == "order" and it["ref_id"]})
    result = {}

    draft_q = """
    query Drafts($ids: [ID!]!) {
      nodes(ids: $ids) {
        ... on DraftOrder {
          legacyResourceId name email
          customer { displayName }
          order { legacyResourceId name }
          metafields(first: 30) { edges { node { namespace key value } } }
        }
      }
    }
    """
    for i in range(0, len(draft_ids), 50):
        gids = [f"gid://shopify/DraftOrder/{d}" for d in draft_ids[i:i + 50]]
        data = shopify_gql(draft_q, {"ids": gids})
        for node in data.get("nodes", []):
            if node:
                result[("draft", node["legacyResourceId"])] = \
                    _entity_from_node(node, node.get("order"))

    order_q = """
    query Orders($ids: [ID!]!) {
      nodes(ids: $ids) {
        ... on Order {
          legacyResourceId name email
          customer { displayName }
          metafields(first: 30) { edges { node { namespace key value } } }
        }
      }
    }
    """
    for i in range(0, len(order_ids), 50):
        gids = [f"gid://shopify/Order/{d}" for d in order_ids[i:i + 50]]
        data = shopify_gql(order_q, {"ids": gids})
        for node in data.get("nodes", []):
            if node:
                result[("order", node["legacyResourceId"])] = _entity_from_node(node, None)
    return result


def _hours_str(mf):
    raw = mf.get(MF_HOURS)
    try:
        return str(float(raw)) if raw not in (None, "") else "0"
    except (TypeError, ValueError):
        return "0"


def diff_shopify_fields(item, entity):
    """Return {column_id: value} of Shopify-owned fields that changed on `item`."""
    mf = entity["_mf"]
    changes = {}

    desired_name = item_name_for(entity)
    if desired_name and desired_name != item["name"]:
        changes["name"] = desired_name

    # Upgrade the Shopify Link from the draft to the ORDER once it's been paid.
    if item["kind"] == "draft":
        order = entity.get("order") or {}
        if order.get("legacyResourceId"):
            order_url = order_admin_url(order["legacyResourceId"])
            if item["link_url"] != order_url:
                changes[COL_LINK] = {"url": order_url,
                                     "text": f"Open Order {order.get('name') or ''}".strip()}

    due = mf.get("due_date")
    if due and due != item["date"]:
        changes[COL_DUE] = {"date": due}

    svc = (mf.get("service_notes") or "")[:1900]
    if svc != item["svc_notes"]:
        changes[COL_SVC_NOTES] = svc

    cos = canonical_car_or_shocks(mf.get("car_or_shocks_"))
    if cos and cos != item["car_or_shocks"]:
        changes[COL_CAR_OR_SHOCKS] = {"label": cos}

    # Hours: always sync from Shopify. Compare as floats to avoid churn (4 vs 4.0).
    desired_hours = _hours_str(mf)
    try:
        same_hours = item["hours"] not in (None, "") and \
            float(item["hours"]) == float(desired_hours)
    except (TypeError, ValueError):
        same_hours = False
    if not same_hours:
        changes[COL_HOURS] = desired_hours

    # Powdercoat: sync Needed/None, but never revert a "Powdercoat Ready".
    if item["powdercoat"] != "Powdercoat Ready":
        desired_pc = "Powdercoat Needed" if str(mf.get(MF_POWDERCOAT, "")).strip() else "None"
        if desired_pc != item["powdercoat"]:
            changes[COL_POWDERCOAT] = {"label": desired_pc}

    return changes


def update_existing_items():
    """Push Shopify-owned field changes onto existing board items."""
    items = fetch_board_items_full()
    if not items:
        return 0
    entities = fetch_entities(items)
    updated = 0
    for it in items:
        if not it["kind"] or not it["ref_id"]:
            continue
        ent = entities.get((it["kind"], it["ref_id"]))
        if not ent:
            continue  # draft/order deleted or not found
        changes = diff_shopify_fields(it, ent)
        if not changes:
            continue
        try:
            set_item_columns(it["item_id"], changes)
            log(f"  ~ {ent['name']} updated: {', '.join(changes.keys())}")
            updated += 1
        except Exception as e:
            log(f"  ✗ update {ent.get('name')} FAILED: {e}")
    return updated


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

    created, skipped, failed = 0, 0, 0

    if not drafts:
        log("No un-synced shop_car_ drafts found — skipping create pass.")

    # Authoritative dedup: which WO#s are already on the board. Guards against
    # tag lag / tags cleared on edit (either of which would else duplicate).
    existing_wos = set()
    if drafts:
        try:
            existing_wos = fetch_existing_wo_numbers()
        except Exception as e:
            die(f"Could not read monday board for dedup: {e}")
        log(f"Found {len(drafts)} candidate draft(s); "
            f"{len(existing_wos)} WO(s) already on board.")

    for draft in drafts:
        wo = draft["name"]
        try:
            if wo in existing_wos:
                # Already on the board — never create a second item. Just make
                # sure the draft is (re)tagged so it stops showing up as un-synced.
                mark_synced(draft)
                log(f"  = {wo} already on board — skipped create, re-tagged")
                skipped += 1
                continue
            item_id = create_monday_item(draft)
            existing_wos.add(wo)  # guard against same-run repeats
            # Only mark synced AFTER the item is created — so a crash retries,
            # never duplicates.
            mark_synced(draft)
            log(f"  ✓ {wo} -> monday item {item_id} (synced)")
            created += 1
            # Powdercoat handling is best-effort: it runs after the WO is safely
            # synced, so a powder-side failure never duplicates the shop item.
            try:
                handle_powdercoat(draft)
            except Exception as e:
                log(f"    ⚠ {wo}: powdercoat step failed (WO still synced): {e}")
        except Exception as e:
            log(f"  ✗ {wo} FAILED: {e}")
            failed += 1

    log(f"Done creating. Created {created}, skipped {skipped}, failed {failed}.")

    # Second pass: push Shopify-owned field changes onto existing board items.
    try:
        updated = update_existing_items()
        log(f"Done updating. {updated} existing item(s) refreshed from Shopify.")
    except Exception as e:
        log(f"Update pass failed: {e}")

    if failed:
        # Non-zero exit so the host's cron surfaces the failure in logs/alerts.
        sys.exit(2)


if __name__ == "__main__":
    main()
