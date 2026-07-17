#!/usr/bin/env python3
"""
Status Log Sync  (monday -> Shopify)
------------------------------------
For each work order on the monday "Shop Work Orders" board, keep the linked
Shopify draft order's Note field as an append-only status log:

    [7/16 12:51 MST] Needs QC
    [7/16 12:47 MST] Working on it
    [7/16 12:40 MST] New

Newest line on top. Read-then-append: the script reads the existing Shopify
note, and only prepends a new line when the monday status differs from the
most recent logged status. So running it repeatedly is safe (idempotent) — it
never duplicates a line and never overwrites history.

How it knows the current status vs. the last-logged one:
  - It parses the top line of the existing Shopify note (format above).
  - If the monday item's Status text != that top-line status, it prepends a
    new timestamped line. Otherwise it does nothing.

Designed to run on the same schedule as sync.py (e.g. every 5 min on Render).

Required environment variables (secrets — never in code):
  SHOPIFY_STORE_DOMAIN, SHOPIFY_ACCESS_TOKEN, MONDAY_API_TOKEN
Optional:
  MONDAY_BOARD_ID   default 18422437311
  SHOP_TZ_LABEL     default "MST" (label only, for the log line)
  DRY_RUN           "true" to log without writing
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
TZ_LABEL = os.environ.get("SHOP_TZ_LABEL", "MST").strip()
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

SHOPIFY_API_VERSION = "2025-01"
SHOPIFY_GQL = f"https://{SHOPIFY_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
MONDAY_GQL = "https://api.monday.com/v2"

# Column IDs on Shop Work Orders board (18422437311)
COL_STATUS = "status"
COL_WO = "text_mm5athg0"      # WO # (holds the Shopify draft name, e.g. #D8297)
COL_LINK = "link_mm5ardz5"    # Shopify Link (holds the admin URL w/ the draft id)

# Matches a log line's leading status, e.g. "[7/16 12:51 MST] Needs QC"
LOG_LINE_RE = re.compile(r"^\[[^\]]*\]\s*(.+?)\s*$")


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
    """Return items with status text, WO#, and the linked draft id (parsed from URL)."""
    query = """
    query BoardItems($board: ID!, $cursor: String) {
      boards(ids: [$board]) {
        items_page(limit: 100, cursor: $cursor) {
          cursor
          items {
            id
            name
            column_values(ids: ["status", "text_mm5athg0", "link_mm5ardz5"]) {
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
            wo = (cv.get(COL_WO) or {}).get("text") or ""
            kind, ref_id = parse_shopify_link((cv.get(COL_LINK) or {}).get("value"))
            if status and kind and ref_id:
                items.append({"name": it["name"], "status": status,
                              "wo": wo, "kind": kind, "ref_id": ref_id})
        cursor = page.get("cursor")
        if not cursor:
            break
    return items


def parse_shopify_link(value):
    """Return (kind, numeric_id) from a monday link column JSON value."""
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


def resolve_note(kind, ref_id):
    """Resolve a board item to where its status log should live and its current
    contents. The log follows the entity: while it's a draft it lives on the
    draft note; once paid/converted it lives on the ORDER note (so it shows on
    the order page). Returns (target_kind, target_gid, existing_note, name) or
    (None, None, None, None) if the entity is gone."""
    if kind == "order":
        q = "query GetOrder($id: ID!) { order(id: $id) { id name note } }"
        o = shopify_gql(q, {"id": f"gid://shopify/Order/{ref_id}"}).get("order")
        if not o:
            return (None, None, None, None)
        return ("order", o["id"], o.get("note") or "", o.get("name"))

    q = """
    query GetDraft($id: ID!) {
      draftOrder(id: $id) { id name note2 order { id name note } }
    }
    """
    d = shopify_gql(q, {"id": f"gid://shopify/DraftOrder/{ref_id}"}).get("draftOrder")
    if not d:
        return (None, None, None, None)
    order = d.get("order")
    if order:
        # Log now lives on the order. Carry the draft's history over the first
        # time (when the order note has no log line yet).
        onote = order.get("note") or ""
        existing = onote if top_logged_status(onote) else (d.get("note2") or "")
        return ("order", order["id"], existing, order.get("name") or d.get("name"))
    return ("draft", d["id"], d.get("note2") or "", d.get("name"))


def top_logged_status(note_text):
    """Return the status on the newest (top) log line, or None if no log yet."""
    for line in note_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = LOG_LINE_RE.match(line)
        if m:
            return m.group(1).strip()
        # first non-empty line isn't a log line -> treat as no prior log
        return None
    return None


def write_note(target_kind, target_gid, existing_note, new_status):
    now = datetime.datetime.now()  # host clock; label is cosmetic
    stamp = now.strftime(f"%-m/%-d %H:%M {TZ_LABEL}") if os.name != "nt" \
        else now.strftime(f"%m/%d %H:%M {TZ_LABEL}")
    new_line = f"[{stamp}] {new_status}"
    combined = (new_line + ("\n" + existing_note if existing_note else "")).strip()

    if DRY_RUN:
        log(f"  DRY_RUN: would prepend '{new_line}' to {target_kind} {target_gid}")
        return

    if target_kind == "order":
        m = """
        mutation OrderNote($id: ID!, $note: String) {
          orderUpdate(input: {id: $id, note: $note}) {
            order { id } userErrors { field message }
          }
        }
        """
        errs = shopify_gql(m, {"id": target_gid, "note": combined})["orderUpdate"]["userErrors"]
    else:
        m = """
        mutation DraftNote($id: ID!, $input: DraftOrderInput!) {
          draftOrderUpdate(id: $id, input: $input) {
            draftOrder { id } userErrors { field message }
          }
        }
        """
        errs = shopify_gql(m, {"id": target_gid, "input": {"note": combined}})[
            "draftOrderUpdate"]["userErrors"]
    if errs:
        raise RuntimeError(f"note update failed: {errs}")


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
    log(f"Status log sync starting ({mode}). Board {MONDAY_BOARD_ID}.")

    try:
        items = fetch_board_items()
    except Exception as e:
        die(f"Could not read monday board: {e}")

    log(f"{len(items)} item(s) with a linked draft.")
    updated = skipped = failed = gone = 0

    for it in items:
        try:
            tkind, tgid, note, name = resolve_note(it["kind"], it["ref_id"])
            if tgid is None:
                gone += 1
                continue  # draft/order deleted; status sync no longer applies
            last = top_logged_status(note)
            if last == it["status"]:
                skipped += 1
                continue  # already logged this status; nothing to do
            write_note(tkind, tgid, note, it["status"])
            log(f"  ✓ {it['wo'] or it['name']}: logged '{it['status']}' on {tkind}"
                + (f" (was '{last}')" if last else " (first entry)"))
            updated += 1
        except Exception as e:
            log(f"  ✗ {it['wo'] or it['name']} FAILED: {e}")
            failed += 1

    log(f"Done. Updated {updated}, unchanged {skipped}, draft-gone {gone}, failed {failed}.")
    if failed:
        sys.exit(2)


if __name__ == "__main__":
    main()
