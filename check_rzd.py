#!/usr/bin/env python3
"""Следит за свободными нижними местами на конкретный поезд РЖД и пишет в Telegram.

Два источника данных (SOURCE в окружении или "source" в config.json):
  tutu — POST https://offers-api.tutu.ru/railway/offers (тот же запрос, что у сайта tutu.ru), без входа;
         в dictionary.train.voyages[*].cars лежат группы мест LOWER / UPPER / SIDE_LOWER / SIDE_UPPER
         по типам вагонов RESERVED_SEAT / COMPARTMENT / LUX / SEDENTARY. Доступен с серверов GitHub.
  rzd  — POST https://ticket.rzd.ru/apib2b/p/Railway/V1/Search/TrainPricing (API сайта РЖД);
         РЖД не пускает адреса GitHub/облаков, годится только с домашнего компьютера. Сертификат
         ticket.rzd.ru выдан Russian Trusted Root CA (НУЦ Минцифры) — бандл в certs/.
Цифры обоих источников совпадают (сверено 2026-10-07 на двух датах).

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
API_RZD = "https://ticket.rzd.ru/apib2b/p/Railway/V1/Search/TrainPricing?service_provider=B2B_RZD"
API_TUTU = "https://offers-api.tutu.ru/railway/offers"
TUTU_CAR_TYPES = {"ReservedSeat": "RESERVED_SEAT", "Compartment": "COMPARTMENT", "Luxury": "LUX",
                  "Sedentary": "SEDENTARY"}   # имена типов вагонов в конфиге — как у РЖД
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
    if env.get("SOURCE"):
        cfg["source"] = env["SOURCE"].strip().lower()
    if env.get("DEPARTURE_TIME"):
        cfg["departure_time"] = env["DEPARTURE_TIME"].strip()
    cfg.setdefault("source", "tutu")
    if cfg["source"] not in ("tutu", "rzd"):
        raise SystemExit("source должен быть tutu или rzd")
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


def post_json(url, body, headers, ctx, who):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"User-Agent": UA, "Content-Type": "application/json",
                                          "Accept": "application/json, text/plain, */*", **headers})
    try:
        with urllib.request.urlopen(req, timeout=45, context=ctx) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        raise RuntimeError("%s HTTP %s: %r" % (who, e.code, e.read()[:300]))
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise RuntimeError("%s вернул не JSON (вероятно, антибот-заглушка): %r" % (who, raw[:200]))


def search(cfg, date, ctx):
    """Поезда на дату в едином виде: список {number, name, dep, arr, groups:[{car_type, lower, upper,
    side_lower, side_upper, total, min_price}]}. car_type — в терминах РЖД (ReservedSeat, Compartment, Luxury)."""
    return search_tutu(cfg, date, ctx) if cfg["source"] == "tutu" else search_rzd(cfg, date, ctx)


def search_rzd(cfg, date, ctx):
    body = {"Origin": cfg["origin_code"], "Destination": cfg["destination_code"],
            "DepartureDate": "%sT00:00:00" % date, "TimeFrom": 0, "TimeTo": 24,
            "CarGrouping": "DontGroup", "GetByLocalTime": True,
            "SpecialPlacesDemand": "StandardPlacesAndForDisabledPersons",
            "CarIssuingType": "All", "GetTrainsFromSchedule": True}
    data = post_json(API_RZD, body, {"Origin": "https://ticket.rzd.ru", "Referer": "https://ticket.rzd.ru/"}, ctx, "РЖД")
    out = []
    for t in data.get("Trains") or []:
        groups = []
        for g in t.get("CarGroups") or []:
            if g.get("IsSaleForbidden"):
                continue
            groups.append({"car_type": g.get("CarType"), "lower": int(g.get("LowerPlaceQuantity") or 0),
                           "upper": int(g.get("UpperPlaceQuantity") or 0),
                           "side_lower": int(g.get("LowerSidePlaceQuantity") or 0),
                           "side_upper": int(g.get("UpperSidePlaceQuantity") or 0),
                           "total": int(g.get("TotalPlaceQuantity") or g.get("PlaceQuantity") or 0),
                           "min_price": g.get("MinPrice")})
        out.append({"number": t.get("DisplayTrainNumber") or t.get("TrainNumber"), "name": t.get("TrainName") or "",
                    "dep": t.get("LocalDepartureDateTime"), "arr": t.get("LocalArrivalDateTime"), "groups": groups})
    return out


def search_tutu(cfg, date, ctx):
    import uuid
    body = {"routes": [{"departureStationCode": str(cfg["origin_code"]), "arrivalStationCode": str(cfg["destination_code"]),
                        "departureDate": date}], "searchId": str(uuid.uuid4()), "source": "trainOffers"}
    data = post_json(API_TUTU, body, {"Origin": "https://www.tutu.ru", "Referer": "https://www.tutu.ru/"}, ctx, "tutu")
    if isinstance(data, list):
        data = data[0] if data else {}
    voyages = ((data.get("dictionary") or {}).get("train") or {}).get("voyages") or {}
    rzd_type = {v: k for k, v in TUTU_CAR_TYPES.items()}
    out = []
    for v in voyages.values():
        groups = []
        for car in v.get("cars") or []:
            g = {"car_type": rzd_type.get(car.get("type"), car.get("type")), "lower": 0, "upper": 0,
                 "side_lower": 0, "side_upper": 0, "total": 0, "min_price": None}
            for sg in car.get("seatsGroups") or []:
                n = int(sg.get("seatsCount") or 0)
                key = {"LOWER": "lower", "UPPER": "upper", "SIDE_LOWER": "side_lower", "SIDE_UPPER": "side_upper"}.get(sg.get("type"))
                if key:
                    g[key] += n
                g["total"] += n
            groups.append(g)
        # stops — остановки от станции посадки до конечной; иногда tutu отдаёт их без времени,
        # тогда время берём из полного маршрута (stopsBeforeDeparture + stops) по названию станции
        stops = (v.get("stops") or []) + []
        full = (v.get("stopsBeforeDeparture") or []) + stops
        origin = (cfg.get("origin_name") or "").lower()[:6]
        dep = next((st.get("departureTime") for st in stops if st.get("departureTime")), None) \
            or next((st.get("departureTime") for st in full if origin and st.get("name", "").lower().startswith(origin)), None)
        arr = next((st.get("arrivalTime") for st in reversed(stops) if st.get("arrivalTime")), None)
        dep, arr = (dep or "")[:19] or None, (arr or "")[:19] or None
        out.append({"number": v.get("numberForPassengers") or v.get("number"), "name": "", "dep": dep, "arr": arr, "groups": groups})
    return out


def norm_train(num):
    """'030Ч' → '030': буква в номере зависит от направления/даты, сравниваем по цифрам."""
    return "".join(ch for ch in str(num) if ch.isdigit()).lstrip("0") or "0"


def find_train(trains, train):
    want = norm_train(train)
    for t in trains:
        if norm_train(t.get("number")) == want:
            return t
    return None


def summarize(train, cfg):
    """Сумма мест по нужным типам вагонов."""
    s = {"lower": 0, "upper": 0, "side_lower": 0, "side_upper": 0, "total": 0, "min_price": None}
    for g in train["groups"]:
        if g["car_type"] not in cfg["car_types"]:
            continue
        for k in ("lower", "upper", "side_lower", "side_upper", "total"):
            s[k] += g[k]
        p = g.get("min_price")
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
    d = datetime.strptime(date, "%Y-%m-%d")
    links = ["tutu: https://www.tutu.ru/poezda/rasp_d.php?nnst1=%s&nnst2=%s&date=%s" % (
        cfg["origin_code"], cfg["destination_code"], d.strftime("%d.%m.%Y"))]
    if cfg.get("origin_node") and cfg.get("destination_node"):
        links.insert(0, "РЖД: https://ticket.rzd.ru/searchresults/v/1/%s/%s/%s" % (cfg["origin_node"], cfg["destination_node"], date))
    return "\n".join(links)


def header(cfg, train, date):
    name = (" «%s»" % train["name"]) if train.get("name") else ""
    # tutu иногда отдаёт поезд без остановок и времени — тогда берём departure_time из настроек
    dep = train.get("dep") or "%sT%s:00" % (date, cfg.get("departure_time") or "00:00")
    arr = train.get("arr")
    route = "%s → %s" % (cfg.get("origin_name") or cfg["origin_code"], cfg.get("destination_name") or cfg["destination_code"])
    types = ", ".join(CAR_TYPE_NAMES.get(c, c) for c in cfg["car_types"])
    return "🚆 <b>%s%s</b>, %s\nОтправление %s%s%s\nТип: %s" % (
        train["number"], name, route, fmt_dt(dep), night_hint(dep), (", прибытие " + fmt_dt(arr)) if arr else "", types)


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
        trains = search(cfg, date, ctx)
        train = find_train(trains, cfg["train"])
        prev = prev_all.get(date)
        if not train:
            print("%s: поезд %s не найден в выдаче %s (поездов: %d)" % (date, cfg["train"], cfg["source"], len(trains)))
            if prev is None:
                messages.append("🚆 Поезд %s на %s не найден в выдаче РЖД — проверьте дату. Продолжаю следить." % (cfg["train"], date))
                prev_all[date] = {"found": False, "wanted": 0}
            continue
        s = summarize(train, cfg)
        print("%s [%s]: %s %s — %s, всего %d%s" % (date, cfg["source"], train["number"], train.get("dep"),
                                                places_line(s, cfg), s["total"],
                                                (", от " + fmt_price(s["min_price"])) if s["min_price"] is not None else ""))
        head = header(cfg, train, date)
        link = search_url(cfg, date)
        was = (prev or {}).get("wanted", 0)
        if s["wanted"] >= cfg["min_lower"] and s["wanted"] > was:
            messages.append("%s\n\n✅ <b>Появились нижние места: %d</b>\n%s\nВсего мест %d%s\n%s" % (
                head, s["wanted"], places_line(s, cfg), s["total"],
                (", от " + fmt_price(s["min_price"])) if s["min_price"] is not None else "", link))
        elif prev is None or not prev.get("found", True):
            messages.append("%s\n\n👀 Взял на контроль. Сейчас: %s; всего мест %d%s\nНапишу, как только появится нижнее.\n%s" % (
                head, places_line(s, cfg), s["total"],
                (", от " + fmt_price(s["min_price"])) if s["total"] and s["min_price"] is not None else "", link))
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
