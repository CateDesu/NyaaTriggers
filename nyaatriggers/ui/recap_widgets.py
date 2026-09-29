"""Icons and compact combat rows for death recaps."""

from collections import OrderedDict
import json
import math
import time
from zipfile import BadZipFile, ZipFile

from PyQt6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QRect, QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QImageReader, QPainter, QPixmap
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkDiskCache, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem, QToolTip

from nyaatriggers import app_common as ac
from nyaatriggers.locale_util import _


class RecapIcons(QObject):
    changed = pyqtSignal()

    def __init__(self, directory, parent=None):
        super().__init__(parent)
        self.manager = QNetworkAccessManager(self)
        disk = QNetworkDiskCache(self.manager)
        disk.setCacheDirectory(str(directory))
        disk.setMaximumCacheSize(64 << 20)
        self.manager.setCache(disk)
        self.entries = OrderedDict()
        self._downloaded_metadata = OrderedDict()
        self.waiting = OrderedDict()
        self.active = set()
        self.failed = OrderedDict()
        self.archive = None
        self.catalog = {}
        try:
            self.archive = ZipFile(ac._ASSETS_DIR / "recap_icons.zip")
            if self.archive.getinfo("catalog.json").file_size > 16 << 20:
                raise ValueError("Icon catalog too large")
            catalog = json.loads(self.archive.read("catalog.json"))
            for sheet in ("Status", "Action"):
                rows = catalog[sheet]
                if not isinstance(rows, dict) or len(rows) > 100000:
                    raise ValueError("Invalid icon catalog")
                for ident, row in rows.items():
                    if (not ident.isdecimal() or not isinstance(row, dict)
                            or not isinstance(row.get("name"), str) or len(row["name"]) > 200
                            or type(row.get("icon")) is not int or not 0 <= row["icon"] < 1000000
                            or type(row.get("max_stacks", 0)) is not int
                            or not 0 <= row.get("max_stacks", 0) <= 255
                            or not isinstance(row.get("description", ""), str)
                            or len(row.get("description", "")) > 10000
                            or type(row.get("category", 0)) is not int
                            or not 0 <= row.get("category", 0) <= 2
                            or type(row.get("is_permanent", False)) is not bool):
                        raise ValueError("Invalid icon catalog row")
            if not isinstance(catalog.get("unavailable_icons", {}), dict):
                raise ValueError("Invalid missing icons")
            self.catalog = catalog
            self.destroyed.connect(self.archive.close)
        except (OSError, ValueError, KeyError, TypeError, BadZipFile):
            if self.archive:
                self.archive.close()
            self.archive = None
        self.placeholder = QPixmap(24, 24)
        self.placeholder.fill(Qt.GlobalColor.transparent)
        painter = QPainter(self.placeholder)
        painter.setPen(QColor("#a8a9b4"))
        painter.setBrush(QColor("#393b47"))
        painter.drawRoundedRect(1, 1, 21, 21, 3, 3)
        painter.drawText(self.placeholder.rect(), Qt.AlignmentFlag.AlignCenter, "?")
        painter.end()

    def get(self, sheet, ident, stacks=0):
        if sheet not in ("Action", "Status") or type(ident) is not int or not 0 < ident <= 0xFFFFFFFF:
            return {}, self.placeholder
        entry = self.catalog.get(sheet, {}).get(str(ident), {})
        limit = entry.get("max_stacks", 0) if entry else 65535
        stacks = stacks if type(stacks) is int and 1 <= stacks <= limit else 0
        key = (sheet, ident, stacks)
        if key in self.entries:
            self.entries.move_to_end(key)
            return self.entries[key]
        if entry and self.archive:
            metadata = {k: v for k, v in entry.items() if k != "icon"}
            icon = entry["icon"] + max(0, stacks - 1)
            if not icon or str(icon) in self.catalog.get("unavailable_icons", {}):
                return metadata, self.placeholder
            try:
                pixmap = self._picture(self.archive.read(f"{icon}.png"))
                self._remember(key, metadata, pixmap)
                return self.entries[key]
            except (OSError, ValueError, KeyError, BadZipFile):
                pass
        if (key not in self.active and key not in self.waiting
                and time.monotonic() >= self.failed.get(key, 0) and len(self.waiting) < 512):
            fields = "Name,Icon,MaxStacks,Description,StatusCategory,IsPermanent" if sheet == "Status" else "Name,Icon"
            self.waiting[key] = (f"https://v2.xivapi.com/api/sheet/{sheet}/{ident}?fields={fields}", None)
            self._pump()
        return self._downloaded_metadata.get((sheet, ident), {}), self.placeholder

    def metadata(self, sheet, ident):
        if sheet not in ("Action", "Status") or type(ident) is not int or not 0 < ident <= 0xFFFFFFFF:
            return {}
        entry = self.catalog.get(sheet, {}).get(str(ident))
        if entry is not None:
            return entry
        key = (sheet, ident)
        if key in self._downloaded_metadata:
            self._downloaded_metadata.move_to_end(key)
            return self._downloaded_metadata[key]
        return self.get(sheet, ident)[0]

    def _remember_metadata(self, sheet, ident, metadata):
        key = (sheet, ident)
        self._downloaded_metadata[key] = metadata
        self._downloaded_metadata.move_to_end(key)
        while len(self._downloaded_metadata) > 512:
            self._downloaded_metadata.popitem(last=False)

    def _remember(self, key, metadata, pixmap):
        self.entries[key] = (metadata, pixmap)
        while len(self.entries) > 512:
            self.entries.popitem(last=False)

    @staticmethod
    def _picture(payload):
        buffer = QBuffer()
        buffer.setData(QByteArray(payload))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer, b"png")
        size = reader.size()
        if not size.isValid() or max(size.width(), size.height()) > 512:
            raise ValueError("Invalid icon size")
        picture = reader.read()
        if picture.isNull():
            raise ValueError("Invalid icon image")
        return QPixmap.fromImage(picture)

    def _pump(self):
        while self.waiting and len(self.active) < 4:
            key, (url, metadata) = self.waiting.popitem(last=False)
            self.active.add(key)
            request = QNetworkRequest(QUrl(url))
            request.setTransferTimeout(15000)
            request.setRawHeader(b"User-Agent", b"NyaaTriggers death recap")
            reply = self.manager.get(request)
            reply.setReadBufferSize(512 * 1024 + 1)
            payload = bytearray()

            def read(reply=reply, payload=payload):
                payload.extend(bytes(reply.readAll()))
                if len(payload) > 512 * 1024 and reply.isRunning():
                    reply.abort()

            def finished(key=key, reply=reply, payload=payload, metadata=metadata, read=read):
                read()
                self.active.discard(key)
                try:
                    if reply.error() != QNetworkReply.NetworkError.NoError or len(payload) > 512 * 1024:
                        raise ValueError("Icon request failed")
                    if metadata is None:
                        fields = json.loads(payload)["fields"]
                        icon_id = fields["Icon"]["id"]
                        if type(icon_id) is not int or not 0 <= icon_id < 1000000:
                            raise ValueError("Invalid icon")
                        max_stacks = fields.get("MaxStacks", 0)
                        metadata = {"name": str(fields.get("Name", ""))[:200], "icon": icon_id,
                                    "max_stacks": max_stacks if type(max_stacks) is int and 0 <= max_stacks <= 255 else 0}
                        if type(max_stacks) is int and 1 <= key[2] <= max_stacks:
                            icon_id += key[2] - 1
                        if key[0] == "Status":
                            category = fields.get("StatusCategory", 0)
                            metadata.update(description=str(fields.get("Description", ""))[:10000],
                                            category=category if type(category) is int and 0 <= category <= 2 else 0,
                                            is_permanent=fields.get("IsPermanent") is True)
                        self._remember_metadata(key[0], key[1], metadata)
                        if metadata["icon"]:
                            path = f"ui/icon/{icon_id // 1000 * 1000:06d}/{icon_id:06d}.tex"
                            self.waiting[key] = (f"https://v2.xivapi.com/api/asset?path={path}&format=png", metadata)
                        else:
                            self._remember(key, metadata, self.placeholder)
                        self.changed.emit()
                    else:
                        self._remember(key, metadata, self._picture(payload))
                        self.changed.emit()
                except (ValueError, KeyError, TypeError, OverflowError):
                    self.failed[key] = time.monotonic() + 60
                    while len(self.failed) > 512:
                        self.failed.popitem(last=False)
                finally:
                    reply.deleteLater()
                    self._pump()

            reply.readyRead.connect(read)
            reply.finished.connect(finished)


def status_key(status):
    return str(status["id"]) if status.get("id") else "name:" + status["name"]


def status_name(status, metadata):
    name = status["name"]
    if not name or name.startswith(("Status ", "Unknown_", "_rsv_")):
        catalog_name = metadata.get("name", "")
        if catalog_name and not catalog_name.startswith("_rsv_"):
            return catalog_name
    return name


class RecapDelegate(QStyledItemDelegate):
    def __init__(self, icons, hidden, parent=None):
        super().__init__(parent)
        self.icons = icons
        self.hidden = hidden

    def statuses(self, event):
        statuses = event.get("statuses", []) + [s | {"on_source": True} for s in event.get("source_statuses", [])]
        return [s for s in statuses if status_key(s) not in self.hidden
                and self.icons.metadata("Status", s.get("id")).get("icon", 1)]

    def _event(self, index):
        return index.siblingAtColumn(0).data(Qt.ItemDataRole.UserRole) or {}

    def _status_rect(self, rect, position):
        columns = max(1, (rect.width() - 8) // 20)
        return QRect(rect.left() + 4 + position % columns * 20,
                     rect.top() + 3 + position // columns * 24, 18, 22)

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        if index.column() == 5:
            count = len(self.statuses(self._event(index)))
            columns = max(1, (option.rect.width() - 8) // 20)
            size.setHeight(max(30, math.ceil(count / columns) * 24 + 6))
        return size

    def paint(self, painter, option, index):
        event = self._event(index)
        column = index.column()
        opt = QStyleOptionViewItem(option)
        if column == 2:
            ident = event.get("action_id") or event.get("status_id")
            if ident:
                _metadata, pixmap = self.icons.get("Action" if event.get("action_id") else "Status", ident,
                                                   event.get("status_stacks", 0))
                self.initStyleOption(opt, index)
                opt.icon = QIcon(pixmap)
                opt.decorationSize = QSize(22, 22)
                opt.features |= QStyleOptionViewItem.ViewItemFeature.HasDecoration
                style = opt.widget.style()
                style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
                return
        super().paint(painter, opt, index)
        if column == 4 and event.get("hp") is not None and event.get("max_hp"):
            painter.save()
            rect = option.rect.adjusted(4, 5, -4, -5)
            painter.setClipRect(rect)
            painter.fillRect(rect, QColor("#30343b"))
            maximum, hp = event["max_hp"], event["hp"]
            width = rect.width()
            health = round(width * min(1, hp / maximum))
            painter.fillRect(QRect(rect.x(), rect.y(), health, rect.height()), QColor("#3c8714"))
            if event.get("hp_after") is not None and event["kind"] in ("heal", "hot"):
                restored = min(event["hp_after"], hp + (event.get("amount") or 0))
                end = round(width * min(1, restored / maximum))
                if end > health:
                    painter.fillRect(QRect(rect.x() + health, rect.y(), end - health, rect.height()), QColor("#80b759"))
            shield = (event.get("shield_after", event.get("shield")) if event["kind"] in ("heal", "hot")
                      else event.get("shield"))
            if shield:
                painter.fillRect(QRect(rect.x(), rect.bottom() - 3, round(width * min(100, shield) / 100), 4), QColor("#e5dd36"))
            painter.setPen(QColor("white"))
            painter.drawText(rect.adjusted(6, 0, -6, -2), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{hp:,}")
            painter.restore()
        elif column == 5:
            painter.save()
            painter.setClipRect(option.rect)
            for pos, status in enumerate(self.statuses(event)):
                _metadata, pixmap = self.icons.get("Status", status.get("id"), status.get("stacks"))
                painter.drawPixmap(self._status_rect(option.rect, pos), pixmap)
            painter.restore()

    def helpEvent(self, event, view, option, index):
        row = self._event(index)
        if index.column() == 2 and row.get("status_id"):
            metadata, _pixmap = self.icons.get("Status", row["status_id"], row.get("status_stacks", 0))
            text = status_name({"name": row["name"]}, metadata)
            if metadata.get("description"):
                text += "\n" + metadata["description"]
            if row.get("status_duration") is not None:
                text += "\n" + _("Duration: {seconds:.1f}s").format(seconds=row["status_duration"])
            if row.get("status_stacks"):
                text += "\n" + _("Stacks / value: {value}").format(value=row["status_stacks"])
            QToolTip.showText(event.globalPos(), text, view)
            return True
        if index.column() == 4 and row.get("hp") is not None:
            text = _("HP: {hp} / {maximum}").format(hp=f"{row['hp']:,}", maximum=f"{row.get('max_hp') or 0:,}")
            if row.get("shield") is not None:
                text += "\n" + _("Shields: approximately {percent}% of maximum HP").format(percent=row["shield"])
            if row.get("hp_after") is not None:
                text += "\n" + _("HP after resolution: {hp}").format(hp=f"{row['hp_after']:,}")
            if row.get("shield_after") is not None:
                text += "\n" + _("Shields after resolution: approximately {percent}%").format(percent=row["shield_after"])
            QToolTip.showText(event.globalPos(), text, view)
            return True
        if index.column() == 5:
            for pos, status in enumerate(self.statuses(row)):
                if self._status_rect(option.rect, pos).contains(event.pos()):
                    metadata, _pixmap = self.icons.get("Status", status.get("id"), status.get("stacks"))
                    name = status_name(status, metadata)
                    text = name
                    if status.get("on_source"):
                        text += "\n" + _("On attacker: {name}").format(name=row["source"])
                    if metadata.get("description"):
                        text += "\n" + metadata["description"]
                    if status["source"]:
                        text += "\n" + _("Applied by: {name}").format(name=status["source"])
                    if status.get("remaining") is not None:
                        text += "\n" + _("{seconds:.1f}s remaining").format(seconds=status["remaining"])
                    if status.get("stacks"):
                        text += "\n" + _("Stacks / value: {value}").format(value=status["stacks"])
                    QToolTip.showText(event.globalPos(), text, view)
                    return True
            QToolTip.hideText()
            return True
        return super().helpEvent(event, view, option, index)
