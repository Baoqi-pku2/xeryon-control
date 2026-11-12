"""Travel geometry and endpoint readings shared by the console and calibration views."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QWidget

from xeryon_control.domain.travel import STROKE_UM
from xeryon_control.ui.widgets import divider, glyph, label, value_label


class PositionSummary(QWidget):
    """Two aligned lines under the plot, with setup details in the travel dialog."""

    def __init__(self, model):
        super().__init__()
        grid = QGridLayout(self)
        grid.setContentsMargins(3, 5, 3, 4)
        grid.setVerticalSpacing(3)
        grid.setHorizontalSpacing(8)
        grid.addWidget(glyph("pulse", 16), 0, 0, 2, 1)
        for column in (2, 4, 6):
            grid.addWidget(divider(vertical=True), 0, column, 2, 1)
        self.encoder_title = label("编码器绝对位置", "muted")
        self.encoder_value = label("—", "summaryValue")
        self.encoder_value.setWordWrap(True)
        self.stroke_value = label("", "summaryValue")
        self.stroke_value.setWordWrap(True)
        self.distances = [label("—", "summaryValue") for _ in range(2)]
        self.feedback = value_label(model.feedback_text, "summaryValue", True)
        fields = (
            (1, label("反馈", "muted"), self.feedback, 2),
            (3, self.encoder_title, self.encoder_value, 5),
            (5, label("机械行程", "muted"), self.stroke_value, 4),
            (7, label("距收回端", "muted"), self.distances[0], 2),
            (8, label("距伸出端", "muted"), self.distances[1], 2),
        )
        for column, title, value, stretch in fields:
            grid.addWidget(title, 0, column)
            grid.addWidget(value, 1, column, Qt.AlignmentFlag.AlignTop)
            grid.setColumnStretch(column, stretch)
        for value in self.distances:
            value.setToolTip("按机械行程和当前位置估算。完整坐标见「行程检查」。")

        def update_encoder():
            self.encoder_title.setText("上次编码器位置" if model.position is not None
                                       and not model.is_fresh() else "编码器绝对位置")
            text = "—" if model.position is None else f"{model.position:,.2f} µm"
            if model.position_count is not None and model.position is not None:
                text += f"  ({model.position_count:,} 计数)"
            self.encoder_value.setText(text)

        def update_travel():
            readings = model.travel_readings.get()
            stroke = f"{STROKE_UM / 1000:g} mm"
            if model.default_limit_active():
                stroke += "   限位 0–13 mm"
            self.stroke_value.setText(stroke)
            distances = readings.distances_um if readings is not None else None
            for index, value in enumerate(self.distances):
                value.setText("—" if distances is None else f"{distances[index] / 1000:.3f} mm")

        model.encoder_caption.subscribe(update_encoder)
        model.travel_readings.subscribe(update_travel)
        update_encoder()
        update_travel()


class TravelReadout(QWidget):
    def __init__(self, readings):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(20)
        geometry = QGridLayout()
        endpoints = QGridLayout()
        for grid in (geometry, endpoints):
            grid.setHorizontalSpacing(9)
            grid.setVerticalSpacing(3)
        layout.addLayout(geometry, 2)
        layout.addLayout(endpoints, 3)
        geometry.addWidget(label("机械行程", "metricTitle"), 0, 0, 1, 2)
        self.margin = label("—")
        self.overview = label("—")
        for row, (caption, value) in enumerate((
            ("标称行程", label(f"{STROKE_UM / 1000:g} mm")),
            ("每端预留", self.margin),
            ("全图范围", self.overview),
        ), start=1):
            geometry.addWidget(label(caption, "muted"), row, 0)
            geometry.addWidget(value, row, 1, Qt.AlignmentFlag.AlignRight)

        endpoints.addWidget(label("端点位置", "metricTitle"), 0, 0)
        self.coordinate = label("", "muted")
        endpoints.addWidget(self.coordinate, 0, 1, 1, 2, Qt.AlignmentFlag.AlignRight)
        for column, caption in enumerate(("端点", "坐标 (mm)", "估算距离 (mm)")):
            alignment = Qt.AlignmentFlag.AlignRight if column else Qt.AlignmentFlag.AlignLeft
            endpoints.addWidget(label(caption, "muted"), 1, column, alignment)
        self.endpoint_values = []
        self.distance_values = []
        for row, caption in enumerate(("收回端", "伸出端"), start=2):
            endpoints.addWidget(label(caption, "muted"), row, 0)
            for column, cells in ((1, self.endpoint_values), (2, self.distance_values)):
                cell = label("—")
                endpoints.addWidget(cell, row, column, Qt.AlignmentFlag.AlignRight)
                cells.append(cell)
        self.setToolTip("端点坐标按已确认的机械行程计算。距离使用当前轴的有效位置反馈。")
        readings.subscribe(lambda: self.update_values(readings.get()))
        self.update_values(readings.get())

    def update_values(self, readings):
        if readings is None:
            return
        self.coordinate.setText(
            "待标定" if readings.endpoints_um is None
            else "相对用户零点" if readings.user_coordinates else "编码器坐标"
        )
        self.margin.setText("—" if readings.margin_um is None else f"{readings.margin_um / 1000:g} mm")
        lo, hi = readings.overview_um
        self.overview.setText(f"{lo / 1000:g} 至 {hi / 1000:g} mm")
        for cells, values in ((self.endpoint_values, readings.endpoints_um),
                              (self.distance_values, readings.distances_um)):
            for index, cell in enumerate(cells):
                cell.setText("—" if values is None else f"{round(values[index] / 1000, 3) or 0:.3f}")
