#!/usr/bin/env python3
"""
OneChurch → Planning Center Online Giving Importer
====================================================
Reads a OneChurch CSV export and imports giving transactions
into Planning Center Online (PCO) via their Giving API v2.

Tested against the OneChurch export format with columns:
  Transaction ID, Date, Person, Email, Designations,
  Paid, Payment Method, Check Number, Batch, Status, Source, ...

Prerequisites:
  pip install requests

Usage:
  # Preview only — nothing written to PCO
  python pco_giving_import.py --file export.csv \\
      --client-id YOUR_CLIENT_ID --secret YOUR_SECRET --dry-run

  # Live import
  python pco_giving_import.py --file export.csv \\
      --client-id YOUR_CLIENT_ID --secret YOUR_SECRET

  # Leave the PCO batch open for review before committing
  python pco_giving_import.py --file export.csv \\
      --client-id YOUR_CLIENT_ID --secret YOUR_SECRET --leave-batch-open

Get your Client ID + Secret at:
  https://api.planningcenter.com/oauth/applications  (Personal Access Tokens)
"""

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime, date
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

# ---------------------------------------------------------------------------
# PCO API base URLs
# ---------------------------------------------------------------------------
GIVING_URL = "https://api.planningcenteronline.com/giving/v2"
PEOPLE_URL = "https://api.planningcenteronline.com/people/v2"

# ---------------------------------------------------------------------------
# OneChurch → PCO payment method mapping
# Extend as needed if your export contains other values.
# ---------------------------------------------------------------------------
PAYMENT_METHOD_MAP = {
    "cash":          "cash",
    "check":         "check",
    "cheque":        "check",
    "credit card":   "card",
    "credit":        "card",
    "debit card":    "card",
    "debit":         "card",
    "card":          "card",
    "ach":           "ach",
    "eft":           "ach",
    "online":        "ach",
    "bank transfer": "ach",
    "e-check":       "ach",
}

# ---------------------------------------------------------------------------
# OneChurch fund name → PCO fund name mapping
# Left side  = value as it appears in the OneChurch "Designations" column
# Right side = exact fund name in Planning Center Online
# Anything not listed here is routed to the FALLBACK_FUND below.
# ---------------------------------------------------------------------------
FUND_NAME_MAP = {
    "children's ministry": "Childrens Ministry",
    "youth ministry":      "Youth Ministry",
    "missions":            "Outreach",
    "building fund":       "Building Program",
    "special offerings":   "Special Offerings",
    "love offering":       "Love Offering",
    # Tithe & Offering passes through unchanged — add it if the PCO name differs
    "tithe & offering":    "Tithe & Offering",
}

# Fund name used when no mapping exists and PCO has no fund with that name
FALLBACK_FUND = "OCS"

# Statuses to skip (won't be imported)
SKIP_STATUSES = {"refunded", "failed", "cancelled", "voided", "declined", "pending"}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PCO API client
# ---------------------------------------------------------------------------
class PCOClient:
    """Thin wrapper around the PCO REST/JSON:API with rate-limit back-off."""

    def __init__(self, client_id: str, secret: str, dry_run: bool = False):
        self.auth    = HTTPBasicAuth(client_id, secret)
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/vnd.api+json",
            "Accept":       "application/vnd.api+json",
            "User-Agent":   "OneChurch PCO Giving Importer (https://github.com)",
        })

    # ---- internal --------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs) -> dict:
        for attempt in range(6):
            resp = self.session.request(method, url, auth=self.auth, **kwargs)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 10))
                log.warning("Rate limited — waiting %ss…", wait)
                time.sleep(wait)
                continue
            try:
                resp.raise_for_status()
            except requests.HTTPError:
                log.error("HTTP %s  %s", resp.status_code, url)
                log.error("Request headers : %s", dict(resp.request.headers))
                log.error("Response body   : %s", resp.text[:1000])
                raise
            return resp.json() if resp.content else {}
        raise RuntimeError(f"Exceeded retry limit for {url}")

    def get(self, url: str, params: dict = None) -> dict:
        return self._request("GET", url, params=params)

    def post(self, url: str, payload: dict) -> dict:
        if self.dry_run:
            log.info("[DRY-RUN] POST %s\n%s", url, json.dumps(payload, indent=2))
            return {"data": {"id": "DRY-RUN-ID", "attributes": {}}}
        return self._request("POST", url, json=payload)

    def patch(self, url: str, payload: dict) -> dict:
        if self.dry_run:
            log.info("[DRY-RUN] PATCH %s", url)
            return {}
        return self._request("PATCH", url, json=payload)

    # ---- People ----------------------------------------------------------

    def find_person(self, first: str, last: str, email: str = None) -> Optional[str]:
        """Search PCO People; return ID if found, else None."""
        params = {
            "where[first_name]": first,
            "where[last_name]":  last,
            "per_page": 5,
        }
        data    = self.get(f"{PEOPLE_URL}/people", params=params)
        results = data.get("data", [])

        if not results:
            return None

        # Prefer an exact email match when we have one
        if email:
            for p in results:
                email_data = self.get(
                    f"{PEOPLE_URL}/people/{p['id']}/emails"
                ).get("data", [])
                addrs = [e["attributes"]["address"].lower() for e in email_data]
                if email.lower() in addrs:
                    log.debug("Person matched by email: %s %s (id=%s)", first, last, p["id"])
                    return p["id"]

        # Fall back to first name match
        log.debug("Person matched by name: %s %s (id=%s)", first, last, results[0]["id"])
        return results[0]["id"]

    def create_person(self, first: str, last: str,
                      email: str = None, suffix: str = None) -> str:
        attrs = {"first_name": first, "last_name": last}
        if suffix:
            attrs["suffix"] = suffix
        payload = {
            "data": {
                "type": "Person",
                "attributes": attrs,
            }
        }
        pid = self.post(f"{PEOPLE_URL}/people", payload)["data"]["id"]
        if email and not self.dry_run:
            try:
                self.post(f"{PEOPLE_URL}/people/{pid}/emails", {
                    "data": {
                        "type": "Email",
                        "attributes": {
                            "address":  email,
                            "location": "Home",
                            "primary":  True,
                        }
                    }
                })
            except Exception:
                log.warning("Could not attach email %s to person %s", email, pid)
        return pid

    def get_or_create_person(self, first: str, last: str,
                             email: str = None, suffix: str = None) -> str:
        pid = self.find_person(first, last, email)
        if pid:
            return pid
        suffix_label = f" {suffix}" if suffix else ""
        log.info("  → Creating new PCO person: %s %s%s", first, last, suffix_label)
        return self.create_person(first, last, email, suffix)

    # ---- Payment Sources -------------------------------------------------

    def get_or_create_payment_source(self, name: str) -> str:
        sources = self.get(f"{GIVING_URL}/payment_sources").get("data", [])
        for s in sources:
            if s["attributes"]["name"].lower() == name.lower():
                log.info("Using existing payment source '%s' (id=%s)", name, s["id"])
                return s["id"]
        log.info("Creating payment source '%s'", name)
        resp = self.post(f"{GIVING_URL}/payment_sources", {
            "data": {"type": "PaymentSource", "attributes": {"name": name}}
        })
        return resp["data"]["id"]

    # ---- Funds -----------------------------------------------------------

    def _load_funds(self) -> dict:
        """Return {lower_name: id} for all funds in the PCO org."""
        funds = self.get(f"{GIVING_URL}/funds").get("data", [])
        return {f["attributes"]["name"].lower(): f["id"] for f in funds}

    def get_default_fund_id(self) -> str:
        data = self.get(f"{GIVING_URL}/funds", params={"where[default]": "true"})
        items = data.get("data", [])
        if items:
            return items[0]["id"]
        all_funds = self.get(f"{GIVING_URL}/funds").get("data", [])
        if all_funds:
            return all_funds[0]["id"]
        raise RuntimeError("No funds found in PCO Giving. Create at least one fund first.")

    # ---- Batches ---------------------------------------------------------

    def create_batch(self, description: str) -> str:
        resp = self.post(f"{GIVING_URL}/batches", {
            "data": {
                "type": "Batch",
                "attributes": {"description": description},
            }
        })
        bid = resp["data"]["id"]
        log.info("Created PCO batch '%s' (id=%s)", description, bid)
        return bid

    def commit_batch(self, batch_id: str):
        self.patch(f"{GIVING_URL}/batches/{batch_id}", {
            "data": {
                "type": "Batch",
                "id":   batch_id,
                "attributes": {"status": "committed"},
            }
        })
        log.info("Committed batch %s", batch_id)

    # ---- Donations -------------------------------------------------------

    def create_note(self, donation_id: str, body: str):
        """Attach an admin-only note to a donation (POST after donation creation)."""
        if not body:
            return
        self.post(f"{GIVING_URL}/donations/{donation_id}/note", {
            "data": {
                "type": "Note",
                "attributes": {"body": body},
            }
        })

    def create_donation(self, *, batch_id: str, person_id: str = None,
                        payment_source_id: str, payment_method: str,
                        received_at: str, designations: list,
                        check_number: str = None,
                        note: str = None) -> str:
        """
        designations: list of {'amount_cents': int, 'fund_id': str}
        person_id: omit (None) for anonymous donations — PCO will show "Anonymous Donor"
        """
        attrs: dict = {
            "received_at":    received_at,
            "payment_method": payment_method,
        }
        if check_number:
            attrs["payment_check_number"] = check_number

        payload = {
            "data": {
                "type": "Donation",
                "attributes": attrs,
                "relationships": {
                    "batch":          {"data": {"type": "Batch",         "id": batch_id}},
                    **( {"person": {"data": {"type": "Person", "id": person_id}}} if person_id else {} ),
                    "payment_source": {"data": {"type": "PaymentSource", "id": payment_source_id}},
                },
            },
            "included": [
                {
                    "type": "Designation",
                    "attributes": {"amount_cents": d["amount_cents"]},
                    "relationships": {
                        "fund": {"data": {"type": "Fund", "id": d["fund_id"]}}
                    },
                }
                for d in designations
            ],
        }
        resp = self.post(f"{GIVING_URL}/batches/{batch_id}/donations", payload)
        donation_id = resp["data"]["id"]
        if note:
            self.create_note(donation_id, note)
        return donation_id


# ---------------------------------------------------------------------------
# CSV helpers — tuned to the OneChurch export format
# ---------------------------------------------------------------------------

# Suffixes stripped from the end of a name before splitting first/last.
# Add to this set if your data contains others.
NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v", "esq", "esq."}


def split_full_name(full_name: str) -> tuple[str, str, str]:
    """
    Split a full name into (first, last, suffix).

    'John Smith'         → ('John',    'Smith',   '')
    'John Smith Jr'      → ('John',    'Smith',   'Jr')
    'John Smith III'     → ('John',    'Smith',   'III')
    'Mary Jo Johnson'    → ('Mary Jo', 'Johnson', '')
    'Mary Jo Johnson Sr' → ('Mary Jo', 'Johnson', 'Sr')
    'Smith'              → ('',        'Smith',   '')
    """
    parts = full_name.strip().split()
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return "", parts[0], ""

    # Strip trailing suffix if present (only when at least 3 words remain with it)
    suffix = ""
    if len(parts) > 2 and parts[-1].lower() in NAME_SUFFIXES:
        suffix = parts[-1]
        parts  = parts[:-1]

    last  = parts[-1]
    first = " ".join(parts[:-1])
    return first, last, suffix


def parse_amount_cents(raw: str) -> int:
    """'$50.00 ' → 5000,  '$1,234.56' → 123456"""
    cleaned = raw.strip().lstrip("$").replace(",", "").strip()
    return round(float(cleaned) * 100)


def parse_designations(raw: str, resolve_fund_fn) -> list[dict]:
    """
    Parse the OneChurch Designations field into a list of
    {'amount_cents': int, 'fund_id': str} dicts.

    Handles two formats:
      Single : "Tithe & Offering"
      Split  : "Tithe & Offering ($150.00), Missions ($50.00)"
    """
    import re
    raw = raw.strip()
    # Detect split format: contains a parenthesised dollar amount
    parts = re.findall(r'([^,(]+?)\s*\(\$([0-9,]+\.\d{2})\)', raw)
    if parts:
        return [
            {
                "amount_cents": round(float(amt.replace(",", "")) * 100),
                "fund_id":      resolve_fund_fn(name.strip().rstrip(",")),
            }
            for name, amt in parts
        ]
    # Single designation — amount comes from the Paid column, handled by caller
    return [{"fund_name": raw, "fund_id": resolve_fund_fn(raw)}]


def parse_date(raw: str) -> str:
    """'6/24/2026 0:00' → '2026-06-24T00:00:00Z'"""
    raw = raw.strip()
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: '{raw}'")


def map_payment_method(raw: str) -> str:
    method = PAYMENT_METHOD_MAP.get(raw.strip().lower())
    if method:
        return method
    log.warning("  Unknown payment method '%s' — defaulting to 'cash'", raw)
    return "cash"


def read_csv(filepath: str) -> tuple[list, list]:
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows    = list(reader)
    return headers, rows


# ---------------------------------------------------------------------------
# Main import
# ---------------------------------------------------------------------------

def run_import(args):
    log.info("=== OneChurch → PCO Giving Importer ===")
    if args.dry_run:
        log.info("*** DRY RUN — nothing will be written to PCO ***")

    client = PCOClient(args.client_id, args.secret, dry_run=args.dry_run)

    # Payment source
    ps_name = args.payment_source_name or "OneChurch"
    if args.payment_source_id:
        payment_source_id = args.payment_source_id
        log.info("Using payment source id: %s", payment_source_id)
    else:
        payment_source_id = client.get_or_create_payment_source(ps_name)
    log.info("Payment source id: %s", payment_source_id)

    # Funds — load all PCO funds once, then resolve via mapping
    pco_funds = client._load_funds()   # {lower_name: id}
    log.info("Loaded %d funds from PCO: %s",
             len(pco_funds), [n.title() for n in pco_funds])

    # Ensure the OCS fallback fund actually exists in PCO
    if FALLBACK_FUND.lower() not in pco_funds:
        log.warning("Fallback fund '%s' not found in PCO — unmapped funds will error. "
                    "Create a '%s' fund in PCO Giving before running live.",
                    FALLBACK_FUND, FALLBACK_FUND)

    def resolve_fund(ocs_name: str) -> str:
        """
        1. Translate the OneChurch fund name via FUND_NAME_MAP.
        2. Look up the translated (or original) name in PCO funds.
        3. Fall back to FALLBACK_FUND ('OCS') if nothing matches.
        """
        key = ocs_name.strip().lower()

        # Step 1: translate
        pco_name = FUND_NAME_MAP.get(key, ocs_name.strip())

        # Step 2: look up in PCO
        if pco_name.lower() in pco_funds:
            return pco_funds[pco_name.lower()]

        # Step 3: OCS fallback
        log.warning("  Fund '%s' (mapped→'%s') not in PCO — routing to '%s'",
                    ocs_name, pco_name, FALLBACK_FUND)
        if FALLBACK_FUND.lower() in pco_funds:
            return pco_funds[FALLBACK_FUND.lower()]

        raise ValueError(
            f"Fund '{ocs_name}' could not be mapped and fallback '{FALLBACK_FUND}' "
            f"does not exist in PCO. Create the '{FALLBACK_FUND}' fund first."
        )

    # Read CSV
    headers, rows = read_csv(args.file)
    log.info("Read %d transaction rows from %s", len(rows), args.file)

    # Filter to Paid only (skip refunds, failed, etc.)
    importable = []
    skipped_status = []
    for r in rows:
        status = r.get("Status", "").strip().lower()
        if status in SKIP_STATUSES:
            skipped_status.append(r.get("Transaction ID", "?"))
        else:
            importable.append(r)

    if skipped_status:
        log.info("Skipping %d non-Paid transactions: %s",
                 len(skipped_status), skipped_status)

    # Filter out non-deductible donations — PCO Giving only supports tax-deductible gifts
    non_deductible = [r.get("Transaction ID", "?") for r in importable
                      if r.get("Deductible", "").strip().lower() != "yes"]
    importable = [r for r in importable
                  if r.get("Deductible", "").strip().lower() == "yes"]
    if non_deductible:
        log.info("Skipping %d non-deductible transactions: %s",
                 len(non_deductible), non_deductible)

    log.info("Importing %d transactions", len(importable))

    # Create one PCO batch for this import run
    batch_label = f"OneChurch Import – {date.today().isoformat()}"
    batch_id    = client.create_batch(batch_label)

    success, errors, skipped_amount = 0, [], []
    # non_deductible already filtered above; tracked for summary
    skipped_non_deductible = non_deductible

    for row in importable:
        txn_id = row.get("Transaction ID", "?").strip()
        try:
            # ── Person ──────────────────────────────────────────────
            full_name = row.get("Person", "").strip()
            if not full_name:
                # Anonymous donation — omit person relationship in PCO
                person_id = None
                log.info("  Txn %s: blank person — will import as Anonymous Donor", txn_id)
            else:
                first, last, suffix = split_full_name(full_name)
                if not last:
                    raise ValueError(f"Cannot determine name from '{full_name}'")
                email = row.get("Email", "").strip() or None
                person_id = client.get_or_create_person(first, last, email, suffix)

            # ── Amount (used as fallback for single-fund rows) ──────
            total_cents = parse_amount_cents(row["Paid"])
            if total_cents == 0:
                log.warning("Skipping Txn %s — zero amount (%s)", txn_id, row["Paid"])
                skipped_amount.append((txn_id, row["Paid"], "zero amount"))
                continue
            if total_cents < 0:
                log.warning("Skipping Txn %s — negative amount (%s), handle refunds manually in PCO", txn_id, row["Paid"])
                skipped_amount.append((txn_id, row["Paid"], "negative amount — handle refund manually in PCO"))
                continue

            # ── Date ────────────────────────────────────────────────
            received_at = parse_date(row["Date"])

            # ── Designations (single or split) ──────────────────────
            raw_desig = row.get("Designations", "").strip()
            designations = parse_designations(raw_desig, resolve_fund)
            # For single-fund rows, amount comes from Paid column
            if len(designations) == 1 and "amount_cents" not in designations[0]:
                designations[0]["amount_cents"] = total_cents

            # ── Payment method ──────────────────────────────────────
            pm = map_payment_method(row.get("Payment Method", "cash"))

            # ── Check number ────────────────────────────────────────
            check_num   = row.get("Check Number",  "").strip() or None

            # ── Description + Notes → PCO admin Note (internal only) ─
            # memo field is not supported for API-imported donations
            desc = row.get("Description", "").strip()
            nts  = row.get("Notes", "").strip()
            note = " | ".join(filter(None, [desc, nts])) or None

            # ── Create donation ─────────────────────────────────────
            donation_id = client.create_donation(
                batch_id=batch_id,
                person_id=person_id,
                payment_source_id=payment_source_id,
                payment_method=pm,
                received_at=received_at,
                designations=designations,
                check_number=check_num,
                note=note,
            )

            desig_summary = ", ".join(
                f"${d['amount_cents']/100:.2f}" for d in designations
            )
            donor_label = f"{first} {last}".strip() if person_id else "Anonymous Donor"
            log.info("✓  Txn %s | %s | %s (%d designation(s)) → donation %s",
                     txn_id, donor_label, desig_summary, len(designations), donation_id)
            success += 1

        except Exception as exc:
            log.error("✗  Txn %s — %s", txn_id, exc)
            errors.append((txn_id, str(exc)))

    # Commit batch unless asked to leave open
    if success > 0 and not args.leave_batch_open:
        client.commit_batch(batch_id)
    elif success == 0:
        log.warning("No donations imported; batch %s left uncommitted.", batch_id)
    else:
        log.info("Batch %s left open for review. Commit it in PCO Giving when ready.", batch_id)

    # Summary
    log.info("")
    log.info("══════════════ Summary ══════════════")
    log.info("  Imported  : %d", success)
    log.info("  Skipped   : %d (non-Paid status): %s",
             len(skipped_status), skipped_status or "none")
    log.info("  Skipped   : %d (non-deductible): %s",
             len(skipped_non_deductible), skipped_non_deductible or "none")
    log.info("  Skipped   : %d (amount issue):", len(skipped_amount))
    for txn, amt, reason in skipped_amount:
        log.info("    Txn %s: %s — %s", txn, amt, reason)
    log.info("  Errors    : %d", len(errors))
    if errors:
        log.info("  Error details:")
        for txn, msg in errors:
            log.info("    Txn %s: %s", txn, msg)
    if args.dry_run:
        log.info("  (Dry run — nothing was written to PCO)")
    log.info("══════════════════════════════════════")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Import OneChurch giving CSV into Planning Center Online Giving."
    )
    p.add_argument("--file", required=True,
                   help="Path to the OneChurch CSV export")
    p.add_argument("--client-id", required=True,
                   help="PCO Personal Access Token Client ID")
    p.add_argument("--secret", required=True,
                   help="PCO Personal Access Token Secret")
    p.add_argument("--payment-source-id",
                   help="Use an existing PCO payment source ID (skip auto-create)")
    p.add_argument("--payment-source-name", default="OneChurch",
                   help="Name for the payment source if creating one (default: OneChurch)")
    p.add_argument("--leave-batch-open", action="store_true",
                   help="Don't commit the PCO batch — leave it for manual review")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse & validate the CSV without writing anything to PCO")
    args = p.parse_args()
    run_import(args)


if __name__ == "__main__":
    main()
