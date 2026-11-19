"""Incremental Knesset OData monitor. Only labels changes 'new' with a prior snapshot."""
import json, os, re, time, hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin
import requests

OUT = Path("docs/odata_snapshot.json")
V4 = "https://knesset.gov.il/OdataV4/ParliamentInfo/"
V3 = "https://knesset.gov.il/Odata/ParliamentInfo.svc/"
ENTITIES = {
    "bills": "KNS_Bill",
    "agendas": "KNS_Agenda",
    "questions": "KNS_Query",
    "committee_sessions": "KNS_CommitteeSession",
    "committee_agenda": "KNS_CommitteeSessionAgenda",
    "positions": "KNS_PersonToPosition",
}
MAX_ROWS = 1200
PAGE_SIZE = 100
HEADERS = {"User-Agent": "LegisRadar-DataMonitor/2.0", "Accept": "application/json"}
session = requests.Session()
session.headers.update(HEADERS)

def fetch_entity(entity):
    errors = []
    for base, version in ((V4, "v4"), (V3, "v3")):
        for ordered in (True, False):
            records, next_url, pages = [], base + entity, 0
            params = {"$format": "json", "$top": PAGE_SIZE}
            if ordered:
                params["$orderby"] = "LastUpdatedDate desc"
            try:
                while next_url and pages < MAX_ROWS // PAGE_SIZE:
                    resp = session.get(next_url, params=params if pages == 0 else None, timeout=35)
                    resp.raise_for_status()
                    payload = resp.json()
                    if isinstance(payload, dict) and "value" in payload:
                        chunk = payload["value"]
                        next_url = payload.get("@odata.nextLink")
                    elif isinstance(payload, dict) and "d" in payload:
                        chunk = payload["d"].get("results", [])
                        next_url = payload["d"].get("__next")
                    else:
                        raise ValueError("Unrecognized JSON format")
                    if not isinstance(chunk, list):
                        raise ValueError("No rows list")
                    records.extend(chunk)
                    pages += 1
                    if not next_url or not chunk:
                        next_url = None
                    if next_url and not next_url.startswith("http"):
                        next_url = urljoin(base, next_url)
                return records, {"status": "ok", "version": version, "ordered_by_last_update": ordered,
                                 "count_sampled": len(records), "truncated": bool(next_url),
                                 "limit": MAX_ROWS}
            except Exception as exc:
                errors.append(f"{version}/{('ordered' if ordered else 'unordered')}: {str(exc)[:150]}")
                time.sleep(0.25)
    return None, {"status": "error", "errors": errors}

def scrub(value):
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()
                if k not in ("__metadata",) and not k.startswith("@odata.")}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value

def record_id(row):
    for key in ("Id", "BillID", "AgendaID", "QueryID", "CommitteeSessionID", "CommitteeSessionAgendaID", "PersonToPositionID"):
        if row.get(key) is not None:
            return str(row[key])
    return None

def fingerprint(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()

def is_business_bill(row):
    text = str(row.get("Name", "")).strip()
    tokens = ["עסק", "מסחר", "קמעונ", "יבוא", "ייצוא", "יצוא", "מכס", "מס הכנסה", "מע״מ",
              "מס ערך מוסף", "מסים", "מיסוי", "תקציב", "רגולצ", "תחרות", "צרכנ",
              "עבודה", "עובד", "מעסיק", "שכר", "ביטוח", "בנק", "אשראי", "פיננס",
              "תעשיי", "אנרגי", "חשמל", "דלק", "תחבור", "תעופ", "נמל", "תשתית",
              "תכנון והבני", "רישוי", "טכנולוג", "סייבר", "תקשורת", "שידור",
              "מזון", "מחיר", "חקלא", "רשות המס", "חברה", "תאגיד", "ארנונה",
              "פטור", "מכרז", "אסדרה", "ייבוא"]
    return any(word in text for word in tokens)

def bill_url(row):
    ident = row.get("BillID") or row.get("Id")
    return f"https://main.knesset.gov.il/Activity/Legislation/Laws/Pages/LawBill.aspx?t=lawsuggestionssearch&lawitemid={ident}" if ident else None

def main():
    now = datetime.now(timezone.utc).isoformat()
    try:
        previous = json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    has_baseline = bool(previous.get("entities"))
    entities, coverage, changes = {}, {}, {}
    for logical, entity in ENTITIES.items():
        rows, state = fetch_entity(entity)
        coverage[logical] = state
        if rows is None:
            # Preserve previous data, explicitly mark stale. Never infer 'no changes' from failure.
            entities[logical] = previous.get("entities", {}).get(logical, {})
            changes[logical] = {"status": "unavailable", "added": [], "changed": []}
            continue
        current = {}
        for original in rows:
            row = scrub(original)
            ident = record_id(row)
            if ident:
                current[ident] = {"hash": fingerprint(row), "record": row}
        old = previous.get("entities", {}).get(logical, {})
        added = [x["record"] for ident, x in current.items() if ident not in old] if has_baseline else []
        changed = [x["record"] for ident, x in current.items() if ident in old and x["hash"] != old[ident]["hash"]] if has_baseline else []
        # An ID appearing in a limited rolling window is not necessarily first-ever created.
        changes[logical] = {
            "status": "comparison_available" if has_baseline else "baseline_initialized",
            "added_to_sample_not_necessarily_new": added[:500],
            "modified_records": changed[:500],
            "added_count": len(added), "modified_count": len(changed),
        }
        entities[logical] = current
        print(logical, state["status"], len(current), "added in sampled window", len(added), "modified", len(changed))
    candidates = changes.get("bills", {}).get("added_to_sample_not_necessarily_new", [])
    business = [dict(row, source_url=bill_url(row)) for row in candidates if is_business_bill(row)]
    report = {
        "generated_at": now, "first_snapshot": not has_baseline,
        "source": "Knesset OData V4 where reachable; legacy OData V3 fallback",
        "coverage": coverage,
        "changes": changes,
        "business_bills_added_to_sample": business,
        "warning": "This is a bounded sample. New-to-sample IDs and LastUpdatedDate do NOT prove legislative creation or full coverage; verify CreationDate/KnessetNum and source before reporting as new."
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"generated_at": now, "entities": entities, "coverage": coverage}, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("docs/odata_changes.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
