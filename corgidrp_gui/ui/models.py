"""Qt item models over the core dataclasses (files, groups)."""
from __future__ import annotations

from qtpy import QtCore, QtGui

from .. import theming
from ..core.parser import DISPLAY_KEYS, FileRecord, GroupAnalysis

Qt = QtCore.Qt

#: default visible header columns in the Parser's file table
FILE_COLUMNS = ["DATALVL", "VISTYPE", "DPAMNAME", "CFAMNAME", "FPAMNAME", "ISPC", "EXPTIME", "EMGAIN_C", "KGAINPAR", "TARGET"]
PathRole = Qt.ItemDataRole.UserRole + 1
GroupKeyRole = Qt.ItemDataRole.UserRole + 2
ObjectRole = Qt.ItemDataRole.UserRole + 3


def _status_color(status: str) -> QtGui.QColor | None:
    p = theming.current_theme().palette
    return {"ok": QtGui.QColor(p.ok), "warn": QtGui.QColor(p.warn), "err": QtGui.QColor(p.err),
            "incomplete": QtGui.QColor(p.err), "working": QtGui.QColor(p.secondary)}.get(status)


class FileTableModel(QtCore.QAbstractTableModel):
    def __init__(self, columns=None, parent=None):
        super().__init__(parent)
        self.columns = list(columns or FILE_COLUMNS)
        self.records: list[FileRecord] = []
        self._group_of: dict[str, tuple] = {}

    # -- data ---------------------------------------------------------------
    def set_records(self, records: list[FileRecord]):
        self.beginResetModel()
        self.records = list(records)
        self.endResetModel()

    def add_records(self, records: list[FileRecord]):
        have = {r.path for r in self.records}
        new = [r for r in records if r.path not in have]
        if not new:
            return
        self.beginInsertRows(QtCore.QModelIndex(), len(self.records), len(self.records) + len(new) - 1)
        self.records.extend(new)
        self.endInsertRows()

    def remove_rows(self, rows: list[int]):
        for r in sorted(set(rows), reverse=True):
            self.beginRemoveRows(QtCore.QModelIndex(), r, r)
            del self.records[r]
            self.endRemoveRows()

    def clear(self):
        self.set_records([])

    def set_group_keys(self, analyses: list[GroupAnalysis]):
        self._group_of = {f.path: a.group.override_key for a in analyses for f in a.group.files}
        if self.records:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.records) - 1, self.columnCount() - 1))

    def paths(self) -> list[str]:
        return [r.path for r in self.records]

    def rows_for_group(self, key: tuple) -> list[int]:
        return [i for i, r in enumerate(self.records) if self._group_of.get(r.path) == key]

    # -- model API ----------------------------------------------------------
    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.records)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 1 + len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return "File" if section == 0 else self.columns[section - 1]
        return section + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        rec = self.records[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return rec.name
            v = rec.value(self.columns[col - 1], "")
            return "" if v is None else str(v)
        if role == Qt.ItemDataRole.ToolTipRole:
            if not rec.ok:
                return f"{rec.path}\n{rec.headers.error}"
            return rec.path
        if role == Qt.ItemDataRole.ForegroundRole and not rec.ok:
            return QtGui.QColor(theming.current_theme().palette.err)
        if role == PathRole:
            return rec.path
        if role == GroupKeyRole:
            return self._group_of.get(rec.path)
        if role == ObjectRole:
            return rec
        if role == Qt.ItemDataRole.TextAlignmentRole and col > 0 and self.columns[col - 1] in ("ISPC", "EXPTIME", "EMGAIN_C", "KGAINPAR"):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


class GroupTableModel(QtCore.QAbstractTableModel):
    HEADERS = ["#", "Group", "Files", "Recipe / chain", "Status"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[GroupAnalysis] = []

    def set_rows(self, rows: list[GroupAnalysis]):
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def update_row(self, i: int, ga: GroupAnalysis):
        self.rows[i] = ga
        self.dataChanged.emit(self.index(i, 0), self.index(i, self.columnCount() - 1))

    def set_status(self, i: int, status: str, message: str = ""):
        self.rows[i].status = status
        if message:
            self.rows[i].message = message
        self.dataChanged.emit(self.index(i, 0), self.index(i, self.columnCount() - 1))

    def clear(self):
        self.set_rows([])

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        ga = self.rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return index.row() + 1
            if col == 1:
                return ga.group.label
            if col == 2:
                return len(ga.group.files)
            if col == 3:
                s = ga.recipe_summary
                return s + ("  (manual)" if ga.override else "")
            if col == 4:
                return {"pending": "…", "working": "resolving…"}.get(ga.status, ga.message or ga.status)
        if role == Qt.ItemDataRole.ToolTipRole:
            lines = [ga.recommendation.reason] if ga.recommendation and ga.recommendation.reason else []
            if ga.message:
                lines.append(ga.message)
            if ga.incomplete:
                lines += [f"  • {lab} for {st} ({rn})" for rn, st, lab in ga.missing_calibs]
                lines.append("Open it in the Recipe Builder to choose the files.")
            return "\n".join(lines) or None
        if role == Qt.ItemDataRole.ForegroundRole and col == 4:
            return _status_color(ga.status)
        if role == Qt.ItemDataRole.FontRole and col == 4 and ga.status in ("err", "warn", "incomplete"):
            f = QtGui.QFont()
            f.setBold(True)
            return f
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (0, 2):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == ObjectRole:
            return ga
        if role == GroupKeyRole:
            return ga.group.override_key
        return None
