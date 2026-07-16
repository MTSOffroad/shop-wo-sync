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
            link_val = (cv.get(COL_LINK) or {}).get("value")
            draft_id = None
            if link_val:
                # link column value is JSON like {"url": ".../draft_orders/123", "text": "..."}
                try:
                    url = json.loads(link_val).get("url", "")
                except Exception:
                    url = ""
                m = re.search(r"/draft_orders/(\d+)", url)
                if m:
                    draft_id = m.group(1)
            if status and draft_id:
                items.append({"name": it["name"], "status": status,
                              "wo": wo, "draft_id": draft_id})
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


def get_draft_note(draft_id):
    q = """
    query GetNote($id: ID!) {
      draftOrder(id: $id) { id name note2 }
    }
    """
    gid = f"gid://shopify/DraftOrder/{draft_id}"
    data = shopify_gql(q, {"id": gid})
    d = data.get("draftOrder")
    if not d:
        return None, None  # draft may have been completed/deleted
    return d.get("name"), (d.get("note2") or "")


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


def prepend_status(draft_id, existing_note, new_status):
    now = datetime.datetime.now()  # host clock; label is cosmetic
    stamp = now.strftime(f"%-m/%-d %H:%M {TZ_LABEL}") if os.name != "nt" \
        else now.strftime(f"%m/%d %H:%M {TZ_LABEL}")
    new_line = f"[{stamp}] {new_status}"
    combined = (new_line + ("\n" + existing_note if existing_note else "")).strip()

    if DRY_RUN:
        log(f"  DRY_RUN: would prepend '{new_line}' to draft {draft_id}")
        return

    m = """
    mutation Append($id: ID!, $input: DraftOrderInput!) {
      draftOrderUpdate(id: $id, input: $input) {
        draftOrder { id }
        userErrors { field message }
      }
    }
    """
    gid = f"gid://shopify/DraftOrder/{draft_id}"
    data = shopify_gql(m, {"id": gid, "input": {"note": combined}})
    errs = data["draftOrderUpdate"]["userErrors"]
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
            name, note = get_draft_note(it["draft_id"])
            if name is None:
                gone += 1
                continue  # draft completed/deleted; status sync no longer applies
            last = top_logged_status(note)
            if last == it["status"]:
                skipped += 1
                continue  # already logged this status; nothing to do
            prepend_status(it["draft_id"], note, it["status"])
            log(f"  ✓ {it['wo'] or it['name']}: logged '{it['status']}'"
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
