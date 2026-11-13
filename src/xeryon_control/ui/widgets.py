"""Qt form controls with two-way binding to the session's values."""

from PySide6.QtCore import QPointF, QSignalBlocker, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from xeryon_control.ui.icons import icon
from PySide6.QtWidgets import (
    QLabel,
    QPushButton,
    QLineEdit,
    QComboBox,
    QListView,
    QStyledItemDelegate,
    QStyleOptionComboBox,
    QStyleOptionButton,
    QStyle,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QVBoxLayout,
    QScrollArea,
)


def label(text="", role=None):
    widget = QLabel(text)
    if role:
        widget.setObjectName(role)
    return widget


def value_label(field, role=None, wrap=False, display=str):
    widget = label(display(field.get()), role)
    widget.setWordWrap(wrap)
    field.subscribe(lambda: widget.setText(display(field.get())))
    return widget


def button(text, action, role=None):
    widget = QPushButton(text)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.clicked.connect(lambda checked=False: action())
    if role:
        widget.setObjectName(role)
    return widget


def glyph(name, size=20):
    widget = QLabel()
    widget.setFixedSize(size, size)
    widget.setPixmap(icon(name).pixmap(widget.size(), widget.devicePixelRatioF()))
    return widget


def segments(*buttons):
    rail = QFrame()
    rail.setObjectName("segmentRail")
    layout = QHBoxLayout(rail)
    layout.setContentsMargins(3, 3, 3, 3)
    layout.setSpacing(2)
    for item in buttons:
        layout.addWidget(item, 1)
    return rail


def divider(vertical=False):
    frame = QFrame()
    frame.setObjectName("divider")
    if vertical:
        frame.setFixedSize(1, 20)
    else:
        frame.setFixedHeight(1)
    return frame


class PresetButton(QPushButton):
    def __init__(self, number, name, target, speed, action):
        super().__init__()
        self.setObjectName("step")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setCheckable(True)
        self.clicked.connect(lambda checked: action(number))
        self.number = number
        self.index_label = label(str(number))
        self.index_label.setFixedSize(20, 20)
        self.index_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name_label = label()
        self.target_label = label()
        self.speed_label = label()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 8, 5, 9)
        layout.setSpacing(7)
        layout.addWidget(self.index_label, 0, Qt.AlignmentFlag.AlignTop)
        lines = QVBoxLayout()
        lines.setSpacing(4)
        lines.addWidget(self.name_label)
        lines.addWidget(self.target_label)
        lines.addWidget(self.speed_label)
        layout.addLayout(lines, 1)
        for item, role in (
            (self.index_label, "stepNumber"),
            (self.name_label, "stepName"),
            (self.target_label, "stepDetail"),
            (self.speed_label, "stepDetail"),
        ):
            item.setObjectName(role)
            item.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.set_step(name, target, speed)

    def set_step(self, name, target, speed):
        self.name_label.setText(name)
        self.target_label.setText(f"目标  {target} µm")
        self.speed_label.setText(f"速度  {speed:g} µm/s")
        self.setAccessibleName(f"{self.number} {name}，目标 {target} µm，速度 {speed:g} µm/s")
        self.setToolTip(self.accessibleName())

    def minimumSizeHint(self):
        return self.layout().minimumSize()

    def sizeHint(self):
        return self.layout().sizeHint()


def edit(field):
    widget = QLineEdit(str(field.get()))
    widget.textEdited.connect(field.set_from_user)

    def update():
        text = str(field.get())
        if widget.text() != text:
            widget.setText(text)

    field.subscribe(update)
    return widget


class ChoiceBox(QComboBox):
    """Share one arrow and popup style between editable ports and fixed choices."""

    def __init__(self):
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMaximumWidth(240)
        choices = QListView()
        choices.setObjectName("choiceList")
        choices.setMouseTracking(True)
        choices.setItemDelegate(QStyledItemDelegate(choices))
        self.setView(choices)

    def paintEvent(self, event):
        super().paintEvent(event)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        arrow = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxArrow,
            self,
        )
        center = QPointF(arrow.center())
        up = self.view().isVisible()
        offset = -2 if up else 2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(
            QPen(
                QColor("#475c79" if self.isEnabled() else "#8492a4"),
                1.6,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.drawPolyline(
            QPolygonF(
                [
                    center + QPointF(-4, -offset),
                    center + QPointF(0, offset),
                    center + QPointF(4, -offset),
                ]
            )
        )
        painter.end()

    def showPopup(self):
        super().showPopup()
        self.update()

    def hidePopup(self):
        super().hidePopup()
        self.update()


def combo(field, choices, editable=False):
    widget = ChoiceBox()
    widget.setEditable(editable)
    widget.addItems(list(choices))
    widget.setCurrentText(str(field.get()))
    widget.currentTextChanged.connect(field.set_from_user)

    def update():
        with QSignalBlocker(widget):
            widget.setCurrentText(str(field.get()))

    field.subscribe(update)
    return widget


class CheckBox(QCheckBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        if self.isChecked():
            option = QStyleOptionButton()
            self.initStyleOption(option)
            rect = self.style().subElementRect(
                QStyle.SubElement.SE_CheckBoxIndicator, option, self
            )
            center = QPointF(rect.center())
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(
                QPen(
                    QColor("white"),
                    1.8,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.drawPolyline(
                QPolygonF(
                    [
                        center + QPointF(-4, 0),
                        center + QPointF(-1, 3),
                        center + QPointF(5, -3),
                    ]
                )
            )
            painter.end()


def check(text, field, changed=None):
    widget = CheckBox(text)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setChecked(bool(field.get()))

    def toggle(value):
        field.set_from_user(value)
        if changed:
            changed()

    widget.toggled.connect(toggle)

    def update():
        with QSignalBlocker(widget):
            widget.setChecked(bool(field.get()))

    field.subscribe(update)
    return widget


def row(*items):
    layout = QHBoxLayout()
    layout.setSpacing(7)
    for item in items:
        if item is None:
            layout.addStretch()
        else:
            layout.addWidget(item)
    return layout


def card():
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 9, 12, 9)
    layout.setSpacing(6)
    return frame, layout


def scroll(widget):
    panel = QScrollArea()
    panel.setWidgetResizable(True)
    panel.setWidget(widget)
    return panel
