"""Выгрузка истории изменений в Excel."""
import io

from openpyxl import Workbook
from openpyxl.styles import Font

from price_bot import calc

HEAD = ["Дата и время", "Кто", "Артикул", "Что меняли", "Было", "Стало", "Изменение, ₽", "Изменение, %"]


def history_xlsx(entries):
    """entries — записи истории (новые сверху) → байты .xlsx."""
    wb = Workbook()
    ws = wb.active
    ws.title = "История"
    ws.append(HEAD)
    for c in ws[1]:
        c.font = Font(bold=True)
    for e in entries:
        when = e.get("at", e["ts"]).replace("T", " ")[:16]
        if e["kind"] == "price":
            old, new = e.get("old"), e["new"]
            ws.append([when, e["who"], e["offer"], "Цена", old, new,
                       new - old if old else None, round((new - old) / old * 100, 1) if old else None])
        else:
            ws.append([when, e["who"], e["offer"], "Скидка от количества",
                       calc.fmt_steps(e.get("old")), calc.fmt_steps(e["new"]), None, None])
    for col, width in zip("ABCDEFGH", (17, 22, 18, 22, 30, 30, 14, 14)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
