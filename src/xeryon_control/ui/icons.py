"""Small high-DPI instrument and navigation icons painted in Qt."""

from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF


@lru_cache(maxsize=64)
def icon(name, color="#263958"):
    result = QIcon()
    for scale in (1, 2):
        pix = QPixmap(24 * scale, 24 * scale)
        pix.fill(Qt.GlobalColor.transparent)
        pix.setDevicePixelRatio(scale)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink = QColor(color)
        p.setPen(
            QPen(
                ink,
                1.65,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )

        def path(*points, filled=False):
            poly = QPolygonF([QPointF(x, y) for x, y in points])
            if filled:
                p.setBrush(ink)
                p.drawPolygon(poly)
            else:
                p.drawPolyline(poly)

        if name == "brand":
            p.setPen(Qt.PenStyle.NoPen)
            for fill, pts in (
                ("#5169ff", [(1, 15), (10, 3), (16, 3), (6, 18)]),
                ("#243fe9", [(12, 9), (16, 3), (23, 13), (18, 20)]),
                ("#8191ff", [(9, 21), (15, 13), (18, 18), (16, 21)]),
            ):
                ink = QColor(fill)
                path(*pts, filled=True)
        elif name == "console":
            p.drawRoundedRect(QRectF(3, 4, 18, 13), 2, 2)
            path((7, 14), (7, 10), (10, 10), (12, 7), (14, 12), (18, 12))
            path((12, 17), (12, 20))
            path((8, 20), (16, 20))
        elif name == "settings":
            for x, y in ((5, 9), (12, 15), (19, 7)):
                path((x, 3), (x, y - 2))
                path((x, y + 2), (x, 21))
                p.drawRoundedRect(QRectF(x - 2.5, y - 1.8, 5, 3.6), 1, 1)
        elif name == "position":
            p.drawEllipse(QPointF(12, 12), 7, 7)
            for pts in (
                ((12, 2), (12, 7)),
                ((12, 17), (12, 22)),
                ((2, 12), (7, 12)),
                ((17, 12), (22, 12)),
            ):
                path(*pts)
            p.setBrush(ink)
            p.drawEllipse(QPointF(12, 12), 1.4, 1.4)
        elif name == "refresh":
            p.drawArc(QRectF(4, 4, 16, 16), 32 * 16, 279 * 16)
            path((20, 3), (20, 8), (15, 8))
        elif name == "port":
            p.drawRoundedRect(QRectF(3, 6, 18, 12), 2, 2)
            path((6, 9), (6, 15))
            path((9, 9), (9, 15))
            path((13, 12), (17, 12))
        elif name == "sequence":
            for y in (5, 11, 17):
                path((3, y), (12, y + 4), (21, y))
            path((3, 5), (12, 1), (21, 5))
        elif name == "clock":
            p.drawEllipse(QPointF(12, 12), 8, 8)
            path((12, 7), (12, 12), (16, 14))
        elif name == "pulse":
            path((2, 13), (7, 13), (10, 6), (14, 18), (17, 11), (22, 11))
        elif name == "play":
            path((7, 4), (20, 12), (7, 20), filled=True)
        elif name == "stop":
            p.setBrush(ink)
            p.drawRoundedRect(QRectF(5, 5, 14, 14), 1.5, 1.5)
        elif name == "check":
            p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
            path((7.5, 12), (10.5, 15), (16.5, 9))
        elif name == "clear":
            path((4, 6), (20, 6))
            path((9, 4), (15, 4))
            path((6, 7), (7, 20), (17, 20), (18, 7))
        else:
            raise ValueError(name)
        p.end()
        result.addPixmap(pix)
    return result
