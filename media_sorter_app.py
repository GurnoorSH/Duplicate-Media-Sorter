#!/usr/bin/env python3
"""PySide6 desktop interface for Duplicate Media Sorter."""

from __future__ import annotations

import math
import sys
import threading
from collections import defaultdict
from pathlib import Path
from string import Template

from PySide6.QtCore import QObject, QRunnable, QSettings, Qt, QThread, QThreadPool, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QRadioButton,
    QScrollArea,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from media_sorter import (
    EXACT_VIDEO_LANE,
    NEAR_VIDEO_LANE,
    PHOTO_LANE,
    RESTORABLE_STATUSES,
    ZERO_FILLED_LANE,
    DuplicateGroup,
    ManifestEntry,
    MediaItem,
    RunManifest,
    ScanCancelled,
    ScanProgress,
    ScanRequest,
    ScanResult,
    default_quarantine_dir,
    probe_vdf_tools,
    quarantine_groups,
    restore_entries,
    runs_dir,
    scan_library,
    thumbnail_path,
)


LANE_LABELS = {
    PHOTO_LANE: "Photos",
    EXACT_VIDEO_LANE: "Exact videos",
    NEAR_VIDEO_LANE: "Near videos",
    ZERO_FILLED_LANE: "Empty files",
}
# Review categories: (key, label, hint, predicate). Identical copies are
# pre-selected; anything that only looks alike starts unselected.
REVIEW_CATEGORIES = (
    ("photos_exact", "Identical photos", "Byte-for-byte copies. Pre-selected for quarantine.", lambda group: group.lane == PHOTO_LANE and group.exact),
    (
        "photos_similar",
        "Similar photos",
        "Look alike but differ: burst shots, edits, resized or re-sent copies. Nothing is selected until you choose.",
        lambda group: group.lane == PHOTO_LANE and not group.exact,
    ),
    ("videos_exact", "Exact videos", "Byte-for-byte copies. Pre-selected for quarantine.", lambda group: group.lane == EXACT_VIDEO_LANE),
    ("videos_near", "Near videos", "Re-encoded or trimmed versions. Nothing is selected until you choose.", lambda group: group.lane == NEAR_VIDEO_LANE),
    (
        "empty_files",
        "Empty files",
        "Photos and videos that are 0 bytes or entirely zero bytes: the data was never written (a failed copy or transfer). Nothing can be recovered from them. Pre-selected for quarantine.",
        lambda group: group.lane == ZERO_FILLED_LANE,
    ),
)
GROUPS_PER_PAGE = 20


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def clear_layout(layout: QVBoxLayout | QHBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()


def page_count(total: int) -> int:
    return max(1, math.ceil(total / GROUPS_PER_PAGE))


def default_selections(groups: list[DuplicateGroup]) -> dict[str, set[str]]:
    return {group.id: {str(item.path) for item in group.duplicates} if group.exact else set() for group in groups}


class Pager(QWidget):
    """Previous / next controls shared by the review and results pages."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.page = 0
        self.pages = 1
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.previous = QPushButton("Previous")
        self.previous.clicked.connect(lambda: self.go(self.page - 1))
        self.label = QLabel()
        self.label.setObjectName("hint")
        self.next = QPushButton("Next")
        self.next.clicked.connect(lambda: self.go(self.page + 1))
        layout.addWidget(self.previous)
        layout.addWidget(self.label)
        layout.addWidget(self.next)

    def reset(self, total: int, page: int = 0) -> None:
        self.pages = page_count(total)
        self.page = min(max(page, 0), self.pages - 1)
        self._refresh()

    def go(self, page: int) -> None:
        if 0 <= page < self.pages and page != self.page:
            self.page = page
            self._refresh()
            self.changed.emit()

    def bounds(self) -> tuple[int, int]:
        start = self.page * GROUPS_PER_PAGE
        return start, start + GROUPS_PER_PAGE

    def _refresh(self) -> None:
        self.label.setText(f"Page {self.page + 1} of {self.pages}")
        self.previous.setEnabled(self.page > 0)
        self.next.setEnabled(self.page < self.pages - 1)


class ThumbnailSignals(QObject):
    ready = Signal(str, str)


class ThumbnailTask(QRunnable):
    def __init__(self, item: MediaItem, size: tuple[int, int], signals: ThumbnailSignals) -> None:
        super().__init__()
        self.item = item
        self.size = size
        self.signals = signals

    def run(self) -> None:
        output = thumbnail_path(self.item, self.size)
        self.signals.ready.emit(str(self.item.path), str(output) if output else "")


class ScanWorker(QObject):
    progress = Signal(object)
    completed = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, request: ScanRequest) -> None:
        super().__init__()
        self.request = request
        self.cancel_event = threading.Event()

    @Slot()
    def run(self) -> None:
        try:
            result = scan_library(self.request, self.progress.emit, self.cancel_event)
        except ScanCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.completed.emit(result)

    def cancel(self) -> None:
        self.cancel_event.set()


class QuarantineWorker(QObject):
    progress = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, source_root: Path, quarantine_root: Path, groups: list[DuplicateGroup], selections: dict[str, set[str]]) -> None:
        super().__init__()
        self.source_root = source_root
        self.quarantine_root = quarantine_root
        self.groups = groups
        self.selections = selections

    @Slot()
    def run(self) -> None:
        try:
            manifest = quarantine_groups(
                self.source_root,
                self.quarantine_root,
                self.groups,
                self.selections,
                self.progress.emit,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.completed.emit(manifest)


class RestoreWorker(QObject):
    progress = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, manifest: RunManifest, entry_ids: set[str]) -> None:
        super().__init__()
        self.manifest = manifest
        self.entry_ids = entry_ids

    @Slot()
    def run(self) -> None:
        try:
            restore_entries(self.manifest, self.entry_ids, self.progress.emit)
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.completed.emit(self.manifest)


class StartPage(QWidget):
    scan_requested = Signal(object)
    manifest_requested = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._default_quarantine = ""
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(52, 44, 52, 44)
        layout.setSpacing(18)
        title = QLabel("Duplicate Media Sorter")
        title.setObjectName("heroTitle")
        subtitle = QLabel("Review duplicate photos and videos before anything moves.")
        subtitle.setObjectName("subtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(14)

        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(24, 24, 24, 24)
        card_layout.setSpacing(14)
        card_layout.addWidget(QLabel("Source library"))
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Choose the folder containing your photos and videos")
        self.source_edit.textChanged.connect(self._source_changed)
        card_layout.addLayout(self._path_row(self.source_edit, self._choose_source))
        card_layout.addWidget(QLabel("Quarantine folder"))
        self.quarantine_edit = QLineEdit()
        self.quarantine_edit.setPlaceholderText("A sibling quarantine folder will be suggested")
        card_layout.addLayout(self._path_row(self.quarantine_edit, self._choose_quarantine))

        self.near_checkbox = QCheckBox("Include optional near-video scan")
        tools = probe_vdf_tools()
        self.near_checkbox.setEnabled(tools.available)
        self.near_checkbox.setToolTip(tools.reason)
        card_layout.addWidget(self.near_checkbox)
        tool_status = QLabel(tools.reason)
        tool_status.setObjectName("hint")
        tool_status.setWordWrap(True)
        card_layout.addWidget(tool_status)

        buttons = QHBoxLayout()
        restore_button = QPushButton("Restore a Previous Run...")
        restore_button.setToolTip("Open a quarantine run manifest to review or restore its files.")
        restore_button.clicked.connect(self._choose_manifest)
        self.scan_button = QPushButton("Scan Library")
        self.scan_button.setObjectName("primaryButton")
        self.scan_button.clicked.connect(self._emit_request)
        buttons.addWidget(restore_button)
        buttons.addStretch()
        buttons.addWidget(self.scan_button)
        card_layout.addLayout(buttons)
        layout.addWidget(card)
        layout.addStretch()

    def _choose_manifest(self) -> None:
        start = Path(self.quarantine_edit.text().strip()) if self.quarantine_edit.text().strip() else Path.home()
        if runs_dir(start).is_dir():
            start = runs_dir(start)
        path, _filter = QFileDialog.getOpenFileName(self, "Open quarantine run", str(start), "Run manifests (*.json)")
        if path:
            self.manifest_requested.emit(Path(path))

    def _path_row(self, editor: QLineEdit, choose) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(editor)
        browse = QPushButton("Browse")
        browse.clicked.connect(choose)
        row.addWidget(browse)
        return row

    def _choose_source(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose source library", self.source_edit.text())
        if folder:
            self.source_edit.setText(folder)

    def _choose_quarantine(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose quarantine folder", self.quarantine_edit.text())
        if folder:
            self.quarantine_edit.setText(folder)

    def _source_changed(self, raw_path: str) -> None:
        if not raw_path:
            return
        source = Path(raw_path)
        suggested = str(default_quarantine_dir(source))
        if not self.quarantine_edit.text() or self.quarantine_edit.text() == self._default_quarantine:
            self.quarantine_edit.setText(suggested)
        self._default_quarantine = suggested

    def _emit_request(self) -> None:
        source = Path(self.source_edit.text().strip())
        if not source.is_dir():
            QMessageBox.warning(self, "Choose a library", "Select an existing source folder first.")
            return
        quarantine = Path(self.quarantine_edit.text().strip()) if self.quarantine_edit.text().strip() else default_quarantine_dir(source)
        self.scan_requested.emit(
            ScanRequest(
                source_root=source,
                quarantine_root=quarantine,
                include_near_videos=self.near_checkbox.isChecked(),
            )
        )


class ProgressPage(QWidget):
    cancel_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(52, 44, 52, 44)
        layout.setSpacing(14)
        self.title = QLabel("Scanning your library")
        self.title.setObjectName("heroTitle")
        self.message = QLabel("Preparing scan")
        self.message.setObjectName("subtitle")
        self.message.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setPlaceholderText("Scan details will appear here.")
        # Large libraries emit tens of thousands of progress lines; keep the log bounded.
        self.logs.document().setMaximumBlockCount(3000)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_requested)
        layout.addWidget(self.title)
        layout.addWidget(self.message)
        layout.addWidget(self.bar)
        layout.addWidget(self.logs, 1)
        layout.addWidget(self.cancel_button, alignment=Qt.AlignmentFlag.AlignRight)

    def reset(self, title: str, cancellable: bool) -> None:
        self.title.setText(title)
        self.message.setText("Preparing")
        self.bar.setRange(0, 0)
        self.logs.clear()
        self.cancel_button.setVisible(cancellable)

    def update_progress(self, progress: ScanProgress) -> None:
        self.message.setText(progress.message)
        if progress.percent is None:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(progress.percent)
        counted = progress.current is not None and progress.total
        if progress.warning or not counted or progress.current % 250 == 0 or progress.current == progress.total:
            prefix = "Warning: " if progress.warning else ""
            self.logs.append(prefix + progress.message)


THUMBNAIL_SIZE = (190, 122)


class MediaTile(QFrame):
    preview_requested = Signal(object, str)

    def __init__(self, item, lane: str, keeper: bool, selected: bool) -> None:
        super().__init__()
        self.item = item
        self.lane = lane
        self.setObjectName("mediaTile")
        self.setMinimumWidth(210)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)
        self.preview = QLabel()
        self.preview.setFixedSize(*THUMBNAIL_SIZE)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setObjectName("preview")
        if lane == ZERO_FILLED_LANE:
            self.preview.setText("No data\n(empty or all zero bytes)")
        elif lane == PHOTO_LANE:
            self.preview.setText("Loading preview...")
        else:
            self.preview.setText("Video")
            self.preview.setCursor(Qt.CursorShape.PointingHandCursor)
            self.preview.mousePressEvent = lambda _event: self.preview_requested.emit(self.item.path, self.lane)  # type: ignore[method-assign]
        layout.addWidget(self.preview)
        name = QLabel(item.path.name)
        name.setToolTip(str(item.path))
        name.setWordWrap(True)
        layout.addWidget(name)
        dimensions = f"  |  {item.width} x {item.height}" if item.width and item.height else ""
        metadata = QLabel(f"{format_bytes(item.size)}{dimensions}")
        metadata.setObjectName("hint")
        layout.addWidget(metadata)
        self.keeper_radio = QRadioButton("Keep as original")
        self.keeper_radio.setChecked(keeper)
        self.move_checkbox = QCheckBox("Move to quarantine")
        self.move_checkbox.setChecked(selected and not keeper)
        self.move_checkbox.setEnabled(not keeper)
        layout.addWidget(self.keeper_radio)
        layout.addWidget(self.move_checkbox)
        # An empty file has nothing worth keeping, so there is no keeper choice.
        self.keeper_radio.setVisible(lane != ZERO_FILLED_LANE)
        if lane not in (PHOTO_LANE, ZERO_FILLED_LANE):
            play = QPushButton("Preview video")
            play.clicked.connect(lambda: self.preview_requested.emit(self.item.path, self.lane))
            layout.addWidget(play)

    def set_thumbnail(self, path: str) -> None:
        pixmap = QPixmap(path) if path else QPixmap()
        if pixmap.isNull():
            self.preview.setText("Preview unavailable")
            return
        self.preview.setPixmap(pixmap.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))


class GroupCard(QFrame):
    selection_changed = Signal(str, object)
    preview_requested = Signal(object, str)

    def __init__(self, group: DuplicateGroup, selected: set[str]) -> None:
        super().__init__()
        self.group = group
        self.selected = set(selected)
        self.tiles: dict[str, MediaTile] = {}
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        if group.lane == ZERO_FILLED_LANE:
            title = QLabel("Empty file")
            kind = QLabel("No data, nothing to keep")
            kind.setObjectName("pillWarn")
        else:
            title = QLabel(f"{len(group.items)} matching files")
            kind = QLabel("Identical copies" if group.exact else "Similar, review before moving")
            kind.setObjectName("pill" if group.exact else "pillWarn")
        title.setObjectName("groupTitle")
        reclaim = QLabel(f"{format_bytes(group.reclaimable_bytes)} reclaimable")
        reclaim.setObjectName("pill")
        heading.addWidget(title)
        heading.addWidget(kind)
        heading.addStretch()
        heading.addWidget(reclaim)
        layout.addLayout(heading)
        tile_row = QHBoxLayout()
        tile_row.setSpacing(10)
        radio_group = QButtonGroup(self)
        radio_group.setExclusive(True)
        for item in group.items:
            path = str(item.path)
            tile = MediaTile(item, group.lane, path == str(group.keeper_path), path in self.selected)
            tile.preview_requested.connect(self.preview_requested)
            tile.keeper_radio.toggled.connect(lambda checked, chosen=item.path: self._keeper_changed(chosen, checked))
            tile.move_checkbox.toggled.connect(lambda checked, chosen=path: self._selection_toggled(chosen, checked))
            radio_group.addButton(tile.keeper_radio)
            tile_row.addWidget(tile)
            self.tiles[path] = tile
        tile_row.addStretch()
        layout.addLayout(tile_row)

    def _keeper_changed(self, path: Path, checked: bool) -> None:
        if not checked:
            return
        previous = str(self.group.keeper_path)
        chosen = str(path)
        self.group.set_keeper(path)
        # Swap roles: the old keeper is only marked to move if the new keeper
        # was. Similar groups start unselected and should stay that way.
        was_selected = chosen in self.selected
        self.selected.discard(chosen)
        if previous != chosen and was_selected:
            self.selected.add(previous)
        for tile_path, tile in self.tiles.items():
            is_keeper = tile_path == chosen
            tile.move_checkbox.blockSignals(True)
            tile.move_checkbox.setEnabled(not is_keeper)
            tile.move_checkbox.setChecked(tile_path in self.selected and not is_keeper)
            tile.move_checkbox.blockSignals(False)
        self.selection_changed.emit(self.group.id, set(self.selected))

    def _selection_toggled(self, path: str, checked: bool) -> None:
        if checked:
            self.selected.add(path)
        else:
            self.selected.discard(path)
        self.selection_changed.emit(self.group.id, set(self.selected))


class ReviewPage(QWidget):
    quarantine_requested = Signal(object)
    back_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.result: ScanResult | None = None
        self.selections: dict[str, set[str]] = {}
        self.category = REVIEW_CATEGORIES[0][0]
        self.visible_tiles: dict[str, list[MediaTile]] = defaultdict(list)
        self.thumbnail_pool = QThreadPool(self)
        self.thumbnail_pool.setMaxThreadCount(4)
        self.thumbnail_signals = ThumbnailSignals()
        self.thumbnail_signals.ready.connect(self._thumbnail_ready)
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        header = QHBoxLayout()
        title = QLabel("Review duplicates")
        title.setObjectName("heroTitle")
        self.summary = QLabel()
        self.summary.setObjectName("subtitle")
        back = QPushButton("New Scan")
        back.clicked.connect(self.back_requested)
        quarantine = QPushButton("Quarantine Selected")
        quarantine.setObjectName("primaryButton")
        quarantine.clicked.connect(lambda: self.quarantine_requested.emit(self.selections))
        header.addWidget(title)
        header.addWidget(self.summary)
        header.addStretch()
        header.addWidget(back)
        header.addWidget(quarantine)
        outer.addLayout(header)

        content = QHBoxLayout()
        self.lanes = QListWidget()
        self.lanes.setFixedWidth(200)
        self.lanes.currentItemChanged.connect(self._lane_changed)
        content.addWidget(self.lanes)

        middle = QVBoxLayout()
        self.category_hint = QLabel()
        self.category_hint.setObjectName("hint")
        self.category_hint.setWordWrap(True)
        middle.addWidget(self.category_hint)
        toolbar = QHBoxLayout()
        select_all = QPushButton("Select All in Category")
        select_all.clicked.connect(lambda: self._select_category(True))
        clear_all = QPushButton("Clear Category")
        clear_all.clicked.connect(lambda: self._select_category(False))
        self.pager = Pager()
        self.pager.changed.connect(self._render_page)
        toolbar.addWidget(select_all)
        toolbar.addWidget(clear_all)
        toolbar.addStretch()
        toolbar.addWidget(self.pager)
        middle.addLayout(toolbar)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.group_host = QWidget()
        self.group_layout = QVBoxLayout(self.group_host)
        self.group_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.group_host)
        middle.addWidget(self.scroll, 1)
        content.addLayout(middle, 1)
        content.addWidget(self._preview_panel())
        outer.addLayout(content, 1)

    def _preview_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("card")
        panel.setFixedWidth(300)
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("Video preview"))
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumHeight(210)
        self.video_widget.setStyleSheet("background: #111827; border-radius: 8px;")
        layout.addWidget(self.video_widget)
        self.preview_name = QLabel("Choose a video to preview.")
        self.preview_name.setObjectName("hint")
        self.preview_name.setWordWrap(True)
        layout.addWidget(self.preview_name)
        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video_widget)
        play = QPushButton("Play / pause")
        play.clicked.connect(self._toggle_play)
        layout.addWidget(play)
        layout.addStretch()
        return panel

    def set_result(self, result: ScanResult) -> None:
        self.result = result
        self.selections = default_selections(result.groups)
        self.lanes.blockSignals(True)
        self.lanes.clear()
        for key, label, _hint, predicate in REVIEW_CATEGORIES:
            count = sum(1 for group in result.groups if predicate(group))
            item = QListWidgetItem(f"{label}  {count}")
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.lanes.addItem(item)
        self.lanes.setCurrentRow(0)
        self.lanes.blockSignals(False)
        self._lane_changed(self.lanes.currentItem())
        self._update_summary()

    def category_groups(self) -> list[DuplicateGroup]:
        if self.result is None:
            return []
        predicate = next(entry[3] for entry in REVIEW_CATEGORIES if entry[0] == self.category)
        return [group for group in self.result.groups if predicate(group)]

    def _lane_changed(self, current: QListWidgetItem | None) -> None:
        if self.result is None or current is None:
            return
        self.category = current.data(Qt.ItemDataRole.UserRole)
        self.category_hint.setText(next(entry[2] for entry in REVIEW_CATEGORIES if entry[0] == self.category))
        self.pager.reset(len(self.category_groups()))
        self._render_page()

    def _render_page(self) -> None:
        self.thumbnail_pool.clear()
        self.visible_tiles.clear()
        clear_layout(self.group_layout)
        groups = self.category_groups()
        if not groups:
            empty = QLabel("No duplicate groups in this category.")
            empty.setObjectName("subtitle")
            self.group_layout.addWidget(empty)
            return
        start, end = self.pager.bounds()
        for group in groups[start:end]:
            card = GroupCard(group, self.selections[group.id])
            card.selection_changed.connect(self._selection_changed)
            card.preview_requested.connect(self.preview_video)
            self.group_layout.addWidget(card)
            if group.lane == PHOTO_LANE:
                for tile in card.tiles.values():
                    self.visible_tiles[str(tile.item.path)].append(tile)
                    self.thumbnail_pool.start(ThumbnailTask(tile.item, THUMBNAIL_SIZE, self.thumbnail_signals))
        self.scroll.verticalScrollBar().setValue(0)

    def _thumbnail_ready(self, item_path: str, thumbnail: str) -> None:
        for tile in self.visible_tiles.get(item_path, []):
            tile.set_thumbnail(thumbnail)

    def _select_category(self, selected: bool) -> None:
        for group in self.category_groups():
            self.selections[group.id] = {str(item.path) for item in group.duplicates} if selected else set()
        self._render_page()
        self._update_summary()

    def _selection_changed(self, group_id: str, selected: set[str]) -> None:
        self.selections[group_id] = selected
        self._update_summary()

    def _update_summary(self) -> None:
        if self.result is None:
            return
        files = 0
        size = 0
        for group in self.result.groups:
            chosen = self.selections.get(group.id, set())
            for item in group.items:
                if str(item.path) in chosen:
                    files += 1
                    size += item.size
        text = f"{len(self.result.groups)} groups  |  {files} files selected ({format_bytes(size)})"
        if self.result.warnings:
            text += f"  |  {len(self.result.warnings)} files could not be read"
        self.summary.setText(text)

    def preview_video(self, path: Path, _lane: str) -> None:
        self.preview_name.setText(str(path))
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def release_preview(self) -> None:
        """Unload the previewed video; a loaded source keeps the file open,
        which stops Windows from moving it."""
        self.player.stop()
        self.player.setSource(QUrl())
        self.preview_name.setText("Choose a video to preview.")

    def _toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()


class ManifestGroupCard(QFrame):
    restore_group = Signal(object)
    check_changed = Signal(str, bool)

    def __init__(self, group_id: str, entries: list[ManifestEntry], checked: set[str]) -> None:
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel(f"{LANE_LABELS.get(entries[0].lane, entries[0].lane)} group"))
        header.addStretch()
        restore = QPushButton("Restore Group")
        restore.clicked.connect(lambda: self.restore_group.emit({entry.id for entry in entries if entry.status in RESTORABLE_STATUSES}))
        header.addWidget(restore)
        layout.addLayout(header)
        kept = entries[0].keeper_path
        keeper = QLabel(f"Original kept: {kept}" if kept else "Empty or zero-filled file: there was no data to keep.")
        keeper.setWordWrap(True)
        keeper.setObjectName("hint")
        layout.addWidget(keeper)
        for entry in entries:
            location = entry.moved_path or entry.original_path
            checkbox = QCheckBox(f"{entry.status}: {location}")
            tooltip = f"Restore to: {entry.original_path}"
            if entry.error:
                tooltip += f"\n{entry.error}"
            checkbox.setToolTip(tooltip)
            checkbox.setEnabled(entry.status in RESTORABLE_STATUSES)
            checkbox.setChecked(entry.id in checked)
            checkbox.toggled.connect(lambda state, entry_id=entry.id: self.check_changed.emit(entry_id, state))
            layout.addWidget(checkbox)


class ResultsPage(QWidget):
    restore_requested = Signal(object)
    open_folder_requested = Signal()
    new_scan_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.manifest: RunManifest | None = None
        self.groups: list[tuple[str, list[ManifestEntry]]] = []
        self.checked: set[str] = set()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        header = QHBoxLayout()
        title = QLabel("Quarantined groups")
        title.setObjectName("heroTitle")
        self.summary = QLabel()
        self.summary.setObjectName("subtitle")
        new_scan = QPushButton("New Scan")
        new_scan.clicked.connect(self.new_scan_requested)
        open_folder = QPushButton("Open Folder")
        open_folder.clicked.connect(self.open_folder_requested)
        restore_all = QPushButton("Restore All")
        restore_all.clicked.connect(self._restore_all)
        restore = QPushButton("Restore Selected")
        restore.clicked.connect(lambda: self.restore_requested.emit(set(self.checked)))
        header.addWidget(title)
        header.addWidget(self.summary)
        header.addStretch()
        header.addWidget(new_scan)
        header.addWidget(open_folder)
        header.addWidget(restore_all)
        header.addWidget(restore)
        outer.addLayout(header)
        self.pager = Pager()
        self.pager.changed.connect(self._render_page)
        outer.addWidget(self.pager, alignment=Qt.AlignmentFlag.AlignRight)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        host = QWidget()
        self.card_layout = QVBoxLayout(host)
        self.card_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(host)
        outer.addWidget(self.scroll, 1)

    def set_manifest(self, manifest: RunManifest) -> None:
        self.manifest = manifest
        self.checked.clear()
        statuses = [entry.status for entry in manifest.entries]
        parts = [f"{statuses.count('moved')} files currently quarantined"]
        for status in ("restored", "skipped", "error"):
            if statuses.count(status):
                parts.append(f"{statuses.count(status)} {status}")
        self.summary.setText("  |  ".join(parts))
        grouped: dict[str, list[ManifestEntry]] = defaultdict(list)
        for entry in manifest.entries:
            grouped[entry.group_id].append(entry)
        self.groups = list(grouped.items())
        self.pager.reset(len(self.groups), self.pager.page)
        self._render_page()

    def _render_page(self) -> None:
        clear_layout(self.card_layout)
        if not self.groups:
            self.card_layout.addWidget(QLabel("No files were moved."))
            return
        start, end = self.pager.bounds()
        for group_id, entries in self.groups[start:end]:
            card = ManifestGroupCard(group_id, entries, self.checked)
            card.restore_group.connect(self.restore_requested)
            card.check_changed.connect(self._check_changed)
            self.card_layout.addWidget(card)
        self.scroll.verticalScrollBar().setValue(0)

    def _check_changed(self, entry_id: str, checked: bool) -> None:
        if checked:
            self.checked.add(entry_id)
        else:
            self.checked.discard(entry_id)

    def _restore_all(self) -> None:
        if self.manifest is not None:
            self.restore_requested.emit({entry.id for entry in self.manifest.entries if entry.status in RESTORABLE_STATUSES})


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Duplicate Media Sorter")
        self.resize(1320, 820)
        self.settings = QSettings("DuplicateMediaSorter", "DuplicateMediaSorter")
        self.dark_mode = self.settings.value("darkMode", False, type=bool)
        self.pages = QStackedWidget()
        self.start_page = StartPage()
        self.progress_page = ProgressPage()
        self.review_page = ReviewPage()
        self.results_page = ResultsPage()
        for page in (self.start_page, self.progress_page, self.review_page, self.results_page):
            self.pages.addWidget(page)
        self.setCentralWidget(self.pages)
        self.thread: QThread | None = None
        self.worker: QObject | None = None
        self.scan_result: ScanResult | None = None
        self.manifest: RunManifest | None = None
        self.restoring_ids: set[str] = set()
        self.start_page.scan_requested.connect(self.start_scan)
        self.start_page.manifest_requested.connect(self.open_manifest)
        self.progress_page.cancel_requested.connect(self.cancel_scan)
        self.review_page.quarantine_requested.connect(self.start_quarantine)
        self.review_page.back_requested.connect(self.show_start)
        self.results_page.restore_requested.connect(self.restore)
        self.results_page.open_folder_requested.connect(self.open_quarantine_folder)
        self.results_page.new_scan_requested.connect(self.show_start)
        self._build_theme_menu()
        self.apply_theme()

    def _build_theme_menu(self) -> None:
        view_menu = self.menuBar().addMenu("&View")
        self.dark_mode_action = QAction("Dark mode", self)
        self.dark_mode_action.setCheckable(True)
        self.dark_mode_action.setChecked(self.dark_mode)
        self.dark_mode_action.toggled.connect(self.set_dark_mode)
        view_menu.addAction(self.dark_mode_action)

    def set_dark_mode(self, enabled: bool) -> None:
        self.dark_mode = enabled
        self.settings.setValue("darkMode", enabled)
        self.apply_theme()

    def apply_theme(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(stylesheet_for_theme("dark" if self.dark_mode else "light"))

    def start_scan(self, request: ScanRequest) -> None:
        self.progress_page.reset("Scanning your library", cancellable=True)
        self.pages.setCurrentWidget(self.progress_page)
        worker = ScanWorker(request)
        worker.completed.connect(self.scan_completed)
        worker.failed.connect(self.operation_failed)
        worker.cancelled.connect(self.scan_cancelled)
        self._start_worker(worker)

    def start_quarantine(self, selections: dict[str, set[str]]) -> None:
        assert self.scan_result is not None
        selected_count = sum(len(values) for values in selections.values())
        if selected_count == 0:
            QMessageBox.information(self, "Nothing selected", "Select at least one duplicate to quarantine.")
            return
        response = QMessageBox.question(
            self,
            "Quarantine selected files?",
            f"Move {selected_count} selected duplicates into the quarantine folder? You can restore them later.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        self.review_page.release_preview()
        request = self.scan_result.request
        self.progress_page.reset("Moving duplicates to quarantine", cancellable=False)
        self.pages.setCurrentWidget(self.progress_page)
        worker = QuarantineWorker(request.source_root, request.effective_quarantine_root, self.scan_result.groups, selections)
        worker.completed.connect(self.quarantine_completed)
        worker.failed.connect(self.operation_failed)
        self._start_worker(worker)

    def _start_worker(self, worker: QObject) -> None:
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.progress.connect(self.progress_page.update_progress)  # type: ignore[attr-defined]
        thread.started.connect(worker.run)  # type: ignore[attr-defined]
        thread.finished.connect(thread.deleteLater)
        self.thread = thread
        self.worker = worker
        thread.start()

    def _finish_worker(self) -> None:
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait()
        self.thread = None
        self.worker = None

    def scan_completed(self, result: ScanResult) -> None:
        self._finish_worker()
        self.scan_result = result
        self.review_page.set_result(result)
        self.pages.setCurrentWidget(self.review_page)

    def quarantine_completed(self, manifest: RunManifest) -> None:
        self._finish_worker()
        self.manifest = manifest
        self.results_page.set_manifest(manifest)
        self.pages.setCurrentWidget(self.results_page)

    def operation_failed(self, message: str) -> None:
        self._finish_worker()
        QMessageBox.critical(self, "Operation failed", message)
        self.pages.setCurrentWidget(self.start_page)

    def cancel_scan(self) -> None:
        if isinstance(self.worker, ScanWorker):
            self.worker.cancel()
            self.progress_page.message.setText("Cancelling scan...")

    def scan_cancelled(self) -> None:
        self._finish_worker()
        self.pages.setCurrentWidget(self.start_page)

    def show_start(self) -> None:
        self.review_page.release_preview()
        self.pages.setCurrentWidget(self.start_page)

    def open_manifest(self, path: Path) -> None:
        try:
            manifest = RunManifest.load(path)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            QMessageBox.critical(self, "Could not open run", f"{path}\n\n{exc}")
            return
        self.manifest = manifest
        self.results_page.set_manifest(manifest)
        self.pages.setCurrentWidget(self.results_page)

    def restore(self, entry_ids: set[str]) -> None:
        if self.manifest is None or not entry_ids:
            return
        response = QMessageBox.question(self, "Restore selected files?", f"Restore {len(entry_ids)} selected files to their original paths?")
        if response != QMessageBox.StandardButton.Yes:
            return
        self.progress_page.reset("Restoring files", cancellable=False)
        self.pages.setCurrentWidget(self.progress_page)
        self.restoring_ids = set(entry_ids)
        worker = RestoreWorker(self.manifest, entry_ids)
        # Bound methods (not lambdas) so Qt queues the call onto the GUI thread.
        worker.completed.connect(self.restore_completed)
        worker.failed.connect(self.operation_failed)
        self._start_worker(worker)

    def restore_completed(self, manifest: RunManifest) -> None:
        self._finish_worker()
        self.results_page.set_manifest(manifest)
        self.pages.setCurrentWidget(self.results_page)
        blocked = [
            entry.error
            for entry in manifest.entries
            if entry.id in self.restoring_ids and entry.status in ("restore-blocked", "restore-error", "missing")
        ]
        if blocked:
            shown = "\n".join(error for error in blocked[:20] if error)
            more = f"\n...and {len(blocked) - 20} more" if len(blocked) > 20 else ""
            QMessageBox.warning(self, f"{len(blocked)} files could not be restored", shown + more)

    def open_quarantine_folder(self) -> None:
        if self.manifest is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.manifest.quarantine_root))

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if isinstance(self.worker, ScanWorker):
            self.worker.cancel()
            self.progress_page.message.setText("Cancelling scan...")
            event.ignore()
            return
        if isinstance(self.worker, (QuarantineWorker, RestoreWorker)):
            QMessageBox.information(self, "Files are moving", "Wait for the current move to finish before closing the app.")
            event.ignore()
            return
        self.review_page.release_preview()
        super().closeEvent(event)


THEMES = {
    "light": {
        "app_bg": "#f6f7f9",
        "text": "#172033",
        "surface": "#ffffff",
        "surface_alt": "#eef2f6",
        "border": "#e3e8ef",
        "field_border": "#d8dee8",
        "muted": "#667085",
        "accent": "#3567e8",
        "accent_hover": "#4d76e8",
        "accent_soft": "#eef4ff",
        "accent_text": "#2457d6",
        "button_border": "#cbd3df",
        "selection_bg": "#dbe7ff",
        "selection_text": "#172033",
        "disabled_bg": "#edf0f4",
        "disabled_text": "#98a2b3",
        "menu_hover": "#eef4ff",
        "tooltip_bg": "#172033",
        "tooltip_text": "#ffffff",
        "warn_soft": "#fff4e5",
        "warn_text": "#b54708",
    },
    "dark": {
        "app_bg": "#101622",
        "text": "#e6edf7",
        "surface": "#172033",
        "surface_alt": "#202b3d",
        "border": "#2f3d52",
        "field_border": "#3a4a61",
        "muted": "#a8b3c4",
        "accent": "#6c8cff",
        "accent_hover": "#8ea6ff",
        "accent_soft": "#1f315f",
        "accent_text": "#c4d2ff",
        "button_border": "#4a5d78",
        "selection_bg": "#263f75",
        "selection_text": "#f8fbff",
        "disabled_bg": "#1d2634",
        "disabled_text": "#6f7f95",
        "menu_hover": "#22304a",
        "tooltip_bg": "#e6edf7",
        "tooltip_text": "#101622",
        "warn_soft": "#3d2a12",
        "warn_text": "#fdb022",
    },
}


def stylesheet_for_theme(theme_name: str) -> str:
    theme = THEMES[theme_name]
    return Template("""
QWidget {
    background: $app_bg;
    color: $text;
    font-size: 14px;
}
QFrame#card, QFrame#mediaTile {
    background: $surface;
    border: 1px solid $border;
    border-radius: 12px;
}
QFrame#mediaTile {
    border-radius: 9px;
}
QLabel#heroTitle {
    font-size: 26px;
    font-weight: 650;
}
QLabel#subtitle, QLabel#hint {
    color: $muted;
}
QLabel#groupTitle {
    font-size: 16px;
    font-weight: 600;
}
QLabel#pill {
    color: $accent_text;
    background: $accent_soft;
    border-radius: 8px;
    padding: 4px 8px;
}
QLabel#pillWarn {
    color: $warn_text;
    background: $warn_soft;
    border-radius: 8px;
    padding: 4px 8px;
}
QLabel#preview {
    color: $muted;
    background: $surface_alt;
    border-radius: 7px;
}
QLineEdit, QTextEdit, QListWidget, QScrollArea, QMenu {
    background: $surface;
    border: 1px solid $field_border;
    border-radius: 8px;
    padding: 8px;
}
QLineEdit:disabled, QTextEdit:disabled, QListWidget:disabled {
    background: $disabled_bg;
    color: $disabled_text;
}
QListWidget::item {
    border-radius: 6px;
    padding: 6px;
}
QListWidget::item:selected {
    background: $selection_bg;
    color: $selection_text;
}
QPushButton {
    background: $surface;
    border: 1px solid $button_border;
    border-radius: 8px;
    padding: 8px 14px;
}
QPushButton:hover {
    border-color: $accent_hover;
}
QPushButton:disabled {
    background: $disabled_bg;
    color: $disabled_text;
    border-color: $border;
}
QPushButton#primaryButton {
    background: $accent;
    border-color: $accent;
    color: white;
    font-weight: 600;
}
QProgressBar {
    border: 1px solid $field_border;
    border-radius: 7px;
    background: $surface;
    text-align: center;
}
QProgressBar::chunk {
    background: $accent_hover;
    border-radius: 6px;
}
QMenuBar {
    background: $surface;
    color: $text;
}
QMenuBar::item {
    background: transparent;
    padding: 6px 10px;
}
QMenuBar::item:selected, QMenu::item:selected {
    background: $menu_hover;
}
QMenu::item {
    padding: 6px 24px;
}
QCheckBox, QRadioButton {
    spacing: 8px;
}
QToolTip {
    background: $tooltip_bg;
    color: $tooltip_text;
    border: 1px solid $field_border;
    padding: 6px;
}
""").substitute(theme)


STYLESHEET = stylesheet_for_theme("light")


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
