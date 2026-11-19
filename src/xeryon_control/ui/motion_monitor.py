"""Encoder motion history, rendered in sample order even on position axes."""

from html import escape
from math import ceil, floor, log10
from time import monotonic

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import QSizePolicy, QSpinBox, QVBoxLayout, QWidget

from xeryon_control.domain.kinematics import EncoderSmoother
from xeryon_control.ui.widgets import button, card, check, combo, label, row


COLORS = ("#3159cc", "#c46118", "#098277", "#9853a2", "#ad4051", "#437d28", "#297baf")
METRICS = (("实际位置", "µm"), ("估算速度", "µm/s"), ("估算加速度", "µm/s²"))


def value_ticks(low, high):
    """Readable linear ticks for the vertical space available in a chart."""
    raw = (high - low) / 3
    scale = 10 ** floor(log10(raw))
    step = next(n for n in (1, 2, 2.5, 5, 10) if n >= raw/scale) * scale
    return [i*step for i in range(ceil(low/step), floor(high/step)+1)]


class Curve:
    def __init__(self):
        self.path = QPainterPath()
        self.bounds = None
        self.break_line = True

    def add(self, x, y):
        if self.break_line:
            self.path.moveTo(x, y)
            self.break_line = False
        else:
            self.path.lineTo(x, y)
        if self.bounds is None:
            self.bounds = (x, x, y, y)
        else:
            left, right, bottom, top = self.bounds
            self.bounds = min(left, x), max(right, x), min(bottom, y), max(top, y)


class SegmentCurves:
    def __init__(self, segment, color):
        self.segment = segment
        self.color = color
        self.smoother = EncoderSmoother(segment.scale)
        self.curves = [Curve() for _ in range(5)]
        self.last_time = None

    def add(self, stamp, position):
        t = stamp - self.segment.clicked
        if self.last_time is not None:
            if t <= self.last_time:
                return
            if t - self.last_time > 1.0:
                self.finish()
                for curve in self.curves:
                    curve.break_line = True
        self.last_time = t
        self.curves[0].add(t, position)
        self._estimates(self.smoother.add(t, position))

    def finish(self):
        self._estimates(self.smoother.finish())

    def _estimates(self, samples):
        for t, position, v, a in samples:
            self.curves[1].add(t, v)
            self.curves[2].add(t, a)
            self.curves[3].add(position, v)
            self.curves[4].add(position, a)


class MonitorChart(QWidget):
    def __init__(self, page, metric):
        super().__init__()
        self.page, self.metric_index = page, metric
        self.setMinimumHeight(100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName(METRICS[metric][0])

    @property
    def by_position(self):
        return self.metric_index != 0 and self.page.axis.currentIndex() == 1

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            self._draw(painter)
        finally:
            painter.end()

    def _draw(self, painter):
        by_position = self.by_position
        idx = self.metric_index + (2 if by_position else 0)
        series = [(s.curves[idx], s.color) for s in self.page.series.values()
                  if s.curves[idx].bounds is not None]
        area = QRectF(72, 8, max(1, self.width()-96), max(1, self.height()-50))
        left, right, low, high = 0., 1., 0., 1.
        if series:
            bounds = [curve.bounds for curve, _ in series]
            left = min(b[0] for b in bounds) if by_position else 0.
            right = max(b[1] for b in bounds)
            if not by_position:
                right = max(s.last_time for s in self.page.series.values() if s.last_time is not None)
            low, high = min(b[2] for b in bounds), max(b[3] for b in bounds)
            margin = max((high-low)*0.08, 0.05)
            low, high = low-margin, high+margin
            if right <= left:
                right = left + 1.
        font = painter.font()
        font.setPixelSize(11)
        painter.setFont(font)
        center = Qt.AlignmentFlag.AlignCenter
        for tick in value_ticks(low, high):
            frac = (tick-low)/(high-low)
            y = area.bottom()-area.height()*frac
            painter.setPen(QColor("#e1e8f0"))
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            painter.setPen(QColor("#51637a"))
            painter.drawText(QRectF(0, y-8, area.left()-8, 16),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f"{tick:g}" if series else "—")
        for i in range(5):
            frac = i/4
            x = area.left()+area.width()*frac
            painter.drawText(QRectF(x-34, area.bottom()+2, 68, 16), center,
                             f"{left+(right-left)*frac:.5g}" if series else "—")
        painter.drawText(QRectF(area.left(), area.bottom()+20, area.width(), 17), center,
                         "实际位置 (µm)" if by_position else "离开始运动的时间 (s)")
        if not series:
            painter.drawText(area, center, "等待编码器样本" if self.page.series else "暂无运动记录")
            return
        sx, sy = area.width()/(right-left), area.height()/(high-low)
        transform = QTransform(sx, 0, 0, -sy, area.left()-left*sx, area.bottom()+low*sy)
        painter.setClipRect(area.adjusted(-2, -2, 2, 2))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setTransform(transform)
        for curve, color in series:
            pen = QPen(color, 1.8)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawPath(curve.path)
            painter.drawPoint(curve.path.currentPosition())


class MonitorPage(QWidget):
    def __init__(self, session):
        super().__init__()
        self.setObjectName("page")
        self.session = session
        self.runs = {}  # Click timestamp -> displayed motion number.
        self.series = {}
        self.run_number = self.color_index = 0
        self.cleared_at = float("-inf")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        self.axis = combo(session.monitor_axis_var, ("开始后时间", "实际位置"))
        self.axis.setAccessibleName("速度和加速度横轴")
        self.axis.currentIndexChanged.connect(self.repaint_charts)
        self.limit = check("只保留最近", session.monitor_limit_var, self.trim)
        self.count = QSpinBox()
        self.count.setAccessibleName("保留运动次数")
        self.count.setRange(1, 999)
        self.count.setValue(int(session.monitor_count_var.get()))
        self.count.setSuffix(" 次")
        self.count.valueChanged.connect(self.change_limit)
        self.clear_button = button("清空记录", self.clear)
        self.clear_button.setToolTip("清除当前显示的所有记录，从下一次开始运动重新记录")
        layout.addLayout(row(label("速度和加速度横轴"), self.axis, None,
                             self.limit, self.count, self.clear_button))
        self.legend = label("", "muted")
        self.legend.setWordWrap(True)
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.legend)
        self.charts = []
        for metric, (name, unit) in enumerate(METRICS):
            frame, inner = card()
            title_row = row(label(name, "title"), label(unit, "muted"), None)
            if metric:
                title_row.addWidget(label("RTS 平滑  0.5 s", "muted"))
            inner.addLayout(title_row)
            chart = MonitorChart(self, metric)
            inner.addWidget(chart, 1)
            layout.addWidget(frame, 1)
            self.charts.append(chart)
        self.trim()

    def change_limit(self, n):
        self.session.monitor_count_var.set_from_user(str(n))
        self.trim()

    def trim(self):
        self.count.setEnabled(self.limit.isChecked())
        if self.limit.isChecked():
            for key in list(self.runs)[:-self.count.value()]:
                del self.runs[key]
            self.series = {k: v for k, v in self.series.items() if k.clicked in self.runs}
        self.update_legend()
        self.repaint_charts()

    def clear(self):
        self.cleared_at = monotonic()
        self.series.clear()
        self.runs.clear()
        self.trim()

    def receive(self, kind, payload):
        if kind == "monitor_end":
            if payload in self.series:
                self.series[payload].finish()
        else:
            segment, stamp, position = payload
            if segment.clicked <= self.cleared_at:
                return
            if segment not in self.series:
                if segment.clicked not in self.runs:
                    self.run_number += 1
                    self.runs[segment.clicked] = self.run_number
                i = self.color_index
                color = (QColor(COLORS[i]) if i < len(COLORS)
                         else QColor.fromHsv(round(i*137.508) % 360, 180, 165))
                self.color_index += 1
                self.series[segment] = SegmentCurves(segment, color)
                self.trim()
            self.series[segment].add(stamp, position)
        self.repaint_charts()

    def update_legend(self):
        parts = []
        for seg, s in self.series.items():
            name = seg.name if seg.name != "开始运动" else "单段"
            reference = "" if seg.calibrated else "（编码器坐标）"
            parts.append(f'<span style="color:{s.color.name()}">■</span> '
                         f'第 {self.runs[seg.clicked]} 次 / {escape(seg.axis)} / {escape(name)}{reference}')
        self.legend.setText("&nbsp;&nbsp;&nbsp;&nbsp;".join(parts))
        self.legend.setVisible(bool(parts))
        self.clear_button.setEnabled(bool(parts))

    def repaint_charts(self):
        for chart in self.charts:
            chart.update()
