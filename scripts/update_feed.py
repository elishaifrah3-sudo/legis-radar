"""
update_feed.py
================
מריץ אותו GitHub Actions כל כמה שעות (ראו .github/workflows/update.yml).
אוסף מידע אמיתי משלושה סוגי מקורות, מתייג רמת אמינות, וכותב docs/feed.json —
הקובץ שהדשבורד (docs/index.html) קורא כדי להיות "חי".

מקורות:
  Tier 1 - רשמי:     Knesset OData (KNS_Bill) + פידי RSS של משרדי ממשלה ב-gov.il
  Tier 2/3 - תקשורת: Google News RSS, מסונן למילות מפתח חקיקתיות,
                      עם מיפוי שם-מקור -> Tier לפי רשימת עיתונים מוכרת.

לא דורש מפתחות API. כל המקורות ציבוריים וחופשיים.
"""

import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

KNESSET_BASE = "https://knesset.gov.il/Odata/ParliamentInfo.svc"

# רשימת פידי RSS רשמיים של משרדי ממשלה (gov.il). אפשר להוסיף עוד —
# כל משרד באתר gov.il חושף פיד תחת .../RSS
GOV_RSS_FEEDS = {
    "משרד המשפטים": "https://www.gov.il/he/Departments/ministry_of_justice/RSS",
    "משרד האוצר": "https://www.gov.il/he/Departments/ministry_of_finance/RSS",
    "משרד הפנים": "https://www.gov.il/he/Departments/ministry_of_interior/RSS",
}

# מילות מפתח לסינון חדשות רלוונטיות לחקיקה/רגולציה
KEYWORDS = [
    "הצעת חוק", "חוק יסוד", "ועדת חוקה", "ועדת הכנסת",
    "רגולציה", "תקנות", "קריאה ראשונה", "קריאה שנייה",
    "היועץ המשפטי לממשלה", "ביקורת המדינה",
]

# מיפוי שם-מקור (כפי שמופיע ב-Google News) -> Tier אמינות
OUTLET_TIER = {
    "ynet": 2, "הארץ": 2, "כאן": 2, "ישראל היום": 2, "וואלה": 2,
    "גלובס": 2, "מעריב": 2, "N12": 2, "כלכליסט": 2, "הכנסת": 1,
    "המכון הישראלי לדמוקרטיה": 3, "the marker": 2,
}


def fetch_knesset_bills(limit=25):
    """שולף את הצעות החוק שעודכנו לאחרונה — Tier 1, המקור הכי אמין שיש."""
    url = f"{KNESSET_BASE}/KNS_Bill"
    params = {
        "$format": "json",
        "$orderby": "LastUpdatedDate desc",
        "$top": str(limit),
    }
    try:
        resp = requests.get(url, params=params, timeout=25)
        resp.raise_for_status()
        rows = resp.json().get("value", [])
    except Exception as exc:
        print(f"[warn] Knesset OData נכשל: {exc}")
        return []

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


def fetch_gov_rss():
    """שולף הודעות ממשרדי ממשלה ישירות מ-gov.il — גם Tier 1."""
    items = []
    for ministry, feed_url in GOV_RSS_FEEDS.items():
        try:
            resp = requests.get(feed_url, timeout=20)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item")[:8]:
                title = (item.findtext("title") or "").strip()
                if not any(k in title for k in KEYWORDS):
                    continue
                items.append({
                    "id": f"gov-{abs(hash(title))}",
                    "title": title,
                    "stage": "הודעת משרד ממשלתי",
                    "tier": 1,
                    "source_type": "gov",
                    "outlet": ministry,
                    "url": item.findtext("link") or feed_url,
                    "updated": item.findtext("pubDate") or "",
                })
        except Exception as exc:
            print(f"[warn] gov.il RSS נכשל עבור {ministry}: {exc}")
    return items


def fetch_news_rss(max_items=20):
    """Google News RSS מסונן למילות מפתח חקיקתיות — Tier 2/3 לפי המקור."""
    items = []
    for kw in KEYWORDS[:5]:  # מגבילים כדי לא להעמיס
        q = urllib.parse.quote(kw)
        url = f"https://news.google.com/rss/search?q={q}&hl=iw&gl=IL&ceid=IL:iw"
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item")[:6]:
                title = (item.findtext("title") or "").strip()
                source_el = item.find("source")
                outlet = source_el.text if source_el is not None else "לא ידוע"
                tier = OUTLET_TIER.get(outlet.strip().lower(), 3) if outlet else 3
                # תיקון: OUTLET_TIER מוגדר עם מפתחות עבריים/אנגליים לא אחידים — השוואה גמישה
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

    # דה-דופליקציה לפי כותרת
    seen = set()
    unique = []
    for it in items:
        key = it["title"][:40]
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    return unique[:max_items]


def main():
    print("שולף מהכנסת (Tier 1)...")
    knesset_items = fetch_knesset_bills()
    print(f"  -> {len(knesset_items)} פריטים")

    print("שולף ממשרדי ממשלה (Tier 1)...")
    gov_items = fetch_gov_rss()
    print(f"  -> {len(gov_items)} פריטים")

    print("שולף מהתקשורת (Tier 2/3)...")
    news_items = fetch_news_rss()
    print(f"  -> {len(news_items)} פריטים")

    feed = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
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

    print("\nנכתב ל-docs/feed.json בהצלחה.")


if __name__ == "__main__":
    main()
