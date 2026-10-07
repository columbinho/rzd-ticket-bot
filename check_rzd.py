#!/usr/bin/env python3
"""Следит за свободными нижними местами на конкретный поезд РЖД и пишет в Telegram.

Источник — API поиска ticket.rzd.ru (тот же, что у сайта), вход не нужен:
  POST /apib2b/p/Railway/V1/Search/TrainPricing → поезда с группами вагонов,
  в каждой группе LowerPlaceQuantity / UpperPlaceQuantity / LowerSidePlaceQuantity / UpperSidePlaceQuantity.
Сертификат ticket.rzd.ru выдан Russian Trusted Root CA (НУЦ Минцифры), его нет в стандартных
хранилищах — бандл лежит в certs/.

Настройки — config.json (локально) или переменные окружения (GitHub Actions). Состояние — state.json.
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
CONFIG_FILE = os.path.join(HERE, "config.json")
CA_FILE = os.path.join(HERE, "certs", "russian_trusted_ca.pem")
API = "https://ticket.rzd.ru/apib2b/p/Railway/V1/Search/TrainPricing?service_provider=B2B_RZD"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
CAR_TYPE_NAMES = {"ReservedSeat": "плацкарт", "Compartment": "купе", "Luxury": "СВ",
                  "Soft": "люкс", "Sedentary": "сидячий", "Shared": "общий"}
MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


# ---------- конфиг / состояние ----------

def _bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "да")


def load_config():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    env = os.environ
    for key, var in (("origin_code", "ORIGIN_CODE"), ("destination_code", "DESTINATION_CODE"),
                     ("origin_node", "ORIGIN_NODE"), ("destination_node", "DESTINATION_NODE"),
                     ("origin_name", "ORIGIN_NAME"), ("destination_name", "DESTINATION_NAME"),
                     ("train", "TRAIN")):
        if env.get(var):
            cfg[key] = env[var].strip()
    if env.get("DATES"):
        cfg["dates"] = [d.strip() for d in env["DATES"].split(",") if d.strip()]
    if env.get("CAR_TYPES"):
        cfg["car_types"] = [c.strip() for c in env["CAR_TYPES"].split(",") if c.strip()]
    if env.get("INCLUDE_SIDE_LOWER"):
        cfg["include_side_lower"] = _bool(env["INCLUDE_SIDE_LOWER"])
    if env.get("MIN_LOWER"):
        cfg["min_lower"] = int(env["MIN_LOWER"])
    cfg.setdefault("car_types", ["ReservedSeat"])
    cfg.setdefault("include_side_lower", True)
    cfg.setdefault("min_lower", 1)
    cfg.setdefault("notify_after_failures", int(env.get("NOTIFY_AFTER_FAILURES", 6)))
    tg = cfg.setdefault("telegram", {})
    if env.get("TG_BOT_TOKEN"):
        tg["bot_token"] = env["TG_BOT_TOKEN"]
    if env.get("TG_CHAT_IDS"):
        tg["chat_ids"] = [c.strip() for c in env["TG_CHAT_IDS"].split(",") if c.strip()]
    missing = [k for k in ("origin_code", "destination_code", "train", "dates") if not cfg.get(k)]
    if missing:
        raise SystemExit("Не заданы: %s (config.json или переменные окружения)" % ", ".join(missing))
    return cfg


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"dates": {}, "failures": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


# ---------- API РЖД ----------

def ssl_context():
    ctx = ssl.create_default_context()
    if os.path.exists(CA_FILE):
        ctx.load_verify_locations(cafile=CA_FILE)
    return ctx


def search(cfg, date, ctx):
    body = {"Origin": cfg["origin_code"], "Destination": cfg["destination_code"],
            "DepartureDate": "%sT00:00:00" % date, "TimeFrom": 0, "TimeTo": 24,
            "CarGrouping": "DontGroup", "GetByLocalTime": True,
            "SpecialPlacesDemand": "StandardPlacesAndForDisabledPersons",
            "CarIssuingType": "All", "GetTrainsFromSchedule": True}
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST", headers={
        "User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json, text/plain, */*",
        "Origin": "https://ticket.rzd.ru", "Referer": "https://ticket.rzd.ru/"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError("РЖД HTTP %s: %r" % (e.code, e.read()[:300]))
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise RuntimeError("РЖД вернул не JSON (вероятно, антибот-заглушка): %r" % raw[:200])


def norm_train(num):
    """'030Ч' → '030': буква в номере зависит от направления/даты, сравниваем по цифрам."""
    return "".join(ch for ch in str(num) if ch.isdigit()).lstrip("0") or "0"


def find_train(data, train):
    want = norm_train(train)
    for t in data.get("Trains") or []:
        if norm_train(t.get("TrainNumber")) == want or norm_train(t.get("DisplayTrainNumber")) == want:
            return t
    return None


def summarize(train, cfg):
    """Сумма мест по нужным типам вагонов."""
    s = {"lower": 0, "upper": 0, "side_lower": 0, "side_upper": 0, "total": 0, "min_price": None}
    for g in train.get("CarGroups") or []:
        if g.get("CarType") not in cfg["car_types"] or g.get("IsSaleForbidden"):
            continue
        s["lower"] += int(g.get("LowerPlaceQuantity") or 0)
        s["upper"] += int(g.get("UpperPlaceQuantity") or 0)
        s["side_lower"] += int(g.get("LowerSidePlaceQuantity") or 0)
        s["side_upper"] += int(g.get("UpperSidePlaceQuantity") or 0)
        s["total"] += int(g.get("TotalPlaceQuantity") or g.get("PlaceQuantity") or 0)
        p = g.get("MinPrice")
        if p is not None and (s["min_price"] is None or p < s["min_price"]):
            s["min_price"] = p
    s["wanted"] = s["lower"] + (s["side_lower"] if cfg["include_side_lower"] else 0)
    return s


# ---------- форматирование ----------

def fmt_dt(iso):
    d = datetime.fromisoformat(iso)
    return "%d %s (%s) %s" % (d.day, MONTHS_RU[d.month - 1], WEEKDAYS_RU[d.weekday()], d.strftime("%H:%M"))


def night_hint(iso):
    """Отправление после полуночи: подсказка «в ночь с … на …», чтобы не перепутать дату."""
    d = datetime.fromisoformat(iso)
    if d.hour >= 5:
        return ""
    prev = d - timedelta(days=1)
    return " — в ночь с %d на %d %s" % (prev.day, d.day, MONTHS_RU[d.month - 1])


def fmt_price(p):
    return ("{:,}".format(int(round(p))).replace(",", " ") + " ₽") if p is not None else "—"


def places_line(s, cfg):
    parts = ["нижних %d" % s["lower"]]
    if "ReservedSeat" in cfg["car_types"]:
        parts.append("боковых нижних %d" % s["side_lower"])
    parts.append("верхних %d" % s["upper"])
    if "ReservedSeat" in cfg["car_types"]:
        parts.append("боковых верхних %d" % s["side_upper"])
    return ", ".join(parts)


def search_url(cfg, date):
    if cfg.get("origin_node") and cfg.get("destination_node"):
        return "https://ticket.rzd.ru/searchresults/v/1/%s/%s/%s" % (cfg["origin_node"], cfg["destination_node"], date)
    return "https://ticket.rzd.ru/"


def header(cfg, train, date):
    name = (" «%s»" % train["TrainName"]) if train.get("TrainName") else ""
    dep = train.get("LocalDepartureDateTime") or "%sT00:00:00" % date
    arr = train.get("LocalArrivalDateTime")
    route = "%s → %s" % (cfg.get("origin_name") or train.get("OriginName"), cfg.get("destination_name") or train.get("DestinationName"))
    types = ", ".join(CAR_TYPE_NAMES.get(c, c) for c in cfg["car_types"])
    return "🚆 <b>%s%s</b>, %s\nОтправление %s%s%s\nТип: %s" % (
        train.get("DisplayTrainNumber") or train.get("TrainNumber"), name, route,
        fmt_dt(dep), night_hint(dep), (", прибытие " + fmt_dt(arr)) if arr else "", types)


# ---------- Telegram ----------

def notify_telegram(cfg, text):
    tg = cfg.get("telegram") or {}
    token, chat_ids = tg.get("bot_token"), tg.get("chat_ids") or []
    if not token or not chat_ids:
        print("[tg] не настроен, сообщение:\n" + text)
        return
    for chat_id in chat_ids:
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token, data=body)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
            print("[tg] отправлено в %s" % chat_id)
        except Exception as e:  # noqa: BLE001
            print("[tg] ошибка отправки в %s: %s" % (chat_id, e))


# ---------- основная логика ----------

def run(cfg, state, dry=False):
    ctx = ssl_context()
    today = datetime.now().strftime("%Y-%m-%d")
    messages = []
    prev_all = state.setdefault("dates", {})
    for date in cfg["dates"]:
        if date < today:
            print("%s: дата прошла, пропускаю" % date)
            continue
        data = search(cfg, date, ctx)
        train = find_train(data, cfg["train"])
        prev = prev_all.get(date)
        if not train:
            print("%s: поезд %s не найден в выдаче (поездов: %d)" % (date, cfg["train"], len(data.get("Trains") or [])))
            if prev is None:
                messages.append("🚆 Поезд %s на %s не найден в выдаче РЖД — проверьте дату. Продолжаю следить." % (cfg["train"], date))
                prev_all[date] = {"found": False, "wanted": 0}
            continue
        s = summarize(train, cfg)
        print("%s: %s %s — %s, всего %d, от %s" % (date, train.get("TrainNumber"), train.get("LocalDepartureDateTime"),
                                                  places_line(s, cfg), s["total"], fmt_price(s["min_price"])))
        head = header(cfg, train, date)
        link = search_url(cfg, date)
        was = (prev or {}).get("wanted", 0)
        if s["wanted"] >= cfg["min_lower"] and s["wanted"] > was:
            messages.append("%s\n\n✅ <b>Появились нижние места: %d</b>\n%s\nВсего мест %d, от %s\n%s" % (
                head, s["wanted"], places_line(s, cfg), s["total"], fmt_price(s["min_price"]), link))
        elif prev is None or not prev.get("found", True):
            messages.append("%s\n\n👀 Взял на контроль. Сейчас: %s; всего мест %d%s\nНапишу, как только появится нижнее.\n%s" % (
                head, places_line(s, cfg), s["total"],
                (", от " + fmt_price(s["min_price"])) if s["total"] else "", link))
        elif was >= cfg["min_lower"] and s["wanted"] < cfg["min_lower"]:
            messages.append("%s\n\n❌ Нижние места снова закончились. Сейчас: %s." % (head, places_line(s, cfg)))
        prev_all[date] = {"found": True, "wanted": s["wanted"], "lower": s["lower"], "side_lower": s["side_lower"],
                          "total": s["total"], "checked_at": datetime.now().isoformat(timespec="seconds")}
    for date in list(prev_all):
        if date not in cfg["dates"]:
            del prev_all[date]
    if messages:
        text = "\n\n".join(messages)
        print("УВЕДОМЛЕНИЕ:\n" + text)
        if not dry:
            notify_telegram(cfg, text)
    else:
        print("Без изменений.")


def main():
    args = sys.argv[1:]
    cfg = load_config()
    state = load_state()
    if "--reset" in args:
        state = {"dates": {}, "failures": 0}
    if "--test" in args:
        notify_telegram(cfg, "🚆 Тест: бот мест РЖД работает. Поезд %s, даты: %s" % (cfg["train"], ", ".join(cfg["dates"])))
        return
    dry = "--dry" in args
    try:
        run(cfg, state, dry=dry)
        state["failures"] = 0
    except Exception as e:  # noqa: BLE001
        state["failures"] = state.get("failures", 0) + 1
        print("ОШИБКА (%d подряд): %s" % (state["failures"], e))
        if state["failures"] == cfg["notify_after_failures"] and not dry:
            notify_telegram(cfg, "⚠️ Бот мест РЖД: %d проверок подряд с ошибкой. Последняя: %s" % (state["failures"], str(e)[:300]))
        if not dry:
            save_state(state)
        sys.exit(1)
    if not dry:
        save_state(state)


if __name__ == "__main__":
    main()
