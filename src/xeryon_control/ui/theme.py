"""Shared instrument-console styling for the Qt Widgets application."""

from PySide6.QtGui import QFont

STYLE = """
QMainWindow, QWidget#root, QScrollArea, QWidget#page {background:#f1f4f9;}
QWidget {color:#1a2942;font-size:14px;}
QLabel {background:transparent;}
QFrame#card, QGroupBox {background:#fff;border:1px solid #d5dfea;border-radius:9px;}
QFrame#header {background:white;border:0;border-bottom:1px solid #cfdae8;}
QFrame#divider {background:#e3e9f1;border:0;}
QGroupBox {margin-top:16px;padding:12px 6px 4px;font-weight:600;}
QGroupBox::title {subcontrol-origin:margin;left:12px;}
QLabel#brand {font-family:"Segoe UI";font-size:25px;font-weight:700;color:#172840;}
QLabel#summaryValue {font-size:12px;font-weight:600;color:#253954;}
QLabel#metricTitle {font-size:12px;font-weight:700;color:#344862;}
QLabel#title {font-size:17px;font-weight:700;color:#192b45;}
QLabel#muted {font-size:12px;color:#52657b;}
QLabel#unit {font-family:"Segoe UI";font-size:23px;font-weight:600;color:#456497;}
QLabel#readout {font-family:"Segoe UI";font-size:55px;font-weight:650;color:#254de7;}
QLabel#readout[stale="true"] {color:#758297;}
QLabel#delta {color:#82521b;background:#fff2da;border:1px solid #f3deb7;border-radius:5px;padding:3px 6px;font-size:13px;}
QLabel#badge {color:#345281;background:#edf3fc;border-radius:5px;padding:4px 8px;font-size:12px;}
QLabel#connection {background:#edf1f6;color:#506176;border-radius:9px;padding:3px 9px;font-size:13px;}
QLabel#connection[online="true"] {background:#e2f5ed;color:#10684d;}
QPushButton {background:#f7f9fc;color:#253953;border:1px solid #d0dae6;border-radius:8px;padding:5px 12px;min-height:21px;font-weight:600;}
QPushButton:hover {background:#eaf0fa;border-color:#acbfd8;}
QPushButton:pressed {background:#dce6f4;border-color:#8ba3c6;}
QPushButton:checked {background:#e8efff;color:#2449c7;border-color:#879feb;}
QPushButton#quiet {background:transparent;border-color:transparent;color:#45607d;}
QPushButton#quiet:hover {background:#edf2f9;color:#233e60;}
QPushButton#quiet:pressed {background:#dfe8f5;}
QPushButton#connect {background:#eaf0ff;color:#274bbb;border-color:#c7d6fa;}
QPushButton#connect:hover {background:#dce7ff;border-color:#a2b7f4;}
QPushButton#connect:pressed {background:#cbdafa;}
QPushButton#nav {background:white;border:0;border-bottom:3px solid transparent;border-radius:0;padding:10px 16px;}
QPushButton#nav:hover {background:#f3f6fc;}
QPushButton#nav:checked {background:#eef2ff;color:#214aca;border-bottom-color:#3459ed;}
QFrame#segmentRail {background:#eef2f8;border:1px solid #dce4ef;border-radius:9px;}
QPushButton#segment {background:transparent;color:#485c75;border:1px solid transparent;border-radius:6px;padding:3px 9px;min-height:18px;}
QPushButton#segment:hover {background:#e0e7f3;color:#253d62;}
QPushButton#segment:checked {background:#3659df;color:white;border-color:#2f51d5;}
QPushButton#segment:pressed {background:#cfdcf5;}
QPushButton#segment:checked:pressed {background:#2446c4;}
QPushButton#step {background:#f7f9fd;border-color:#d6dfeb;border-radius:9px;}
QLabel#stepNumber {background:#e5ebf5;color:#476085;border-radius:6px;font-family:"Segoe UI";font-weight:700;font-size:12px;}
QLabel#stepName {font-weight:700;font-size:14px;}
QLabel#stepDetail {font-family:"Segoe UI";color:#40556e;font-size:12px;}
QPushButton#step:hover {background:#f0f4fd;border-color:#97ace4;}
QPushButton#step:checked {background:#eaf0ff;border-color:#6887ec;}
QPushButton#step:pressed {background:#e0e9ff;}
QPushButton#step[activeStep="true"] {background:#3458e6;border-color:#3458e6;}
QPushButton#step[activeStep="true"] QLabel {color:white;}
QPushButton#step[activeStep="true"] QLabel#stepNumber {background:#2542ae;}
QPushButton#primary, QPushButton#stop, QPushButton#localStop {font-weight:700;min-height:24px;border-radius:8px;}
QPushButton#primary, QPushButton#localStop {font-size:17px;min-height:31px;}
QPushButton#primary {background:#3254df;color:white;border-color:#2d50d7;}
QPushButton#primary:hover {background:#294bcc;}
QPushButton#primary:pressed {background:#203cac;}
QPushButton#stop {background:#bf2941;color:white;border-color:#b5223a;}
QPushButton#stop:hover {background:#aa2238;}
QPushButton#stop:pressed {background:#94192f;}
QPushButton#localStop {background:#fff1f2;color:#af263b;border-color:#ebbbc3;}
QPushButton#localStop:hover {background:#ffe4e8;border-color:#db91a0;}
QPushButton#localStop:pressed {background:#f9d2db;}
QLineEdit,QComboBox,QPlainTextEdit,QTableWidget {background:#fff;border:1px solid #c5d2e2;border-radius:7px;padding:4px 8px;selection-background-color:#d9e5ff;selection-color:#1b3f94;}
QComboBox {background:#f8faff;min-height:22px;padding:4px 26px 4px 9px;font-weight:600;combobox-popup:0;}
QComboBox:hover,QLineEdit:hover {border-color:#9bafcd;}
QComboBox::drop-down {subcontrol-origin:border;subcontrol-position:center right;width:25px;background:transparent;border:0;}
QComboBox::down-arrow {image:none;width:0;height:0;}
QComboBox QLineEdit {background:transparent;border:0;padding:0;}
QListView#choiceList {background:white;color:#26374e;border:1px solid #cbd7e8;border-radius:8px;outline:0;padding:4px;}
QListView#choiceList::item {min-height:24px;padding:3px 9px;border-radius:5px;}
QListView#choiceList::item:hover {background:#eff3fb;}
QListView#choiceList::item:selected {background:#e2ebff;color:#2346b7;}
QPushButton:focus,QPushButton#nav:focus,QPushButton#quiet:focus,QPushButton#connect:focus,QPushButton#step:focus,QPushButton#segment:focus,QPushButton#primary:focus,QPushButton#localStop:focus,QPushButton#stop:focus,QLineEdit:focus,QComboBox:focus,QComboBox:on {border:1px solid #5c7cf2;}
QPushButton:disabled,QPushButton#connect:disabled,QPushButton#quiet:disabled,QPushButton#segment:disabled,QPushButton#primary:disabled,QLineEdit:disabled,QComboBox:disabled {background:#eff3f8;color:#78879b;border-color:#e0e6ee;}
QLineEdit#targetInput {font-family:"Segoe UI";font-size:23px;font-weight:600;padding:2px 9px;}
QLineEdit#speedInput {font-family:"Segoe UI";font-size:18px;font-weight:600;padding:2px 7px;}
QCheckBox {spacing:7px;}
QCheckBox::indicator {width:15px;height:15px;border-radius:4px;border:1px solid #aebdd0;background:white;}
QCheckBox::indicator:checked {background:#375adf;border-color:#375adf;}
QCheckBox::indicator:disabled {background:#d9e1ee;border-color:#c6d2e1;}
QCheckBox::indicator:checked:disabled {background:#8e9fc1;}
QCheckBox::indicator:focus,QCheckBox::indicator:hover {border:1px solid #4c71ee;}
QTabWidget::pane {border:0;}
QTabBar::tab {background:transparent;padding:7px 16px;border-bottom:2px solid transparent;}
QTabBar::tab:selected {color:#294fc5;border-bottom-color:#416cf1;font-weight:650;}
QScrollArea {border:0;}
QSlider::groove:horizontal {background:#dce4f1;height:5px;border-radius:2px;}
QSlider::sub-page:horizontal {background:#5979e9;border-radius:2px;}
QSlider::handle:horizontal {background:white;border:2px solid #496ce5;border-radius:7px;width:11px;margin:-5px 0;}
QTabWidget#settingsTabs::pane {border:0;background:#f1f4f9;}
QTabWidget#settingsTabs QTabBar {background:white;}
QTabWidget#settingsTabs QTabBar::tab {font-weight:600;min-height:24px;margin:4px 2px;padding:5px 10px;border:1px solid transparent;border-radius:6px;color:#4e6079;}
QTabWidget#settingsTabs QTabBar::tab:selected {background:#e7edff;border-color:#ccd9fc;color:#294ac3;}
QTabWidget#settingsTabs QTabBar::tab:hover:!selected {background:#eff3f8;}
QLabel#settingLabel {color:#4e6079;}
QFrame#settingsMetrics {background:#f3f6fb;border:1px solid #e4eaf4;border-radius:7px;}
QLabel#settingValue {color:#203d67;font-weight:600;}
QPushButton#advancedStop {color:#aa2841;background:#fff2f4;border-color:#e6b9c1;}
QPushButton#advancedStop:hover {background:#ffe5eb;}
QPlainTextEdit#deviceLog {background:#122136;color:#d8e6f6;border:0;padding:12px;font-family:Consolas;}
QProgressBar {border:0;border-radius:5px;background:#e9eef8;text-align:center;min-height:15px;}
QProgressBar::chunk {background:#7394fa;border-radius:5px;}
"""


def apply_theme(app):
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(STYLE)
