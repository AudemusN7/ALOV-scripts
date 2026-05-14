# Minimum requirement: Python 3.10+
# Build via this command: py -m PyInstaller --noconsole --onefile --icon="icon.ico" --add-data "icon.ico;." --add-data "banner.png;." ALOVSanityChecker_v2.5.py

import concurrent.futures
import csv
import ctypes
import glob
import html
import json
import os
import struct
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QIcon, QFont, QPixmap


# --- Utilities ---

CONFIG_FILE = "alov_config.json"


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def load_config():
    default = {"FFPROBE_PATH": "", "ARCHIVE_ROOT": "", "BINK_ROOT": "", "CSV_PATH": ""}
    if not os.path.exists(CONFIG_FILE):
        return default
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return default | json.load(f)
    except Exception:
        return default


def save_config(config):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
    except Exception:
        pass


@dataclass
class VideoMetadata:
    width: int = 0
    height: int = 0
    fps: float = 0.0
    frame_count: int = 0
    codec_profile: str = ""
    error: str | None = None


@dataclass(frozen=True)
class CsvFlags:
    is_interpolated: bool
    is_bik_only: bool
    exclude_mov: bool
    exclude_bik: bool


# --- Worker Thread ---

class ValidatorWorker(QThread):
    log_signal = Signal(str, str)
    finished_signal = Signal(int, int, int)

    EXPECTED_WIDTH = 3840
    EXPECTED_HEIGHT = 2160
    MIN_INTERP_FPS = 59.0
    MAX_INTERP_FPS = 60.0

    VALID_PRORES = {"hq", "standard", "apple prores 422 hq", "apple prores 422"}
    VALID_EXCLUSIONS = {"MOV", "BIK", "YES", "NO", ""}

    def __init__(self, target_filter, ignore_rounding, deep_scan, mode, config):
        super().__init__()
        self.target_filter = target_filter
        self.ignore_rounding = ignore_rounding
        self.deep_scan = deep_scan
        self.mode = mode
        self.config = dict(config)
        self.counts = {"GREEN": 0, "YELLOW": 0, "RED": 0}
        self.is_cancelled = False
        self.active_processes = set()
        self.lock = threading.Lock()

    def parse_csv_flags(self, row) -> CsvFlags:
        interp_val = row.get("Is Interpolated", "").strip().upper()
        is_bik_only = interp_val == "YES (BIK ONLY)"

        exclusion = row.get("Excluded", "").strip().upper()
        if exclusion not in self.VALID_EXCLUSIONS:
            self.counts["YELLOW"] += 1
            file_name = row.get("File Name", "<unknown>")
            self.log_signal.emit(
                "YELLOW",
                f"Invalid Excluded value '{exclusion}' for {file_name}; treating as NO.",
            )
            exclusion = "NO"

        return CsvFlags(
            is_interpolated=interp_val.startswith("YES"),
            is_bik_only=is_bik_only,
            exclude_mov=exclusion in {"MOV", "YES"},
            exclude_bik=exclusion in {"BIK", "YES"},
        )

    def get_mov_metadata(self, file_path) -> VideoMetadata:
        ffprobe = self.config.get("FFPROBE_PATH", "ffprobe") or "ffprobe"
        cmd = [ffprobe, "-v", "error", "-select_streams", "v:0"]
        cmd += (
            ["-count_frames", "-show_entries", "stream=width,height,r_frame_rate,nb_read_frames,profile"]
            if self.deep_scan
            else ["-show_entries", "stream=width,height,r_frame_rate,nb_frames,profile"]
        )
        cmd += ["-of", "json", file_path]

        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                startupinfo=startupinfo,
            )
            with self.lock:
                self.active_processes.add(process)
            stdout, stderr = process.communicate()
            with self.lock:
                self.active_processes.discard(process)

            if self.is_cancelled:
                return VideoMetadata(error="Cancelled")
            if process.returncode != 0:
                return VideoMetadata(error=stderr.strip())

            data = json.loads(stdout)
            stream = data.get("streams", [{}])[0]
            if not stream:
                return VideoMetadata(error="No streams")

            fps_parts = stream.get("r_frame_rate", "0/1").split("/")
            fps = round(float(fps_parts[0]) / float(fps_parts[1]), 3) if int(fps_parts[1]) != 0 else 0.0

            frame_key = "nb_read_frames" if self.deep_scan else "nb_frames"
            raw_frames = stream.get(frame_key, 0)

            return VideoMetadata(
                width=int(stream.get("width", 0)),
                height=int(stream.get("height", 0)),
                fps=fps,
                frame_count=int(raw_frames) if raw_frames not in ["N/A", "", None] else 0,
                codec_profile=stream.get("profile", "").strip(),
            )
        except Exception as e:
            return VideoMetadata(error=str(e))

    def get_bink_metadata(self, file_path) -> VideoMetadata:
        try:
            with open(file_path, "rb") as f:
                h = f.read(44)
                if len(h) < 44 or h[0:3] not in [b"BIK", b"KB2"]:
                    return VideoMetadata(error="Invalid Bink")

                fps_denominator = struct.unpack("<I", h[32:36])[0]
                fps = 0.0 if fps_denominator == 0 else struct.unpack("<I", h[28:32])[0] / fps_denominator

                return VideoMetadata(
                    width=struct.unpack("<I", h[20:24])[0],
                    height=struct.unpack("<I", h[24:28])[0],
                    frame_count=struct.unpack("<I", h[8:12])[0],
                    fps=round(fps, 3),
                )
        except Exception as e:
            return VideoMetadata(error=str(e))

    def validate_file(self, task):
        if self.is_cancelled:
            return "YELLOW", "Task cancelled."
        file_path, exp_frames, is_interp = task["path"], task["expected_frames"], task["is_interpolated"]
        name = os.path.basename(file_path)

        if not os.path.exists(file_path):
            return "RED", f"Missing: {name}"

        meta = self.get_bink_metadata(file_path) if self.mode == "BIK" else self.get_mov_metadata(file_path)
        if meta.error:
            return "RED", f"Metadata Error [{name}]: {meta.error}"

        errors = []
        if meta.width != self.EXPECTED_WIDTH or meta.height != self.EXPECTED_HEIGHT:
            errors.append(f"Res: {meta.width}x{meta.height}")

        if self.mode == "MOV":
            prof = meta.codec_profile.lower()
            if prof not in self.VALID_PRORES:
                errors.append(f"Profile: {prof if prof else 'None'}")

        diff = abs(meta.frame_count - exp_frames)
        if is_interp and not (self.MIN_INTERP_FPS <= meta.fps <= self.MAX_INTERP_FPS):
            errors.append(f"FPS: {meta.fps} (Range: 59-60)")

        if not (diff == 0 or (diff == 1 and self.ignore_rounding)):
            errors.append(f"Frames: {meta.frame_count} (Exp: {exp_frames})")

        if not errors:
            return "GREEN", f"{name} passed integrity check!"

        if len(errors) == 1 and "Frames:" in errors[0] and 1 <= diff <= 2 and is_interp:
            return "YELLOW", f"{name} off by {diff} frames. Consider fixing."

        return "RED", f"{name} errors: " + ", ".join(errors)

    def parse_frame_count(self, row, column_name):
        try:
            return int(row.get(column_name, "0") or 0)
        except ValueError:
            file_name = row.get("File Name", "<unknown>")
            self.counts["YELLOW"] += 1
            self.log_signal.emit("YELLOW", f"Invalid {column_name} value for {file_name}; using 0.")
            return 0

    def find_interpolated_movs(self, source_dir, base_name):
        patterns = [
            os.path.join(source_dir, "INTERPOLATED", f"{base_name}_60_*.mov"),
            os.path.join(source_dir, f"{base_name}_60_*.mov"),
        ]
        found = []
        for pattern in patterns:
            found.extend(glob.glob(pattern))
        return sorted(set(os.path.normpath(path) for path in found))

    def add_expected_file(self, expected_files, path):
        expected_files.add(os.path.normcase(os.path.normpath(path)))

    def run(self):
        try:
            csv_path = self.config.get("CSV_PATH", "")
            root_dir = self.config.get("BINK_ROOT", "") if self.mode == "BIK" else self.config.get("ARCHIVE_ROOT", "")

            tasks = []
            expected_files = set()
            scanned_subdirs = set()

            with open(csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    source = row.get("Source", "")
                    if self.target_filter != "ALL" and not source.startswith(self.target_filter):
                        continue

                    base_name = os.path.splitext(row.get("File Name", ""))[0]
                    if not base_name:
                        continue

                    source_dir = os.path.normpath(os.path.join(root_dir, source))
                    scanned_subdirs.add(source_dir)

                    flags = self.parse_csv_flags(row)

                    if self.mode == "BIK":
                        bik_path = os.path.join(source_dir, f"{base_name}.bik")
                        self.add_expected_file(expected_files, bik_path)
                        if not flags.exclude_bik:
                            exp_frames = (
                                self.parse_frame_count(row, "Interpolated Frames")
                                if flags.is_interpolated
                                else self.parse_frame_count(row, "Vanilla Frames")
                            )
                            tasks.append({"path": bik_path, "expected_frames": exp_frames, "is_interpolated": flags.is_interpolated})

                    elif self.mode == "MOV":
                        v_path = os.path.join(source_dir, f"{base_name}.mov")
                        self.add_expected_file(expected_files, v_path)
                        if not flags.exclude_mov:
                            v_frames = self.parse_frame_count(row, "Vanilla Frames")
                            tasks.append({"path": v_path, "expected_frames": v_frames, "is_interpolated": False})

                        if flags.is_interpolated:
                            interp_dir = os.path.join(source_dir, "INTERPOLATED")
                            scanned_subdirs.add(interp_dir)

                            found = self.find_interpolated_movs(source_dir, base_name)
                            if flags.is_bik_only:
                                continue

                            if flags.exclude_mov:
                                for f_path in found:
                                    self.add_expected_file(expected_files, f_path)
                            else:
                                for f_path in found:
                                    self.add_expected_file(expected_files, f_path)

                                if len(found) == 1:
                                    i_frames = self.parse_frame_count(row, "Interpolated Frames")
                                    tasks.append({"path": found[0], "expected_frames": i_frames, "is_interpolated": True})
                                elif not found:
                                    self.counts["RED"] += 1
                                    self.log_signal.emit("RED", f"Missing Interpolated file for {base_name}")
                                else:
                                    self.counts["RED"] += 1
                                    self.log_signal.emit("RED", f"Multiple Interpolated files for {base_name}: {', '.join(found)}")

            # Parallel Process
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                results = executor.map(self.validate_file, tasks)
                for status, msg in results:
                    if self.is_cancelled:
                        break
                    if status in self.counts:
                        self.counts[status] += 1
                    self.log_signal.emit(status, msg)

            # Ghost File Sweep
            ext_filter = ".bik" if self.mode == "BIK" else ".mov"
            for s_dir in scanned_subdirs:
                if not os.path.exists(s_dir):
                    continue
                for entry in os.scandir(s_dir):
                    if entry.is_file() and entry.name.lower().endswith(ext_filter):
                        if os.path.normcase(os.path.normpath(entry.path)) not in expected_files:
                            self.counts["RED"] += 1
                            self.log_signal.emit("RED", f"[GHOST] Found file not in CSV: {entry.path}")

        except Exception:
            self.log_signal.emit("RED", f"Worker Error: {traceback.format_exc()}")
        finally:
            self.finished_signal.emit(self.counts["GREEN"], self.counts["YELLOW"], self.counts["RED"])

    def stop(self):
        self.is_cancelled = True
        with self.lock:
            for p in self.active_processes:
                try:
                    p.kill()
                except Exception:
                    pass


# --- UI ---

class ResizableBanner(QLabel):
    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self.original_pixmap = pixmap
        self.setMinimumSize(1, 1)
        self.setAlignment(Qt.AlignCenter)

    def resizeEvent(self, event):
        if not self.original_pixmap.isNull():
            scaled_pixmap = self.original_pixmap.scaled(
                self.width(),
                400,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.setPixmap(scaled_pixmap)
        super().resizeEvent(event)


class ALOVSanityChecker(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ALOV Sanity Checker v2.5")
        self.resize(1200, 850)

        icon_path = resource_path("icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.config = load_config()
        self.log_file = None
        self.worker = None
        self.init_ui()

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        banner_img = resource_path("banner.png")
        if os.path.exists(banner_img):
            self.banner = ResizableBanner(QPixmap(banner_img))
            layout.addWidget(self.banner)

        config_group = QGroupBox("Configuration Paths")
        config_layout = QVBoxLayout()
        self.paths = {}
        for key, label in [
            ("FFPROBE_PATH", "FFprobe EXE:"),
            ("ARCHIVE_ROOT", "ProRes Archive Root:"),
            ("BINK_ROOT", "Bink Deployment Root:"),
            ("CSV_PATH", "Reference CSV:"),
        ]:
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            le = QLineEdit(self.config.get(key, ""))
            le.textChanged.connect(self.save_config_from_ui)
            btn = QPushButton("Browse")
            btn.clicked.connect(lambda chk=False, k=key, l=le: self.browse(k, l))
            row.addWidget(le)
            row.addWidget(btn)
            self.paths[key] = le
            config_layout.addLayout(row)
        config_group.setLayout(config_layout)
        layout.addWidget(config_group)

        ctrl = QHBoxLayout()
        self.mode_selector = QComboBox()
        self.mode_selector.addItems(["MOV (ProRes)", "BIK (Final)"])
        self.mode_selector.currentIndexChanged.connect(self.update_ui_for_mode)

        self.archive_selector = QComboBox()
        self.archive_selector.addItems(["ALL", "LE1", "LE2", "LE3"])
        self.ignore_rounding_cb = QCheckBox("Allow +/-1 Frame Mismatch")
        self.deep_scan_cb = QCheckBox("Deep Scan")

        self.run_btn = QPushButton("Run Check")
        self.run_btn.setMinimumHeight(35)
        self.run_btn.clicked.connect(self.start_validation)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setMinimumHeight(35)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        self.clear_btn = QPushButton("Clear Log")
        self.clear_btn.setMinimumHeight(35)
        self.clear_btn.clicked.connect(lambda: self.log_display.clear())

        ctrl.addWidget(QLabel("Mode:"))
        ctrl.addWidget(self.mode_selector)
        ctrl.addWidget(QLabel("Target:"))
        ctrl.addWidget(self.archive_selector)
        ctrl.addWidget(self.ignore_rounding_cb)
        ctrl.addWidget(self.deep_scan_cb)
        ctrl.addStretch()
        ctrl.addWidget(self.run_btn)
        ctrl.addWidget(self.cancel_btn)
        ctrl.addWidget(self.clear_btn)
        layout.addLayout(ctrl)

        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setStyleSheet("background: #1e1e1e; color: #fff;")
        self.log_display.setFont(QFont("Consolas", 10))
        layout.addWidget(self.log_display)

        self.status_label = QLabel("Ready")
        layout.addWidget(self.status_label)
        self.update_ui_for_mode()

    def browse(self, key, le):
        res = QFileDialog.getOpenFileName(self)[0] if "PATH" in key else QFileDialog.getExistingDirectory(self)
        if res:
            le.setText(res)
            self.save_config_from_ui()

    def save_config_from_ui(self):
        for k, le in self.paths.items():
            self.config[k] = le.text()
        save_config(self.config)

    def append_log(self, status, msg):
        timestamp = datetime.now().strftime("%H:%M:%S")
        color = {"GREEN": "#55FF55", "YELLOW": "#FFFF55", "RED": "#FF5555"}.get(status, "#FFF")
        safe_msg = html.escape(msg)
        self.log_display.append(
            f'<span style="color:#888888">[{timestamp}]</span> '
            f'<b style="color:{color}">[{status}]</b> {safe_msg}'
        )
        if self.log_file and not self.log_file.closed:
            try:
                self.log_file.write(f"[{timestamp}] [{status}] {msg}\n")
                self.log_file.flush()
            except Exception:
                pass

    def update_ui_for_mode(self):
        is_bik = "BIK" in self.mode_selector.currentText()
        self.ignore_rounding_cb.setChecked(is_bik)
        self.ignore_rounding_cb.setEnabled(not is_bik)
        self.deep_scan_cb.setChecked(False)
        self.deep_scan_cb.setEnabled(not is_bik)
        self.status_label.setText("Settings Locked for BIK Mode" if is_bik else "Settings Unlocked for MOV Mode")

    def start_validation(self):
        if self.log_file and not self.log_file.closed:
            self.log_file.close()
        self.save_config_from_ui()
        self.log_display.clear()

        target = self.archive_selector.currentText()
        mode = "MOV" if "MOV" in self.mode_selector.currentText() else "BIK"
        csv_path = self.config.get("CSV_PATH", "")
        root_dir = self.config.get("BINK_ROOT", "") if mode == "BIK" else self.config.get("ARCHIVE_ROOT", "")

        if not os.path.exists(csv_path):
            return self.append_log("RED", "Pre-flight failed: Reference CSV path is invalid.")
        if not os.path.exists(root_dir):
            return self.append_log("RED", f"Pre-flight failed: Archive root path is invalid -> {root_dir}")

        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.status_label.setText("Processing...")

        log_name = f"alov_log_{target}.txt" if target != "ALL" else "alov_log.txt"
        try:
            self.log_file = open(log_name, "w", encoding="utf-8")
        except Exception as e:
            self.append_log("RED", f"Failed to initialize log file: {e}")
            self.log_file = None

        self.worker = ValidatorWorker(target, self.ignore_rounding_cb.isChecked(), self.deep_scan_cb.isChecked(), mode, self.config)
        self.worker.log_signal.connect(self.append_log)
        self.worker.finished_signal.connect(self.done)
        self.worker.start()

    def cancel(self):
        if self.worker and self.worker.isRunning():
            self.status_label.setText("Cancelling processes safely. Please wait...")
            self.worker.stop()
            self.cancel_btn.setEnabled(False)

    def done(self, g, y, r):
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.status_label.setText(f"Done. Results -> Green: {g} | Yellow: {y} | Red: {r}")
        if self.log_file and not self.log_file.closed:
            self.log_file.close()
        self.worker = None


if __name__ == "__main__":
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("alov.sanitychecker.v2.5")
    app = QApplication(sys.argv)
    icon_path = resource_path("icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    window = ALOVSanityChecker()
    window.show()
    sys.exit(app.exec())
