"""Compact encoder feedback and nominal device specifications."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QWidget

from xeryon_control.domain.travel import STROKE_UM
from xeryon_control.ui.widgets import divider, glyph, label, value_label


class PositionSummary(QWidget):
    """Two aligned lines under the plot, with nominal geometry only."""

    def __init__(self, model):
        super().__init__()
        grid = QGridLayout(self)
        grid.setContentsMargins(3, 5, 3, 4)
        grid.setVerticalSpacing(3)
        grid.setHorizontalSpacing(8)
        grid.addWidget(glyph("pulse", 16), 0, 0, 2, 1)
        for column in (2, 4):
            grid.addWidget(divider(vertical=True), 0, column, 2, 1)
        self.encoder_title = label("编码器绝对位置", "muted")
        self.encoder_value = label("—", "summaryValue")
        self.encoder_value.setWordWrap(True)
        self.stroke_value = label("", "summaryValue")
        self.stroke_value.setWordWrap(True)
        self.feedback = value_label(model.feedback_text, "summaryValue", True)
        fields = (
            (1, label("反馈", "muted"), self.feedback, 2),
            (3, self.encoder_title, self.encoder_value, 5),
            (5, label("标称行程", "muted"), self.stroke_value, 4),
        )
        for column, title, value, stretch in fields:
            grid.addWidget(title, 0, column)
            grid.addWidget(value, 1, column, Qt.AlignmentFlag.AlignTop)
            grid.setColumnStretch(column, stretch)
        def update_encoder():
            self.encoder_title.setText("上次编码器位置" if model.position is not None
                                       and not model.is_fresh() else "编码器绝对位置")
            text = "—" if model.position is None else f"{model.position:,.2f} µm"
            if model.position_count is not None and model.position is not None:
                text += f"  ({model.position_count:,} 计数)"
            self.encoder_value.setText(text)

        self.stroke_value.setText(f"{STROKE_UM / 1000:g} mm")

        model.encoder_caption.subscribe(update_encoder)
        update_encoder()
