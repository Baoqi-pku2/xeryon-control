"""Interactive position diagram in the encoder coordinate system."""

from math import ceil, floor, isfinite, log10

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

CENTER = Qt.AlignmentFlag.AlignCenter


class PositionPlot(QWidget):
    foot = 30

    def __init__(self, model):
        super().__init__()
        self.model = model
        self.setMinimumSize(325, 196)
        self.setAccessibleName("推杆位置图，向下伸出为正")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    @property
    def housing_scale(self):
        return min(0.86, self.width() / 650)

    @property
    def top(self):
        # Leave room above the zero line for the board and the coordinate caption.
        return ceil(29 + 73 * self.housing_scale)

    def y_for(self, raw):
        m = self.model
        return self.top + (raw - m.view_min) / (m.view_max - m.view_min) * (
            self.height() - self.top - self.foot
        )

    def preview(self, y):
        self.model.preview_fraction(
            (y - self.top) / max(1, self.height() - self.top - self.foot)
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.preview(event.position().y())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.preview(event.position().y())

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.model.nudge(1 if event.key() == Qt.Key.Key_Down else -1)
        else:
            super().keyPressEvent(event)

    def housing(self, p, x, exit_y):
        # Original XLA drawing coordinates: the shaft is x=0, the PCB ends at +12.
        p.save()
        p.translate(x, exit_y)
        p.scale(self.housing_scale, self.housing_scale)
        p.translate(0, -39)
        p.setPen(QPen(QColor("#9cacc0"), 1.5))
        p.setBrush(QColor("#394d63"))
        p.drawRect(QRectF(-174, -14, 14, 28))
        p.drawRoundedRect(QRectF(-160, -34, 178, 68), 3, 3)
        fresh = self.model.is_fresh()
        p.setPen(QColor("#548782" if fresh else "#778a9d"))
        p.setBrush(QColor("#175c59" if fresh else "#47586b"))
        p.drawRect(QRectF(-152, -29, 164, 58))
        for dx in (-138, -12):
            for dy in (-21, 21):
                p.setPen(QColor("#c2cfdc"))
                p.setBrush(QColor("#223144"))
                p.drawEllipse(QPointF(dx, dy), 3.4, 3.4)
        p.setPen(QColor("#8da6b8"))
        p.setBrush(QColor("#192d3c"))
        for dx in (-112, -82):
            for dy in (-21, 7):
                p.drawRect(QRectF(dx, dy, 18, 14))
        p.fillRect(QRectF(-40, -21, 14, 42), QColor("#eeb34a"))
        p.setPen(QColor("#f5f8ff"))
        p.drawText(QRectF(-129, -8, 68, 16), CENTER, "XLA-5-95")
        p.setPen(QColor("#a0b2c4"))
        p.setBrush(QColor("#37495f"))
        p.drawRect(QRectF(-12, 34, 24, 5))
        p.restore()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        bottom, left, x = h - self.foot, 67, w * 0.52
        m = self.model
        background = QLinearGradient(0, 0, w, h)
        background.setColorAt(0, QColor("#13243b"))
        background.setColorAt(1, QColor("#0d192d"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(background)
        p.drawRoundedRect(QRectF(self.rect()), 6, 6)

        def line(x1, y1, x2, y2):
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        def clip(raw):
            return min(bottom, max(self.top, self.y_for(raw)))

        font = QFont(self.font())
        font.setPixelSize(12)
        p.setFont(font)
        p.setPen(QColor("#c4d2e6"))
        p.drawText(
            13,
            21,
            "用户坐标 / mm" if m.user_zero_set else "编码器坐标 / mm（零点待标定）",
        )
        p.drawText(
            QRectF(w - 146, 7, 132, 20),
            Qt.AlignmentFlag.AlignRight,
            f"跨度 {(m.view_max - m.view_min) / 1000:g} mm",
        )

        grid_pen = QPen(QColor("#38516b"), 1, Qt.PenStyle.DotLine)
        p.setPen(grid_pen)
        for col in range(1, 11):
            gx = left + (w - left - 14) * col / 10
            line(gx, self.top, gx, bottom)

        zero = m.user_zero_um if m.user_zero_set else 0
        # Choose a multiple of 1, 2, or 5 that keeps labels separate at any zoom.
        desired = (m.view_max - m.view_min) / max(2, (bottom - self.top) / 30)
        power = 10 ** floor(log10(desired))
        step = next(n * power for n in (1, 2, 5, 10) if n * power >= desired)
        minor = step / 5
        first = ceil((m.view_min - zero) / minor)
        last = floor((m.view_max - zero) / minor)
        for tick in range(first, last + 1):
            relative = tick * minor
            y = self.y_for(zero + relative)
            if tick % 5 == 0:
                p.setPen(grid_pen)
                line(left, y, w - 14, y)
                p.setPen(QColor("#dbe5f5"))
                p.drawText(
                    QRectF(3, y - 9, left - 11, 18),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    f"{relative / 1000:.{max(2, 3 - floor(log10(step)))}f}",
                )
            p.setPen(QColor("#91a6c2"))
            line(left - (6 if tick % 5 == 0 else 3), y, left, y)
        line(left, self.top, left, bottom)
        if m.app.travel_range.confirmed:
            lo, hi = m.app.travel_range.command_bounds
            p.fillRect(
                QRectF(left + 2, clip(lo), 2, clip(hi) - clip(lo)), QColor("#508996")
            )

        body = clip(zero) if m.user_zero_set else self.top
        actual = m.position
        if actual is not None and isfinite(actual):
            y = clip(actual)
            half_width = 8 * self.housing_scale
            shaft = QRectF(x - half_width, min(body, y), 2 * half_width, abs(y - body))
            p.setPen(QColor("#afc2d6"))
            p.setBrush(QColor("#37495f"))
            p.drawRect(shaft)
            p.setPen(QColor("#8096b0"))
            line(x - half_width * 0.45, body, x - half_width * 0.45, y)
        self.housing(p, x, body)
        try:
            target = float(m.target.get())
        except ValueError:
            target = None
        previous_box = None
        for raw, is_target in ((actual, False), (target, True)):
            if raw is None or not isfinite(raw):
                continue
            y = clip(raw)
            fresh = m.is_fresh()
            ink = QColor("#ffc05b" if is_target else "#71adff" if fresh else "#b2bdcd")
            p.setPen(
                QPen(
                    ink,
                    1.5,
                    Qt.PenStyle.DashLine if is_target else Qt.PenStyle.SolidLine,
                )
            )
            line(left, y, w - 13, y)
            p.setPen(QPen(ink, 1))
            p.setBrush(ink)
            if is_target:
                p.drawPolygon(
                    QPolygonF(
                        [
                            QPointF(x, y - 5),
                            QPointF(x + 5, y),
                            QPointF(x, y + 5),
                            QPointF(x - 5, y),
                        ]
                    )
                )
            else:
                p.drawEllipse(QPointF(x, y), 4.5, 4.5)
            name = "目标" if is_target else "实际" if fresh else "上次读数（过期）"
            text = f"{name}  {(raw - zero) / 1000:+.2f} mm"
            if not m.view_min <= raw <= m.view_max:
                text = name + "（超出视图）"
            box_w = p.fontMetrics().horizontalAdvance(text) + 17
            bx = min(x + max(14, w * 0.105), w - box_w - 8)
            by = y - 12
            if previous_box and abs(by - previous_box.top()) < 26:
                by = previous_box.top() + (26 if y < bottom - 22 else -26)
            box = QRectF(bx, by, box_w, 24)
            p.setBrush(QColor("#17263a"))
            p.drawRoundedRect(box, 3, 3)
            p.drawText(box, CENTER, text)
            previous_box = box
        p.setPen(QColor("#b9cce0"))
        p.drawText(
            QRectF(w - 132, h - 24, 117, 19),
            Qt.AlignmentFlag.AlignRight,
            "向下伸出为正   ↓",
        )
        p.end()
