"""Отчёт «кто чем занимался за день»: из журнала событий и списка задач. Без запросов в сеть."""
import html
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь",
          "Октябрь", "Ноябрь", "Декабрь"]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
LABELS = {
    "add": "➕ создана", "done": "✅ выполнена", "undone": "↩️ снята отметка «выполнена»",
    "rename": "✏️ переименована", "complete": "🗄 убрана в архив", "uncomplete": "🗄 возвращена из архива",
    "executor": "👥 сменились исполнители", "deadline": "⏰ изменён срок", "deadlineFailed": "⏰ просрочена",
    "file": "📎 прикреплён файл", "delete": "🗑 удалена", "restore": "♻️ восстановлена",
    "string": "🏷 стикер", "text": "🏷 стикер", "numeric": "🏷 стикер", "sprint": "🏷 стикер",
    "timetracking": "⏱ время", "stopwatch": "⏱ секундомер", "timer": "⏱ таймер",
}
SKIP = {"deadlineFailed"}  # не действие человека


def day_window(d: date, tz: ZoneInfo):
    start = datetime.combine(d, time(), tz)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


def day_title(d: date):
    return f"{d:%d.%m.%Y} ({WEEKDAYS[d.weekday()]})"


def short_name(name: str):
    """«Настасья 👩🏽‍💻» → «Настасья»: только буквы, пробелы и дефисы."""
    return re.sub(r"[^\w\s\-.]", "", name or "").strip() or "Без имени"


def first_word(name: str):
    parts = short_name(name).split()
    return parts[0].lower() if parts else ""


def clean(text, limit=120):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def collect(user_id, events, tasks, start, end):
    """События одного человека за окно [start, end), сгруппированные по задаче или чату.

    events — из журнала; tasks — список задач API (для «создал» и «выполнил» без лога).
    Возвращает {ключ: {"task_id"|"chat_id", "items": [(ts, kind, extra, text)]}}.
    """
    groups = {}

    def put(key, ref, ts, kind, extra=None, text=None):
        g = groups.setdefault(key, {**ref, "items": []})
        g["items"].append((ts, kind, extra or {}, text or ""))

    seen = set()
    for e in events:
        if e["user_id"] != user_id or e["kind"] in SKIP or not (start <= e["ts"] < end):
            continue
        if e["kind"] == "message":
            target = e["task_id"] or e["chat_id"]
            put(target, {"task_id": e["task_id"], "chat_id": e["chat_id"]}, e["ts"], "message", text=e["text"])
        else:
            seen.add((e["task_id"], e["kind"]))
            put(e["task_id"], {"task_id": e["task_id"]}, e["ts"], e["kind"], e["extra"])
    # то, что видно по самим задачам, даже если события не дошли
    for t in tasks:
        if t.get("deleted"):
            continue
        ts = t.get("timestamp") or 0
        if t.get("createdBy") == user_id and start <= ts < end and (t["id"], "add") not in seen:
            put(t["id"], {"task_id": t["id"]}, ts, "add")
        done = t.get("completedTimestamp") or 0
        if (t.get("completed") and start <= done < end and user_id in (t.get("assigned") or [])
                and (t["id"], "done") not in seen):
            put(t["id"], {"task_id": t["id"]}, done, "done")
    return groups


def count(groups):
    return sum(len(g["items"]) for g in groups.values())


def plan(user_id, tasks, start, end):
    """План дня: задачи сотрудника со сроком в этот день и просроченные, не закрытые к началу дня.
    [(задача, статус)], статус: done — выполнена в этот день или раньше срока, open — нет, late — хвост с прошлых дней."""
    out = []
    for t in tasks:
        if t.get("deleted") or user_id not in (t.get("assigned") or []):
            continue
        dl = (t.get("deadline") or {}).get("deadline") or 0
        done = t.get("completedTimestamp") if t.get("completed") else None
        if not dl or dl >= end or (done and done < start):
            continue
        if dl >= start:
            out.append((t, "done" if done and done < end else "open"))
        elif not done or done >= start:
            out.append((t, "done" if done and done < end else "late"))
    return sorted(out, key=lambda x: ((x[0].get("deadline") or {}).get("deadline") or 0))


def render(name, d, groups, tasks_by_id, where, column_title, chat_title, partial=False, plan_items=()):
    """План|Факт за день. where(task) → «Доска › Колонка»; column_title(id); chat_title(id)."""
    e = html.escape
    out = [f"📊 <b>План|Факт · {e(name)}</b>\n{day_title(d)}"]

    # --- план ---
    if plan_items:
        done = sum(1 for _, s in plan_items if s == "done")
        lines = [f"🗓 <b>План: {len(plan_items)}, выполнено {done}</b>"]
        icon = {"done": "✅", "open": "⬜️", "late": "⏳"}
        for t, s in plan_items:
            tail = " (срок прошёл раньше)" if s == "late" else ""
            lines.append(f"{icon[s]} {e(clean(t.get('title'), 80))}{tail}")
        out.append("\n".join(lines))
    else:
        out.append("🗓 <b>План:</b> задач со сроком на этот день нет.")

    # --- факт ---
    boards, task_chats, group_chats = [], [], []
    order = sorted(groups.values(), key=lambda g: max(i[0] for i in g["items"]), reverse=True)
    for g in order:
        t = tasks_by_id.get(g.get("task_id") or "")
        actions = [(ts, k, x) for ts, k, x, _ in sorted(g["items"], key=lambda i: i[0]) if k != "message"]
        msgs = [(ts, txt) for ts, k, _, txt in sorted(g["items"], key=lambda i: i[0]) if k == "message"]
        if g.get("chat_id") and not g.get("task_id"):
            last = clean(msgs[-1][1], 140) if msgs else ""
            group_chats.append(f"• «{e(chat_title(g['chat_id']))}»: {len(msgs)} сообщ., последнее в {hm(msgs[-1][0])}"
                               + (f"\n  «{e(last)}»" if last else ""))
            continue
        title = f"<b>{e(clean(t.get('title'), 90))}</b>" if t else "<b>задача удалена или нет доступа</b>"
        place = f"\n  <i>{e(where(t))}</i>" if t else ""
        if actions:
            lines = []
            for ts, kind, extra in actions:
                if kind == "move":
                    label = f"➡️ перенесена в «{e(column_title(extra.get('to')))}»"
                elif kind == "rename" and extra.get("newName"):
                    label = f"✏️ новое название «{e(clean(extra['newName'], 60))}»"
                else:
                    label = LABELS.get(kind, kind)
                lines.append(f"  {hm(ts)} {label}")
            boards.append(f"• {title}{place}\n" + "\n".join(lines))
        if msgs:
            last = clean(msgs[-1][1], 140)
            task_chats.append(f"• {title}: {len(msgs)} сообщ., последнее в {hm(msgs[-1][0])}"
                              + (f"\n  «{e(last)}»" if last else ""))
    total = count(groups)
    if not total:
        out.append("✅ <b>Факт:</b> действий в YouGile за этот день не найдено.")
    else:
        n_msg = sum(1 for g in groups.values() for i in g["items"] if i[1] == "message")
        out.append(f"✅ <b>Факт: {total} действий</b> (из них сообщений: {n_msg})")
        if boards:
            out.append("📋 <b>Доски задач</b>\n" + "\n".join(boards))
        if task_chats:
            out.append("💬 <b>Чаты задач</b>\n" + "\n".join(task_chats))
        if group_chats:
            out.append("👥 <b>Групповые чаты</b>\n" + "\n".join(group_chats))
    out.append("<i>Личные переписки YouGile не видны боту: их API не отдаёт.</i>"
               + ("\n<i>Этот день раньше запуска бота: история собрана по задачам сотрудника, могут быть пропуски.</i>"
                  if partial else ""))
    return "\n\n".join(out)


def digest(groups, tasks_by_id, column_title, chat_title, plan_items=()):
    """Факты дня простым текстом для конспекта: задачи с действиями и выдержки из переписки."""
    out = []
    if plan_items:
        out.append("План (сроки на день): " + "; ".join(
            f"{clean(t.get('title'), 70)} — {'выполнена' if s == 'done' else 'не выполнена'}"
            + (" (хвост с прошлых дней)" if s == "late" else "") for t, s in plan_items))
    order = sorted(groups.values(), key=lambda g: max(i[0] for i in g["items"]), reverse=True)
    for g in order:
        t = tasks_by_id.get(g.get("task_id") or "")
        acts, msgs = [], []
        for ts, kind, extra, text in sorted(g["items"], key=lambda i: i[0]):
            if kind == "message":
                if clean(text):
                    msgs.append(clean(text, 160))
            elif kind == "move":
                acts.append(f"перенесена в «{column_title(extra.get('to'))}»")
            else:
                acts.append(LABELS.get(kind, kind).split(" ", 1)[-1])
        n_msg = sum(1 for i in g["items"] if i[1] == "message")
        if g.get("chat_id") and not g.get("task_id"):
            head = f"Групповой чат «{chat_title(g['chat_id'])}»"
        else:
            head = f"Задача «{clean(t.get('title'), 90)}»" if t else "Удалённая задача"
        line = head + (": " + ", ".join(dict.fromkeys(acts)) if acts else "")
        if n_msg:
            line += f"; сообщений {n_msg}" + (": " + " | ".join(msgs[-4:]) if msgs else "")
        out.append(line)
    return "\n".join(f"- {x}" for x in out)


def company_line(name, groups, tasks_by_id):
    """Одна строка итогов: «Имя: ✅2 ➡️3 ➕1 💬15 — задача1; задача2»."""
    kinds = {}
    for g in groups.values():
        for _, k, _, _ in g["items"]:
            kinds[k] = kinds.get(k, 0) + 1
    parts = [f"{icon}{kinds[k]}" for k, icon in (("done", "✅"), ("move", "➡️"), ("add", "➕"), ("message", "💬"))
             if kinds.get(k)]
    other = sum(v for k, v in kinds.items() if k not in ("done", "move", "add", "message"))
    if other:
        parts.append(f"✏️{other}")
    # главное — задачи с наибольшим числом действий, выполненные вперёд
    top = sorted((g for g in groups.values() if g.get("task_id") in tasks_by_id),
                 key=lambda g: (any(i[1] == "done" for i in g["items"]), len(g["items"])), reverse=True)[:2]
    names = "; ".join(clean(tasks_by_id[g["task_id"]].get("title"), 40) for g in top)
    return f"<b>{html.escape(name)}</b>: {' '.join(parts)}" + (f" — {html.escape(names)}" if names else "")


def company(d, lines, idle, partial=False):
    total = len(lines)
    text = [f"🏢 <b>Итоги дня компании</b> · {day_title(d)}", f"Работали в YouGile: {total}"]
    text += lines
    if idle:
        text.append("Без действий: " + html.escape(", ".join(idle)))
    text.append("<i>✅ выполнено ➡️ перенесено ➕ создано 💬 сообщений ✏️ прочее</i>")
    if partial:
        text.append("<i>День раньше запуска бота: учтены только созданные и выполненные задачи.</i>")
    return "\n".join(text)


def overdue(tasks, now_ms):
    """Открытые задачи с прошедшим сроком: {user_id: [(задача, дней просрочки)]}."""
    out = {}
    for t in tasks:
        dl = (t.get("deadline") or {}).get("deadline") or 0
        if not dl or dl >= now_ms or t.get("deleted") or t.get("archived") or t.get("completed"):
            continue
        days = max(1, int((now_ms - dl) // 86400_000))
        for uid in t.get("assigned") or ["—"]:
            out.setdefault(uid, []).append((t, days))
    for items in out.values():
        items.sort(key=lambda x: -x[1])
    return out


def week_row(user_id, events, tasks, start, end, now_ms):
    """Неделя по человеку: выполнено, создано, сообщений, всего действий, просрочено сейчас."""
    g = collect(user_id, events, tasks, start, end)
    kinds = {}
    for grp in g.values():
        for _, k, _, _ in grp["items"]:
            kinds[k] = kinds.get(k, 0) + 1
    late = len(overdue([t for t in tasks if user_id in (t.get("assigned") or [])], now_ms).get(user_id, []))
    return {"done": kinds.get("done", 0), "add": kinds.get("add", 0), "msg": kinds.get("message", 0),
            "total": count(g), "late": late}


_tz = None


def hm(ts):
    return datetime.fromtimestamp(ts / 1000, _tz).strftime("%H:%M")


def set_tz(tz):
    global _tz
    _tz = tz


def chunks(text, limit=3900):
    """Режет длинный отчёт по пустым строкам, а слишком длинные блоки — по строкам, чтобы влезть в Telegram."""
    pieces = []
    for block in text.split("\n\n"):
        if len(block) <= limit:
            pieces.append((block, "\n\n"))
        else:
            pieces += [(line[:limit], "\n") for line in block.split("\n")]
    out, cur = [], ""
    for piece, sep in pieces:
        if cur and len(cur) + len(piece) + 2 > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}{sep}{piece}" if cur else piece
    return out + ([cur] if cur else [])
