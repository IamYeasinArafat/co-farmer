"""Plain-language views for the farmer board (app.py): no numbers without meaning, no code words.

Everything here is computed from the database and the ranges in force (code, no AI), in English and Arabic,
so the farmer board works even before the first agent run and never waits for a model.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import db
from farm import field_info, field_kinds, tz_of
from knowledge import CANDIDATE_CROPS

T = {   # UI text: key -> (English, Arabic)
    "my_farm": ("My farm", "مزرعتي"),
    "updated": ("Updated", "آخر تحديث"),
    "today": ("Today", "اليوم"),
    "weather": ("Weather today", "طقس اليوم"),
    "up_to": ("up to", "حتى"),
    "hottest": ("hottest around", "الأشد حرارة حوالي"),
    "beds": ("Your beds", "أحواضك"),
    "advice": ("Today's advice", "نصيحة اليوم"),
    "todo": ("To do", "المطلوب منك"),
    "done": ("Done", "تم"),
    "helpful": ("Helpful", "مفيد"),
    "correct": ("Correct it", "صحّح"),
    "correct_q": ("What should it say or do?", "ماذا يجب أن تقول أو تفعل؟"),
    "save": ("Save", "حفظ"),
    "thanks": ("Thanks, noted.", "شكراً، تم التسجيل."),
    "plant": ("What to plant", "ماذا تزرع"),
    "plant_now": ("Plant now", "ازرع الآن"),
    "later": ("In the coming months", "في الأشهر القادمة"),
    "ready_in": ("ready in about", "جاهز خلال"),
    "days": ("days", "يوماً"),
    "per_harvest": ("per harvest from this bed", "لكل حصاد من هذا الحوض"),
    "water_need": ("Water", "الماء"),
    "care": ("Care", "العناية"),
    "notes": ("Tell us what you see", "أخبرنا بما تلاحظه"),
    "note_ph": ("e.g. the leaves on the west edge look pale", "مثلاً: الأوراق على الطرف الغربي باهتة"),
    "add": ("Send", "إرسال"),
    "ask": ("Ask the farm assistant", "اسأل مساعد المزرعة"),
    "ask_ph": ("Ask anything about your beds, what to plant, or prices…", "اسأل عن أحواضك أو ماذا تزرع أو الأسعار…"),
    "refresh": ("Get fresh advice", "احصل على نصيحة جديدة"),
    "refresh_help": ("The AI team checks your farm (takes a few minutes).", "يفحص فريق الذكاء الاصطناعي مزرعتك (يستغرق بضع دقائق)."),
    "no_plan": ("No advice yet: press 'Get fresh advice'.", "لا توجد نصيحة بعد: اضغط «احصل على نصيحة جديدة»."),
    "no_data": ("No readings from the sensors yet.", "لا توجد قراءات من الحساسات بعد."),
    "soil": ("Soil moisture", "رطوبة التربة"), "air": ("Air", "الهواء"), "tank": ("Tank", "الخزان"),
    "watered": ("Last watered", "آخر ري"), "never": ("not today", "ليس اليوم"),
    "ago_min": ("{n} min ago", "قبل {n} دقيقة"), "ago_h": ("{n} h ago", "قبل {n} ساعة"),
    "day_n": ("day {n}", "اليوم {n}"), "harvest_in": ("harvest in ~{n} days", "الحصاد بعد ~{n} يوماً"),
    "harvest_now": ("ready to harvest", "جاهز للحصاد"), "no_crop": ("no crop yet", "بدون محصول بعد"),
    "gross": ("Money is an estimate before costs (seed, water, labour).", "المبالغ تقديرية قبل التكاليف (البذور والماء والعمالة)."),
    "price_conf": ("price", "السعر"),
    "low": ("low", "قليل"), "medium": ("medium", "متوسط"), "high": ("high", "كثير"),
    "easy": ("easy", "سهلة"), "hard": ("hard", "صعبة"),
    "official": ("official Qatar price", "سعر رسمي في قطر"), "estimate": ("estimated price", "سعر تقديري"),
    "proxy": ("rough price guess", "سعر تقريبي"), "farmer": ("your price", "سعرك"),
    "market": ("Market tips", "نصائح السوق"),
    "team": ("Your AI farm team", "فريقك الذكي للمزرعة"),
    "big_picture": ("The big picture", "الصورة الكاملة"),
    "advice_only": ("advice", "نصيحة"),
    "n_warnings": ("warnings", "تنبيهات"), "n_changes": ("setting changes", "تعديلات"), "n_checked": ("checked by code", "راجعها النظام"),
    "n_todos": ("to-dos", "مهام"),
    "profit_year": ("Profit / year", "الربح السنوي"), "setup": ("Set-up cost", "تكلفة الإنشاء"), "payback": ("Pays back in", "يسترد خلال"),
    "years": ("years", "سنوات"), "sales": ("Sales", "المبيعات"), "per_year": ("per year", "سنوياً"), "of_sales": ("of sales", "من المبيعات"),
    "budget": ("budget", "الميزانية"), "no_budget": ("no budget given", "لم تُحدد ميزانية"),
    "site": ("Site plan", "مخطط المزرعة"), "setup_items": ("Where the set-up money goes", "أين تذهب تكلفة الإنشاء"),
    "combo": ("Best crop combination through the year", "أفضل مزيج محاصيل على مدار السنة"),
    "combo_note": ("Only crops that make money are kept, and no zone puts more than half its area into one crop.",
                   "نحتفظ فقط بالمحاصيل الرابحة، ولا يُخصَّص أكثر من نصف أي منطقة لمحصول واحد."),
    "money": ("Money in, money out (per year)", "الدخل والمصاريف (سنوياً)"),
    "options": ("Design options", "خيارات التصميم"), "option": ("Option", "الخيار"), "fits_budget": ("Fits budget", "ضمن الميزانية"),
    "today_crop": ("Today's crop only", "المحصول الحالي فقط"), "best_mix": ("best crop combination", "أفضل مزيج"),
    "tab_today": ("Today", "اليوم"), "tab_plan": ("Farm plan & money", "خطة المزرعة والمال"), "tab_talk": ("Notes & questions", "ملاحظات وأسئلة"),
    "kpi_beds": ("Beds doing well", "أحواض بحالة جيدة"), "kpi_beds_sub": ("right now", "الآن"),
    "kpi_harvest": ("Next harvest", "الحصاد القادم"), "kpi_water": ("Water", "الماء"),
    "estimate_word": ("estimate, after costs", "تقدير بعد التكاليف"),
    "planned_bed": ("Planned: ready to build", "مخطط: جاهز للإنشاء"),
    "stage_land": ("new farm plan", "خطة مزرعة جديدة"), "stage_farm": ("working farm", "مزرعة قائمة"),
    "land_note": ("This farm is still land: follow the farm plan tab to build it. Daily advice starts when the sensors are installed.",
                  "هذه المزرعة ما زالت أرضاً: اتبع تبويب خطة المزرعة لإنشائها. تبدأ النصائح اليومية عند تركيب الحساسات."),
}


def t(key: str, ar: bool, **kw: Any) -> str:
    en, arabic = T.get(key, (key, key))
    return (arabic if ar else en).format(**kw)


def crop_key(name: str | None) -> str | None:
    """'purple amaranth' / 'Lettuce' / 'mulukhiyah' -> CANDIDATE_CROPS key."""
    name = (name or "").strip().lower()
    if not name or name == "undecided":
        return None
    for key, crop in CANDIDATE_CROPS.items():
        if name in (key.replace("_", " "), crop["name"].lower(), crop["name"].split(" (")[0].lower(), crop.get("ar", "")):
            return key
    return next((k for k, c in CANDIDATE_CROPS.items() if name in c["name"].lower() or c["name"].lower() in name), None)


def crop_label(name: str | None, ar: bool) -> str:
    key = crop_key(name)
    if not key:
        return t("no_crop", ar) if not name or name == "undecided" else name
    crop = CANDIDATE_CROPS[key]
    return crop.get("ar", crop["name"]) if ar else crop["name"]


def _latest(con, field_id: str, since_s: int = 6 * 3600, farm_id: str | None = None) -> dict[str, float]:
    rows = db.read_log(con, time.time() - since_s, field_id, farm_id)
    out: dict[str, float] = {}
    for r in rows:
        if r["type"] == "reading":
            try:
                out[r["kind"]] = float(r["value"])
            except ValueError:
                pass
    return out


def _last_watered(con, field_id: str, farm_id: str | None = None) -> float | None:
    rows = [r for r in db.read_log(con, time.time() - 86400, field_id, farm_id)
            if r["type"] == "action" and r["value"] == "activated"]
    return rows[-1]["ts"] if rows else None


def bed_cards(con, farm: dict[str, Any], plan: dict[str, Any] | None, ar: bool) -> list[dict[str, Any]]:
    """One card per bed: a traffic light, a one-line headline, a few plain facts."""
    ranges = {f: v for f, v in (plan or {}).get("ranges", {}).items() if isinstance(v, dict)} or farm["limits"]["start"]
    cards = []
    kinds = field_kinds(farm["hardware"])
    tank = next((_latest(con, f, farm_id=farm["id"]).get("level") for f in kinds if "level" in kinds[f]), None)
    tank_min = next((r.get("level", [20])[0] for r in ranges.values() if r.get("level")), 20)
    for field_id in kinds:
        info = field_info(farm, field_id)
        now = _latest(con, field_id, farm_id=farm["id"])
        r = ranges.get(field_id, {})
        level, lines = "ok", []
        soil = now.get("soil_moisture")
        if soil is not None and r.get("soil_moisture"):
            lo, hi = r["soil_moisture"]
            if soil < lo:
                level = "act" if tank is not None and tank < tank_min else "watch"
                lines.append(("Dry: the tank is too low to water, refill it" if level == "act" else "Dry: the pump is watering it")
                             if not ar else ("جافة: الخزان منخفض جداً للري، املأه" if level == "act" else "جافة: المضخة تسقيه الآن"))
            elif soil < lo + 2:
                lines.append("Drying: it will be watered soon" if not ar else "تجف: سيتم الري قريباً")
            elif soil > hi + 5:
                level = "watch"
                lines.append("Very wet: check for a leak or stop hand-watering" if not ar else "مبللة جداً: تحقق من تسرب أو أوقف الري اليدوي")
            else:
                lines.append("Soil moisture is good" if not ar else "رطوبة التربة جيدة")
        temp = now.get("temp_air")
        if temp is not None and r.get("temp_air") and temp > r["temp_air"][1]:
            level = "watch" if level == "ok" else level
            lines.append(f"Hot ({temp:.0f} °C): shade cloth helps at midday" if not ar else f"حار ({temp:.0f}°م): الغطاء الظليل يساعد وقت الظهيرة")
        if "level" in kinds[field_id] and tank is not None and tank < tank_min + 10:
            level = "act" if tank < tank_min else ("watch" if level == "ok" else level)
            lines.append(f"Tank low ({tank:.0f} %): refill soon" if not ar else f"الخزان منخفض ({tank:.0f}٪): املأه قريباً")
        if not now:
            level, lines = "none", [t("no_data", ar)]
        watered = _last_watered(con, field_id, farm["id"])
        if watered:
            mins = int((time.time() - watered) / 60)
            ago = t("ago_min", ar, n=mins) if mins < 90 else t("ago_h", ar, n=round(mins / 60))
        else:
            ago = t("never", ar)
        crop = info.get("crop") or farm["profile"].get("crop")
        key = crop_key(crop)
        age = info.get("age_days")
        timing = None
        if key and age is not None:
            left = CANDIDATE_CROPS[key]["cycle_days"] - age
            timing = t("harvest_now", ar) if left <= 0 else t("harvest_in", ar, n=left)
        cards.append({"field_id": field_id, "level": level, "headline": lines[0] if lines else "", "more": lines[1:],
                      "crop": crop_label(crop, ar), "day": t("day_n", ar, n=age) if age is not None else None, "timing": timing,
                      "soil": soil, "temp": temp, "tank": now.get("level"), "watered": ago})
    return cards


def updated_at(con, farm: dict[str, Any]) -> str | None:
    row = con.execute("SELECT MAX(ts) FROM log WHERE farm_id = ?", (farm["id"],)).fetchone()[0]
    return datetime.fromtimestamp(row, tz_of(farm)).strftime("%H:%M") if row else None


def market_tips(plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    advice = (plan or {}).get("advice")
    if isinstance(advice, dict):
        return advice.get("market") or []
    return advice or []   # plans saved before crop advice existed stored a plain list


MONTHS_EN = {"Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April", "May": "May", "Jun": "June", "Jul": "July",
             "Aug": "August", "Sep": "September", "Oct": "October", "Nov": "November", "Dec": "December"}
MONTHS_AR = {"Jan": "يناير", "Feb": "فبراير", "Mar": "مارس", "Apr": "أبريل", "May": "مايو", "Jun": "يونيو", "Jul": "يوليو",
             "Aug": "أغسطس", "Sep": "سبتمبر", "Oct": "أكتوبر", "Nov": "نوفمبر", "Dec": "ديسمبر"}


def month_name(short: str | None, ar: bool) -> str:
    return (MONTHS_AR if ar else MONTHS_EN).get(short or "", short or "")
