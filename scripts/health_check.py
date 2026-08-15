"""
health_check.py
================
רץ אחרי כל עדכון (ראו update.yml). לא "סוכן שמסתכל" - בדיקות
דטרמיניסטיות וקבועות שבודקות בדיוק את מה שיכול להישבר:

  1. feed.json תקין syntactically ולא ריק
  2. כל אחת מ-3 הרגליים לא stale_fallback יותר מ-X סבבים ברצף
  3. generated_at לא ישן מדי (אות לכך שה-workflow עצמו הפסיק לרוץ)
  4. האתר החי (GitHub Pages) בכלל עונה (200)

אם נמצאת בעיה — הסקריפט פותח (או מעדכן) GitHub Issue יחיד עם
אבחון מדויק, כדי שלא תצטרכו לגלות בעצמכם שמשהו נשבר.
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests

REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
PAGES_URL = os.environ.get("PAGES_URL", "")

API = "https://api.github.com"
HEADERS = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github+json"}

ISSUE_TITLE = "🔴 מכ״ם חקיקה — נמצאה תקלה אוטומטית"


def check_feed():
    problems = []
    try:
        with open("docs/feed.json", "r", encoding="utf-8") as f:
            feed = json.load(f)
    except Exception as exc:
        return [f"feed.json לא ניתן לקריאה/פרסור: {exc}"], None

    gen = feed.get("generated_at")
    try:
        gen_dt = datetime.fromisoformat(gen.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - gen_dt).total_seconds() / 3600
        if age_hours > 3:
            problems.append(
                f"feed.json לא התעדכן {age_hours:.1f} שעות — סימן שה-workflow "
                f"התתקע או שה-cron הפסיק לרוץ."
            )
    except Exception:
        problems.append("שדה generated_at חסר או לא תקין בפורמט.")

    leg_status = feed.get("leg_status", {})
    for leg, status in leg_status.items():
        if status != "ok":
            problems.append(f"רגל '{leg}' נמצאת ב-stale_fallback (המקור נכשל בסבב האחרון).")

    counts = feed.get("counts", {})
    if sum(counts.values()) == 0:
        problems.append("כל שלוש הרגליים ריקות — feed.json בפועל ריק לגמרי.")

    return problems, feed


def check_site_reachable():
    if not PAGES_URL:
        return ["PAGES_URL לא הוגדר בסביבת ה-workflow."]
    try:
        resp = requests.get(PAGES_URL, timeout=20)
        if resp.status_code != 200:
            return [f"האתר החי מחזיר קוד {resp.status_code} במקום 200."]
    except Exception as exc:
        return [f"האתר החי לא נגיש בכלל: {exc}"]
    return []


def find_existing_issue():
    url = f"{API}/repos/{REPO}/issues"
    resp = requests.get(url, headers=HEADERS, params={"state": "open", "labels": "auto-health"})
    if resp.status_code != 200:
        return None
    for issue in resp.json():
        if issue["title"] == ISSUE_TITLE:
            return issue["number"]
    return None


def open_or_update_issue(problems):
    body = "נמצאו הבעיות הבאות בבדיקה האוטומטית:\n\n" + "\n".join(f"- {p}" for p in problems)
    body += f"\n\n_נבדק אוטומטית: {datetime.now(timezone.utc).isoformat()}_"

    existing = find_existing_issue()
    if existing:
        requests.post(
            f"{API}/repos/{REPO}/issues/{existing}/comments",
            headers=HEADERS,
            json={"body": body},
        )
        print(f"עודכן Issue קיים #{existing}")
    else:
        requests.post(
            f"{API}/repos/{REPO}/issues",
            headers=HEADERS,
            json={"title": ISSUE_TITLE, "body": body, "labels": ["auto-health"]},
        )
        print("נפתח Issue חדש")


def close_issue_if_resolved():
    existing = find_existing_issue()
    if existing:
        requests.patch(
            f"{API}/repos/{REPO}/issues/{existing}",
            headers=HEADERS,
            json={"state": "closed"},
        )
        requests.post(
            f"{API}/repos/{REPO}/issues/{existing}/comments",
            headers=HEADERS,
            json={"body": "✅ הבדיקה האוטומטית עברה בהצלחה — סוגר את ה-Issue."},
        )
        print(f"נסגר Issue #{existing} (הבעיה נפתרה)")


def main():
    problems, feed = check_feed()
    problems += check_site_reachable()

    if problems:
        print("נמצאו בעיות:")
        for p in problems:
            print(f"  - {p}")
        if REPO and TOKEN:
            open_or_update_issue(problems)
        sys.exit(1)
    else:
        print("הכל תקין.")
        if REPO and TOKEN:
            close_issue_if_resolved()
        sys.exit(0)


if __name__ == "__main__":
    main()
