"""
update_feed.py
================
רץ כל שעה (ראו .github/workflows/update.yml). אוסף מידע אמיתי משלושה
סוגי מקורות, מתייג רמת אמינות, וכותב docs/feed.json.

תיקון מרכזי מהגרסה הקודמת:
  פידי ה-RSS של gov.il אינם בכתובת קבועה שאפשר לנחש — לכל משרד יש
  GUID ייחודי. הפונקציה discover_gov_feed_url() קודם קוראת את דף
  הנחיתה הרשמי (.../Departments/{slug}/RSS), שולפת ממנו את כתובת
  ה-API האמיתית עם ה-GUID (בדיוק כמו שדפדפן אנושי היה עושה), ורק
  אז מביאה את הפיד עצמו. אין GUID מנוחש בקוד.

עמידות: אם מקור נכשל בסבב מסוים, אנחנו *לא* מוחקים את הדאטה הטוב
מהסבב הקודם — משמרים אותו ומסמנים leg_status="stale_fallback",
כדי שמעקב הבריאות (health_check.py) יוכל להתריע בלי שהאתר ייראה ריק.
"""

import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

KNESSET_BASE = "https://knesset.gov.il/Odata/ParliamentInfo.svc"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LegisRadarBot/1.0; +https://github.com/)"
}

GOV_MINISTRIES = [
    "ministry_of_justice",
    "ministry_of_finance",
    "ministry_of_interior",
    "prime-ministers-office",
]

KEYWORDS = [
    "הצעת חוק", "חוק יסוד", "ועדת חוקה", "ועדת הכנסת",
    "רגולציה", "תקנות", "קריאה ראשונה", "קריאה שנייה",
    "היועץ המשפטי לממשלה", "ביקורת המדינה",
]

OUTLET_TIER = {
    "ynet": 2, "הארץ": 2, "כאן": 2, "ישראל היום": 2, "וואלה": 2,
    "גלובס": 2, "מעריב": 2, "n12": 2, "כלכליסט": 2,
    "המכון הישראלי לדמוקרטיה": 3, "the marker": 2,
}


def fetch_knesset_bills(limit=25):
    url = f"{KNESSET_BASE}/KNS_Bill"
    params = {"$format": "json", "$orderby": "LastUpdatedDate desc", "$top": str(limit)}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        rows = resp.json().get("value", [])
    except Exception as exc:
        print(f"[warn] Knesset OData נכשל: {exc}")
        return None

    items = []
    for r in rows:
        items.append({
            "id": f"kns-{r.get('BillID')}",
            "title": r.get("Name", "ללא שם"),
            "stage": r.get("SubTypeDesc") or "בתהליך",
            "tier": 1,
            "source_type": "knesset",
            "outlet": "אתר הכנסת (OData רשמי)",
            "url": f"https://main.knesset.gov.il/Activity/Legislation/Laws/Pages/LawBill.aspx?t=lawsuggestionssearch&lawitemid={r.get('BillID')}",
            "updated": r.get("LastUpdatedDate", ""),
        })
    return items


def discover_gov_feed_url(ministry_slug, channel="NewsApi"):
    landing_url = f"https://www.gov.il/he/Departments/{ministry_slug}/RSS"
    try:
        resp = requests.get(landing_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        match = re.search(rf"/he/api/{channel}/rss/([0-9a-fA-F-]{{36}})", resp.text)
        if not match:
            print(f"[warn] לא נמצא GUID עבור {ministry_slug}/{channel}")
            return None
        return f"https://www.gov.il/he/api/{channel}/rss/{match.group(1)}"
    except Exception as exc:
        print(f"[warn] כשל בגילוי פיד עבור {ministry_slug}: {exc}")
        return None


def fetch_gov_rss():
    items = []
    any_success = False
    for slug in GOV_MINISTRIES:
        feed_url = discover_gov_feed_url(slug, channel="NewsApi")
        if not feed_url:
            continue
        try:
            resp = requests.get(feed_url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            any_success = True
            for item in root.findall(".//item")[:10]:
                title = (item.findtext("title") or "").strip()
                if not any(k in title for k in KEYWORDS):
                    continue
                items.append({
                    "id": f"gov-{abs(hash(title))}",
                    "title": title,
                    "stage": "הודעת משרד ממשלתי",
                    "tier": 1,
                    "source_type": "gov",
                    "outlet": slug,
                    "url": item.findtext("link") or feed_url,
                    "updated": item.findtext("pubDate") or "",
                })
        except Exception as exc:
            print(f"[warn] gov.il RSS נכשל עבור {slug}: {exc}")

    return items if any_success else None


def fetch_news_rss(max_items=20):
    items = []
    any_success = False
    for kw in KEYWORDS[:5]:
        q = urllib.parse.quote(kw)
        url = f"https://news.google.com/rss/search?q={q}&hl=iw&gl=IL&ceid=IL:iw"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            any_success = True
            for item in root.findall(".//item")[:6]:
                title = (item.findtext("title") or "").strip()
                source_el = item.find("source")
                outlet = source_el.text if source_el is not None else "לא ידוע"
                tier = 3
                for known, t in OUTLET_TIER.items():
                    if known.lower() in (outlet or "").lower():
                        tier = t
                        break
                items.append({
                    "id": f"news-{abs(hash(title))}",
                    "title": title,
                    "stage": "סיקור תקשורתי",
                    "tier": tier,
                    "source_type": "news",
                    "outlet": outlet,
                    "url": item.findtext("link") or url,
                    "updated": item.findtext("pubDate") or "",
                })
        except Exception as exc:
            print(f"[warn] Google News RSS נכשל עבור '{kw}': {exc}")

    if not any_success:
        return None

    seen, unique = set(), []
    for it in items:
        key = it["title"][:40]
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    return unique[:max_items]


def load_previous_feed():
    try:
        with open("docs/feed.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"knesset_bills": [], "gov_announcements": [], "news_feed": []}


def main():
    prev = load_previous_feed()
    leg_status = {}

    print("שולף מהכנסת (Tier 1)...")
    knesset_items = fetch_knesset_bills()
    if knesset_items is None:
        knesset_items = prev.get("knesset_bills", [])
        leg_status["knesset"] = "stale_fallback"
        print(f"  -> נכשל, נשמר דאטה קודם ({len(knesset_items)} פריטים)")
    else:
        leg_status["knesset"] = "ok"
        print(f"  -> {len(knesset_items)} פריטים")

    print("שולף ממשרדי ממשלה (Tier 1)...")
    gov_items = fetch_gov_rss()
    if gov_items is None:
        gov_items = prev.get("gov_announcements", [])
        leg_status["gov"] = "stale_fallback"
        print(f"  -> נכשל, נשמר דאטה קודם ({len(gov_items)} פריטים)")
    else:
        leg_status["gov"] = "ok"
        print(f"  -> {len(gov_items)} פריטים")

    print("שולף מהתקשורת (Tier 2/3)...")
    news_items = fetch_news_rss()
    if news_items is None:
        news_items = prev.get("news_feed", [])
        leg_status["news"] = "stale_fallback"
        print(f"  -> נכשל, נשמר דאטה קודם ({len(news_items)} פריטים)")
    else:
        leg_status["news"] = "ok"
        print(f"  -> {len(news_items)} פריטים")

    feed = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "leg_status": leg_status,
        "counts": {
            "knesset": len(knesset_items),
            "gov": len(gov_items),
            "news": len(news_items),
        },
        "knesset_bills": knesset_items,
        "gov_announcements": gov_items,
        "news_feed": news_items,
    }

    with open("docs/feed.json", "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)

    print(f"\nנכתב ל-docs/feed.json. leg_status = {leg_status}")


if __name__ == "__main__":
    main()
