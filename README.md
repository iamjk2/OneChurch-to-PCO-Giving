# OneChurch Transactions → Planning Center Online Giving Importer

A Python script that reads a **OneChurch** giving export (CSV) and imports the transactions into **Planning Center Online (PCO)** via the Giving API v2.

---

## Features

- Matches donors to existing PCO People records by name + email before creating new ones
- Handles name suffixes (`Jr`, `Sr`, `II`, `III`, `IV`, etc.) — stored in the PCO `suffix` field
- Supports **split designations** — e.g. `Tithe & Offering ($150.00), Missions ($50.00)`
- Supports **anonymous donations** (blank Person field → PCO "Anonymous Donor")
- Maps OneChurch fund names to PCO fund names with a configurable lookup table
- Routes unrecognised funds to a configurable fallback fund (default: `OCS`)
- Maps OneChurch payment methods (`Check`, `Cash`, `Credit Card`, `ACH`, etc.) to PCO values
- Merges OneChurch `Description` and `Notes` fields into a PCO admin-only Note
- Tags every donation with a PCO Payment Source (default: `OneChurch`)
- Skips non-importable rows by Status (`refunded`, `failed`, `cancelled`, `voided`, `declined`, `pending`)
- Skips **non-deductible donations** (`Deductible` ≠ `Yes`) — PCO Giving only supports tax-deductible gifts
- Skips zero and negative amounts with a warning, listing them in the final summary
- Handles multiple date formats from OneChurch exports (`0:00`, `12:00:00 AM`, date-only, etc.)
- Rate-limit back-off with automatic retry
- `--dry-run` mode — parses and validates the CSV without writing anything to PCO
- `--leave-batch-open` mode — imports donations but leaves the PCO batch uncommitted for review
- Detailed run summary showing imported, skipped, and errored transactions by ID

---

## Prerequisites

- Python 3.10+
- `requests` library

```bash
pip install requests
```

---

## PCO Setup

Before running the script, make sure the following exist in your PCO Giving account:

1. **Funds** — create all funds referenced in your OneChurch export, including an `OCS` fund for unmatched designations (Giving → Settings → Funds)
2. **Payment Source** — create a `OneChurch` payment source (Giving → Settings → Payment Sources), or the script will create one automatically
3. **API Token** — create a Personal Access Token at https://api.planningcenteronline.com/personal_access_tokens. The user account used to generate the token must be a **Giving Administrator** in PCO

> **Note:** Organization-level admin access alone is not sufficient — the user must be explicitly granted Administrator or Manager access within the Giving module (Giving → Settings → People & Permissions).

---

## Usage

### Dry run (recommended first step)
Parses the CSV and shows what would be imported — nothing is written to PCO.

```bash
python pco_giving_import.py \
  --file export.csv \
  --client-id YOUR_CLIENT_ID \
  --secret YOUR_SECRET \
  --dry-run
```

### Live import
```bash
python pco_giving_import.py \
  --file export.csv \
  --client-id YOUR_CLIENT_ID \
  --secret YOUR_SECRET
```

### Leave batch open for review
Imports donations into PCO but does not commit the batch, allowing you to review in PCO Giving before committing.

```bash
python pco_giving_import.py \
  --file export.csv \
  --client-id YOUR_CLIENT_ID \
  --secret YOUR_SECRET \
  --leave-batch-open
```

### All options

| Flag | Default | Description |
|---|---|---|
| `--file` | *(required)* | Path to the OneChurch CSV export |
| `--client-id` | *(required)* | PCO Personal Access Token Client ID |
| `--secret` | *(required)* | PCO Personal Access Token Secret |
| `--payment-source-name` | `OneChurch` | Name of the PCO Payment Source to tag donations with |
| `--payment-source-id` | | Use an existing PCO Payment Source ID (skips auto-create/lookup) |
| `--leave-batch-open` | | Don't commit the batch after importing |
| `--dry-run` | | Validate without writing to PCO |

---

## OneChurch CSV Column Mapping

| OneChurch Column | PCO Destination | Notes |
|---|---|---|
| `Person` | People — `first_name`, `last_name`, `suffix` | Searches for existing record first; creates if not found. Blank = Anonymous Donor. Suffixes (`Jr`, `II`, etc.) are split out automatically |
| `Email` | People — `Email` record | Used to confirm person match; saved on new records |
| `Date` | Donation — `received_at` | Handles `6/24/2026 0:00`, `6/24/2026 12:00:00 AM`, date-only, and other common formats |
| `Paid` | Designation — `amount_cents` | Net dollar amount; zero and negative amounts are skipped with a warning |
| `Designations` | Designation — `Fund` | Supports single and split designations. Mapped via `FUND_NAME_MAP`; unmatched funds route to `OCS` |
| `Payment Method` | Donation — `payment_method` | Mapped to PCO values: `check`, `cash`, `card`, `ach` |
| `Check Number` | Donation — `payment_check_number` | Only included when present |
| `Description` + `Notes` | Donation — admin Note | Combined into a single PCO Note (internal/admin only); joined with ` \| ` when both present |
| `Status` | Import filter | Rows with status `refunded`, `failed`, `cancelled`, `voided`, `declined`, or `pending` are skipped |
| `Deductible` | Import filter | Only rows where `Deductible` = `Yes` are imported; all others are skipped and listed in the summary |
| `Transaction ID` | Log output only | Used for tracing errors back to the OneChurch record |

---

## Fund Name Mapping

Edit `FUND_NAME_MAP` near the top of the script to match your OneChurch fund names to your PCO fund names:

```python
FUND_NAME_MAP = {
    "children's ministry": "Childrens Ministry",
    "youth ministry":      "Youth Ministry",
    "missions":            "Outreach",
    "building fund":       "Building Program",
    "special offerings":   "Special Offerings",
    "love offering":       "Love Offering",
    "tithe & offering":    "Tithe & Offering",
}
```

Any fund name not in this map that also doesn't exist in PCO will be routed to the fallback fund:

```python
FALLBACK_FUND = "OCS"
```

---

## Payment Method Mapping

Edit `PAYMENT_METHOD_MAP` to add any payment type values your OneChurch export uses:

```python
PAYMENT_METHOD_MAP = {
    "cash":          "cash",
    "check":         "check",
    "credit card":   "card",
    "ach":           "ach",
    # ... add more as needed
}
```

---

## Name Suffix Handling

The script automatically detects and strips common suffixes from the end of a full name before splitting first and last name. Suffixes are stored in the PCO `suffix` field on the Person record.

Recognised suffixes (defined in `NAME_SUFFIXES` near the top of the script):

`Jr`, `Jr.`, `Sr`, `Sr.`, `II`, `III`, `IV`, `V`, `Esq`, `Esq.`

Examples:

| OneChurch `Person` | PCO `first_name` | PCO `last_name` | PCO `suffix` |
|---|---|---|---|
| `John Smith` | `John` | `Smith` | |
| `John Smith Jr` | `John` | `Smith` | `Jr` |
| `John Smith III` | `John` | `Smith` | `III` |
| `Mary Jo Johnson Sr.` | `Mary Jo` | `Johnson` | `Sr.` |

Add to `NAME_SUFFIXES` if your data contains suffixes not in the list.

---

## Example Summary Output

```
══════════════ Summary ══════════════
  Imported  : 8
  Skipped   : 4 (non-Paid status): ['8414', '8413', '8412', '8411']
  Skipped   : 1 (non-deductible): ['8415']
  Skipped   : 2 (amount issue):
    Txn 8405: $0.00 — zero amount
    Txn 8406: -$50.00 — negative amount — handle refund manually in PCO
  Errors    : 1
  Error details:
    Txn 8407: Cannot parse date: 'invalid'
══════════════════════════════════════
```

---

## Known Limitations

- **Refunds** — negative amounts are skipped; refunds must be entered manually in PCO Giving
- **Soft Credits** — the OneChurch `Soft Credits` column is not imported
- **Recurring schedules** — schedule/recurring giving data is not imported
- **Multi-fund split amounts** — amounts are taken from the Designations field (e.g. `Fund ($100.00)`) not the `Paid` column when a split is detected
- **Memo field** — PCO does not support writing the memo/donor-facing note field for API-imported donations; `Description` and `Notes` are stored as an internal admin Note instead

---

## Tested Against

- OneChurch export format for all Transaction Columns as of June 2026
- Planning Center Online Giving API v2 (`2019-10-18`)
- Python 3.12 / 3.14
