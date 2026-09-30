#!/usr/bin/env python3
"""Seat watcher and auto-booker for Tokyo's driving-license written test (警視庁 学科試験).

With no booking it books the best open seat. It then live-checks every day that could beat
the current booking and swaps to it (cancel -> book -> fall back) as soon as a seat frees up.
"""
import argparse
import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
BOOKED = HERE / "booked.json"
JST = timezone(timedelta(hours=9))
PLACE_NAMES = {"250": "江東", "270": "府中", "280": "鮫洲"}
BASE = "https://license-test-tokyo-prd-police-pref-api.tokyo-madoguchi-yoyaku.com"
SITE = "https://license-test.tokyo-madoguchi-yoyaku.com"
HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ja,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Content-Type": "application/json; charset=UTF-8",
    "Origin": SITE,
    "Referer": SITE + "/",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "User-Agent": "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Mobile Safari/537.36",
}
C = SimpleNamespace()


def log(msg):
    print(f"{datetime.now(JST):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def die(msg):
    log(f"错误：{msg}")
    sys.exit(1)


def env(name, default=""):
    return os.environ.get(name, default).strip()


def env_date(name):
    value = env(name)
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y%m%d")
        except ValueError:
            pass
    die(f"{name}={value} 格式不对，应为 YYYY-MM-DD")


def configure():
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.strip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip())
    C.course = env("COURSE", "11")
    C.places = [p.strip() for p in env("PLACES", "250,270,280").split(",") if p.strip()]
    C.prefer_pm = env("PREFER_PM", "1") == "1"
    C.auto = env("AUTO_BOOK", "1") == "1"
    C.interval = float(env("REQ_INTERVAL", "3"))
    C.refresh = float(env("CAL_REFRESH", "300"))
    C.date_from, C.date_to = env_date("DATE_FROM"), env_date("DATE_TO")
    if C.date_from and C.date_to and C.date_from > C.date_to:
        die(f"DATE_FROM {C.date_from} 晚于 DATE_TO {C.date_to}")


def today(days=0):
    return (datetime.now(JST) + timedelta(days=days)).strftime("%Y%m%d")


def window():
    """Bookable dates (YYYYMMDD, inclusive): tomorrow in JST onward, clipped to DATE_FROM..DATE_TO."""
    tomorrow = today(1)
    if C.date_to and C.date_to < tomorrow:
        die(f"预约范围已过：DATE_TO={C.date_to}，最早只能约 {tomorrow}")
    return max(C.date_from or tomorrow, tomorrow), C.date_to


def in_window(s, first, last):
    return first <= s["date"] <= (last or "99999999")


def months(first, last):
    """Calendar months to fetch: first..last, or three months when there is no end date."""
    y, m = int(first[:4]), int(first[4:6])
    out = [f"{y}{m:02d}"]
    while (last and out[-1] < last[:6]) or (not last and len(out) < 3):
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append(f"{y}{m:02d}")
    return out


def request(path, payload=None):
    data = payload and json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + path, data, HEADERS), timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except ValueError:
            body = None
        return {**(body if isinstance(body, dict) else {}), "http": e.code}


def to_slot(s, place):
    return {
        "key": f"{place}-{s['date']}-{s['starttime']}",
        "place": place,
        "date": s["date"],
        "start": s["starttime"],
        "end": s["endtime"],
        "free": int(s["capacity"]) - int(s["reservation"]),
    }


def calendar(first, last):
    """All sessions in the window from the month calendars (cached server-side for ~5 min)."""
    out = []
    for month in months(first, last):
        for place in C.places:
            r = request(f"/calgetres?date={month}&coursecode={C.course}&placecode={place}&user=pub")
            if "body" not in r:
                log(f"日历 {month}/{place}: {r}")
                return None
            out += [s for s in (to_slot(x, place) for x in r["body"]) if in_window(s, first, last)]
    return out


def live(place, date):
    """Real-time sessions of one day: (slots, None) or (None, error)."""
    r = request("/getres", {"date": date, "coursecode": C.course, "placecode": place})
    return ([to_slot(x, place) for x in r["body"]], None) if "body" in r else (None, r)


def applicant():
    return {"license": "", **{k: env(f"RES_{k.upper()}") for k in ("name", "phone", "birthday", "gracer_no")}}


def customer(path, action):
    """/customerops action 1: A1101 = no booking, B1101 = has one. /cancel action 3: A1301 = cancelled."""
    a = applicant()
    return request(path, {
        "res_no": "", "license": a["license"], "phone": a["phone"], "birthday": a["birthday"],
        "action": action, "coursecode": C.course, "gracer_no": a["gracer_no"], "name": a["name"],
    })


def put(s):
    """/putres: status OK (A4001) = booked, B4002 = full."""
    return request("/putres", {
        "date": s["date"], "coursecode": C.course, "placecode": s["place"],
        "starttime": s["start"], "endtime": s["end"], **applicant(),
    })


def load_held():
    return json.loads(BOOKED.read_text())["slot"] if BOOKED.exists() else None


def save_held(slot, resp):
    BOOKED.write_text(json.dumps({"slot": slot, "resp": resp}, ensure_ascii=False))


def rank(s):
    """Earlier day first; on the same day follow the PLACES order, then prefer the afternoon (PREFER_PM=1)."""
    t = int(s["start"])
    return s["date"], C.places.index(s["place"]), -t if C.prefer_pm else t


def better(s, held):
    return held is None or rank(s) < rank(held)


def describe(s):
    if not s:
        return "无"
    d, t = s["date"], s["start"]
    return f"[{PLACE_NAMES.get(s['place'], s['place'])}] {d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:]}"


def notify(subject, body):
    if not env("SMTP_USER"):
        return
    msg = MIMEText(f"{body}\n\n{SITE}\n", "plain", "utf-8")
    msg["Subject"] = f"[学科予約] {subject}"
    msg["From"] = env("SMTP_USER")
    msg["To"] = env("MAIL_TO") or env("SMTP_USER")
    try:
        with smtplib.SMTP_SSL(env("SMTP_HOST", "smtp.gmail.com"), int(env("SMTP_PORT", "465")), timeout=30) as smtp:
            smtp.login(env("SMTP_USER"), env("SMTP_PASS"))
            smtp.send_message(msg)
        log(f"邮件已发送：{subject}")
    except Exception as e:
        log(f"邮件失败：{e!r}")


def book(cands, held, pool):
    """Move to the best of `cands` (live-verified free seats).

    Only one booking per person, so the current one is cancelled first. If every candidate
    fails, the old slot is rebooked, else the next best open one. Returns (booked_new, message).
    """
    status = customer("/customerops", "1")
    log(f"状态：{status}")
    has_booking = status.get("code") != "A1101"
    if has_booking and not held:
        msg = "网站上已有预约，但本地没有记录。请在网站取消后重启。"
        notify("⚠️ 已停止", msg)
        die(msg)
    if has_booking:
        r = customer("/cancel", "3")
        log(f"取消：{r}")
        if r.get("code") != "A1301":
            return False, f"取消失败，未改约：{r}"
    fails = []
    for s in cands:
        r = put(s)
        log(f"预约 {s['key']}：{r}")
        if r.get("status") == "OK":
            save_held(s, r)
            return True, f"已预约 {describe(s)}（原 {describe(held)}）\nyoyakuno: {r.get('yoyakuno')}"
        fails.append(f"{s['key']} {r.get('code')}")
    failed = "、".join(fails)
    if not held:
        return False, f"预约失败：{failed}"
    tried = {s["key"] for s in cands} | {held["key"]}
    for s in [held] + [p for p in pool if p["key"] not in tried][:10]:
        r = put(s)
        log(f"重新预约 {s['key']}：{r}")
        if r.get("status") == "OK":
            save_held(s, r)
            return False, f"改约失败（{failed}），已重新预约 {describe(s)}\nyoyakuno: {r.get('yoyakuno')}"
    BOOKED.unlink(missing_ok=True)
    return False, f"改约失败（{failed}），重新预约也失败，现在没有预约！请立刻手动预约。"


def attempt(found, held, pool, mailed):
    """Act on live-verified free seats. Returns True if the booking may have changed."""
    found.sort(key=rank)
    log(f"空位：{[(s['key'], s['free']) for s in found]}")
    if not C.auto:
        new = [s for s in found if s["key"] not in mailed]
        mailed.update(s["key"] for s in new)
        if new:
            notify("有空位", "\n".join(f"{describe(s)}  空位 {s['free']}" for s in new))
        return False
    ok, msg = book(found, held, pool)
    log(msg.replace("\n", " "))
    if ok:
        notify("✅ 已改约" if held else "✅ 已预约", msg)
    elif msg not in mailed:
        mailed.add(msg)
        notify("⚠️ 预约失败", msg)
    return True


def grab(pool, first, last, mailed):
    """No booking yet: book the best seat the calendar shows, confirmed by a live check."""
    for s in pool[:10]:
        slots, err = live(s["place"], s["date"])
        if err:
            break
        free = [x for x in slots if x["free"] > 0 and in_window(x, first, last)]
        if free:
            attempt(free, None, pool, mailed)
            break
    return load_held()


def main():
    parser = argparse.ArgumentParser(description="警視庁 学科試験 空位监控 / 自动预约")
    parser.add_argument("--test-mail", action="store_true", help="发一封测试邮件")
    args = parser.parse_args()
    configure()
    if args.test_mail:
        return notify("测试邮件", "邮件配置正常。")
    missing = [k for k in ("RES_NAME", "RES_PHONE", "RES_BIRTHDAY", "RES_GRACER_NO") if not env(k)]
    if C.auto and missing:
        die(f".env 缺少 {', '.join(missing)}")

    held = load_held()
    if held and held["date"] < today():
        if C.auto:
            log(f"{describe(held)} 已过期，取消：{customer('/cancel', '3')}")
        BOOKED.rename(HERE / f"booked-{held['date']}.json")
    first, last = window()
    log(f"启动：{first}–{last or '不限'}，考场 {','.join(C.places)}，"
        f"每 {C.interval:g}s 实时查 1 天，{'自动预约' if C.auto else '只通知'}")

    targets, pool, refreshed, i, mailed = [], [], 0.0, 0, set()
    while True:
        try:
            first, last = window()
            held = load_held()
            if held and held["date"] < today():
                log(f"{describe(held)} 已过，停止")
                return
            if time.time() - refreshed > C.refresh:
                cal = calendar(first, last)
                if cal is not None:
                    refreshed = time.time()
                    pool = sorted((s for s in cal if s["free"] > 0), key=rank)
                    if held is None and C.auto:
                        held = grab(pool, first, last, mailed)
                    targets = sorted({(s["date"], s["place"]) for s in cal if better(s, held)})
                    i = 0
                    log(f"日历：{len(cal)} 场，{len(targets)} 天可能更好（当前 {describe(held)}）")
            if targets:
                date, place = targets[i % len(targets)]
                i += 1
                slots, err = live(place, date)
                if err:
                    log(f"实时 {place}/{date}：{err}")
                    time.sleep(60 if err.get("http") == 403 else 5)
                    continue
                found = [s for s in slots if s["free"] > 0 and in_window(s, first, last) and better(s, held)]
                if found and attempt(found, held, pool, mailed):
                    refreshed = 0
        except Exception as e:
            log(f"异常：{e!r}")
            time.sleep(5)
        time.sleep(C.interval)


if __name__ == "__main__":
    main()
