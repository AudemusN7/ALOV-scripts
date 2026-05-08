# Build via this command: py -m PyInstaller --noconsole --onefile --icon="icon.ico" --add-data "icon.ico;." --add-data "banner.png;." ALOVSanityChecker_v2.3.py

import sys
import os
import csv
import ctypes
import glob
import subprocess
import json
import struct
import traceback
import concurrent.futures
import threading
from typing import Optional
from datetime import datetime
from dataclasses import dataclass

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QPushButton, QTextEdit, QLabel,
    QComboBox, QCheckBox, QFileDialog, QGroupBox, QLineEdit, QSizePolicy
)
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QPixmap, QIcon, QFont


# --- Utilities ---

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

CONFIG_FILE = "alov_config.json"

def load_config():
    default = {
        "FFPROBE_PATH": "",
        "ARCHIVE_ROOT": "",
        "BINK_ROOT": "",
        "CSV_PATH": ""
    }
    if not os.path.exists(CONFIG_FILE):
        return default
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Failed to load config: {e}")
        return default

def save_config(config):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        print(f"Failed to save config: {e}")


# --- Data Models ---

@dataclass
class VideoMetadata:
    width: int = 0
    height: int = 0
    fps: float = 0.0
    frame_count: int = 0
    codec_profile: str = ""
    error: Optional[str] = None


# --- Worker Thread ---

class ValidatorWorker(QThread):
    log_signal = Signal(str, str)
    finished_signal = Signal(int, int, int)

    VALID_PRORES_PROFILES = {"hq", "standard", "apple prores 422 hq", "apple prores 422"}
    INVALID_PRORES_PROFILES = {"proxy", "lt", "4444", "4444 xq", "apple prores 422 proxy", "apple prores 422 lt", "apple prores 4444", "apple prores 4444 xq"}

    def __init__(self, target_filter, ignore_rounding, deep_scan, mode, config):
        super().__init__()
        self.target_filter = target_filter
        self.ignore_rounding = ignore_rounding
        self.deep_scan = deep_scan
        self.mode = mode
        self.config = dict(config)
        self.counts = {"GREEN": 0, "YELLOW": 0, "RED": 0}
        self.is_cancelled = False
        
        # Tracking active processes to ensure clean cancellation
        self.active_processes = set()
        self.lock = threading.Lock()

    def get_mov_metadata(self, file_path) -> VideoMetadata:
        ffprobe = self.config.get("FFPROBE_PATH", "ffprobe")
        if not ffprobe:
            ffprobe = "ffprobe"

        cmd = [ffprobe, '-v', 'error', '-select_streams', 'v:0']

        if self.deep_scan:
            cmd += ['-count_frames', '-show_entries', 'stream=width,height,r_frame_rate,nb_read_frames,profile']
        else:
            cmd += ['-show_entries', 'stream=width,height,r_frame_rate,nb_frames,profile']

        cmd += ['-of', 'json', file_path]

        # Prevent shell popup window on Windows
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, startupinfo=startupinfo
            )
            
            with self.lock:
                self.active_processes.add(process)
                
            stdout, stderr = process.communicate()
            
            with self.lock:
                if process in self.active_processes:
                    self.active_processes.remove(process)
            
            if self.is_cancelled:
                return VideoMetadata(error="Task cancelled.")
                
            if process.returncode != 0:
                return VideoMetadata(error=f"FFprobe Error: {stderr.strip()}")

            data = json.loads(stdout)
            streams = data.get("streams", [])
            
            if not streams:
                return VideoMetadata(error="No video streams found")

            stream = streams[0]

            try:
                fps_parts = stream.get('r_frame_rate', '0/1').split('/')
                fps = round(float(fps_parts[0]) / float(fps_parts[1]), 3) if int(fps_parts[1]) != 0 else 0.0
            except Exception:
                fps = 0.0

            frame_key = 'nb_read_frames' if self.deep_scan else 'nb_frames'
            raw_frames = stream.get(frame_key, 0)
            if raw_frames in ["N/A", None, ""]:
                raw_frames = 0

            return VideoMetadata(
                width=int(stream.get('width', 0)),
                height=int(stream.get('height', 0)),
                fps=fps,
                frame_count=int(raw_frames),
                codec_profile=stream.get('profile', '').strip()
            )

        except FileNotFoundError:
            return VideoMetadata(error="FFprobe executable not found. Please check FFPROBE_PATH.")
        except Exception as e:
            return VideoMetadata(error=f"FFprobe Runtime Error: {str(e)}")

    def get_bink_metadata(self, file_path) -> VideoMetadata:
        try:
            with open(file_path, 'rb') as f:
                header = f.read(44)
                if len(header) < 44:
                    return VideoMetadata(error="Incomplete Bink header")

                if header[0:3] not in [b'BIK', b'KB2']:
                    return VideoMetadata(error="Invalid Bink Signature")

                frame_count = struct.unpack('<I', header[8:12])[0]
                width = struct.unpack('<I', header[20:24])[0]
                height = struct.unpack('<I', header[24:28])[0]
                fps_div = struct.unpack('<I', header[28:32])[0]
                fps_den = struct.unpack('<I', header[32:36])[0]

                fps = round(fps_div / fps_den, 3) if fps_den != 0 else 0.0

                return VideoMetadata(
                    width=width,
                    height=height,
                    fps=fps,
                    frame_count=frame_count
                )
        except Exception as e:
            return VideoMetadata(error=f"Read Error: {str(e)}")

    def validate_prores_profile(self, profile: str) -> Optional[str]:
        profile = profile.strip().lower()
        
        if profile in self.VALID_PRORES_PROFILES:
            return None
        if profile in self.INVALID_PRORES_PROFILES:
            return f"Invalid ProRes Profile: {profile}"
            
        # Fallback broad matches
        if 'hq' in profile or 'standard' in profile:
            return None
        if 'proxy' in profile or 'lt' in profile or '4444' in profile:
            return f"Invalid ProRes Profile: {profile}"
            
        return f"Unknown ProRes Profile: {profile}"

    def validate_file(self, task):
        if self.is_cancelled:
            return "YELLOW", "Task cancelled."

        file_path = task['path']
        expected_frames = task['expected_frames']
        is_interpolated = task['is_interpolated']
        name = os.path.basename(file_path)

        if not os.path.exists(file_path):
            return "RED", f"Missing file: {name}"

        meta = self.get_bink_metadata(file_path) if self.mode == "BIK" else self.get_mov_metadata(file_path)

        if meta.error:
            return "RED", f"Metadata failure for {name}: {meta.error}"

        status = "GREEN"
        errors = []

        if meta.width != 3840 or meta.height != 2160:
            errors.append(f"Res: {meta.width}x{meta.height}")

        if self.mode == "MOV":
            profile_error = self.validate_prores_profile(meta.codec_profile)
            if profile_error:
                errors.append(profile_error)

        actual_frames = meta.frame_count
        diff = abs(actual_frames - expected_frames)

        if is_interpolated and 0.0 < meta.fps < 59.0:
            errors.append(f"FPS: {meta.fps}")

        if diff == 0 or (diff == 1 and self.ignore_rounding):
            pass
        elif 1 <= diff <= 2 and is_interpolated:
            errors.append(f"Frames: {actual_frames} (Exp: {expected_frames})")
            status = "YELLOW"
        else:
            errors.append(f"Frames: {actual_frames} (Exp: {expected_frames})")
            status = "RED"

        if len(errors) > 0 and status != "YELLOW":
            status = "RED"
        elif len(errors) > 1 and status == "YELLOW":
            status = "RED"

        if status == "GREEN":
            return "GREEN", f"{name} passed."
        elif status == "YELLOW":
            return "YELLOW", f"{name} off by {diff} frames."
        else:
            return "RED", f"{name} errors: " + ", ".join(errors)

    def run(self):
        try:
            csv_path = self.config.get("CSV_PATH", "")
            if not os.path.exists(csv_path):
                self.log_signal.emit("RED", "CSV Reference file not found. Check configurations.")
                return

            root_dir = self.config.get("BINK_ROOT", "") if self.mode == "BIK" else self.config.get("ARCHIVE_ROOT", "")
            if not os.path.exists(root_dir):
                self.log_signal.emit("RED", f"Root directory not found: {root_dir}")
                return

            tasks = []
            
            # Use explicit ALOV schema validation rules
            with open(csv_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                
                for row in reader:
                    if self.target_filter != "ALL" and not row.get('Source', '').startswith(self.target_filter):
                        continue
                        
                    exclusion_col = 'Excluded (BIK)' if self.mode == 'BIK' else 'Excluded (MOV)'
                    if row.get(exclusion_col, '').strip().upper() == 'YES':
                        continue

                    base_name = os.path.splitext(row.get('File Name', ''))[0]
                    if not base_name:
                        continue
                        
                    is_interp = (row.get('Is Interpolated', '').strip().upper() == 'YES')

                    if self.mode == "BIK":
                        bink_dir = os.path.join(root_dir, row.get('Source', ''))
                        bik_file = os.path.join(bink_dir, f"{base_name}.bik")
                        exp_frames_str = row.get('Interpolated Frames', '0') if is_interp else row.get('Vanilla Frames', '0')
                        try:
                            exp_frames = int(exp_frames_str)
                        except ValueError:
                            exp_frames = 0
                        tasks.append({'path': bik_file, 'expected_frames': exp_frames, 'is_interpolated': is_interp})
                    
                    elif self.mode == "MOV":
                        mov_dir = os.path.join(root_dir, row.get('Source', ''))
                        std_file = os.path.join(mov_dir, f"{base_name}.mov")
                        try:
                            vanilla_frames = int(row.get('Vanilla Frames', '0'))
                        except ValueError:
                            vanilla_frames = 0
                            
                        tasks.append({'path': std_file, 'expected_frames': vanilla_frames, 'is_interpolated': False})
                        
                        if is_interp:
                            interp_dir = os.path.join(mov_dir, "INTERPOLATED")
                            pattern = os.path.join(interp_dir, f"{base_name}_60_*.mov")
                            found = sorted(glob.glob(pattern)) 
                            
                            if not found:
                                self.counts["RED"] += 1
                                self.log_signal.emit("RED", f"Missing Interpolated file for {base_name}")
                            elif len(found) > 1:
                                self.counts["RED"] += 1
                                self.log_signal.emit("RED", f"Multiple Interpolated files found for {base_name}.")
                            else:
                                try:
                                    interp_frames = int(row.get('Interpolated Frames', '0'))
                                except ValueError:
                                    interp_frames = 0
                                tasks.append({'path': found[0], 'expected_frames': interp_frames, 'is_interpolated': True})
            
            if not tasks:
                self.log_signal.emit("YELLOW", "No files found to process based on filters and CSV.")
                return

            self.log_signal.emit("GREEN", f"Found {len(tasks)} files to process. Starting parallel validation...")

            # Deploy multithreaded workers to speed up operations
            max_workers = 8 if self.mode == "BIK" else 4
            tasks.sort(key=lambda t: t['path'].lower())

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                results = executor.map(self.validate_file, tasks)

                for status, msg in results:
                    if self.is_cancelled:
                        break

                    if status in self.counts:
                            self.counts[status] += 1

                    self.log_signal.emit(status, msg)

        except Exception as e:
            self.log_signal.emit("RED", f"Worker Engine Error:\n{traceback.format_exc()}")
        finally:
            self.finished_signal.emit(self.counts["GREEN"], self.counts["YELLOW"], self.counts["RED"])

    def stop(self):
        self.is_cancelled = True
        # Cleanly kill all active background sub-processes
        with self.lock:
            for process in self.active_processes:
                try:
                    process.kill()
                except Exception:
                    pass
            self.active_processes.clear()


# --- UI Components ---

class ResizableBanner(QLabel):
    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self.original_pixmap = pixmap
        self.setMinimumSize(1, 1)
        self.setAlignment(Qt.AlignCenter)

    def resizeEvent(self, event):
        if not self.original_pixmap.isNull():
            scaled_pixmap = self.original_pixmap.scaled(
                self.width(), 400, 
                Qt.AspectRatioMode.KeepAspectRatio, 
                Qt.TransformationMode.SmoothTransformation
            )
            self.setPixmap(scaled_pixmap)


class ALOVSanityChecker(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ALOV Sanity Checker v2.3")
        self.resize(1200, 850)
        
        icon_path = resource_path("icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.config = load_config()
        self.log_file = None
        self.init_ui()

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)

        # Banner
        banner_img = resource_path("banner.png")
        if os.path.exists(banner_img):
            pixmap = QPixmap(banner_img)
            self.banner = ResizableBanner(pixmap)
            self.banner.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            layout.addWidget(self.banner)

        # Config Group
        config_group = QGroupBox("Configuration Paths")
        config_layout = QVBoxLayout()
        
        self.paths = {}
        path_configs = [
            ("FFPROBE_PATH", "FFprobe EXE:"), 
            ("ARCHIVE_ROOT", "MOV Archive Root:"), 
            ("BINK_ROOT", "Bink Archive Root:"), 
            ("CSV_PATH", "Reference CSV:")
        ]
        
        for key, label in path_configs:
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            le = QLineEdit(self.config.get(key, ""))
            le.textChanged.connect(self.save_current_config)
            btn = QPushButton("Browse")
            btn.clicked.connect(lambda checked=False, k=key, l=le: self.browse_path(k, l))
            row.addWidget(le)
            row.addWidget(btn)
            self.paths[key] = le
            config_layout.addLayout(row)
            
        config_group.setLayout(config_layout)
        layout.addWidget(config_group)

        # Controls Row
        ctrl_layout = QHBoxLayout()
        self.mode_selector = QComboBox()
        self.mode_selector.addItems(["MOV (ProRes Masters)", "BIK (Final Binks)"])
        self.mode_selector.currentIndexChanged.connect(self.update_ui_for_mode)
        
        self.archive_selector = QComboBox()
        self.archive_selector.addItems(["ALL", "LE1", "LE2", "LE3"])
        
        self.ignore_rounding_cb = QCheckBox("Ignore Rounding (±1)")
        self.verbose_cb = QCheckBox("Deep Scan (Slower)")
        
        self.run_btn = QPushButton("Run Sanity Check")
        self.run_btn.setMinimumHeight(35)
        self.run_btn.clicked.connect(self.start_validation)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setMinimumHeight(35)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_validation)
        
        self.clear_btn = QPushButton("Clear Log")
        self.clear_btn.setMinimumHeight(35)
        self.clear_btn.clicked.connect(lambda: self.log_display.clear())

        ctrl_layout.addWidget(QLabel("Mode:"))
        ctrl_layout.addWidget(self.mode_selector)
        ctrl_layout.addWidget(QLabel("Target:"))
        ctrl_layout.addWidget(self.archive_selector)
        ctrl_layout.addWidget(self.ignore_rounding_cb)
        ctrl_layout.addWidget(self.verbose_cb)
        ctrl_layout.addStretch()
        ctrl_layout.addWidget(self.run_btn)
        ctrl_layout.addWidget(self.cancel_btn)
        ctrl_layout.addWidget(self.clear_btn)
        layout.addLayout(ctrl_layout)

        # Log Display
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setStyleSheet("background-color: #1e1e1e; color: #ffffff;")
        self.log_display.setFont(QFont("Consolas", 10))
        layout.addWidget(self.log_display)

        self.status_label = QLabel("Ready")
        layout.addWidget(self.status_label)

        self.update_ui_for_mode()

    def browse_path(self, key, line_edit):
        file_keys = {"FFPROBE_PATH", "CSV_PATH"}

        if key in file_keys:
            path, _ = QFileDialog.getOpenFileName(self, "Select File")
        else:
            path = QFileDialog.getExistingDirectory(self, "Select Directory")

        if path:
            line_edit.setText(path)
            self.save_current_config()

    def save_current_config(self):
        for key, le in self.paths.items():
            self.config[key] = le.text()
        save_config(self.config)

    def append_log(self, status, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        color = "#FFFFFF" 
        if status == "GREEN": color = "#55FF55"
        elif status == "YELLOW": color = "#FFFF55"
        elif status == "RED": color = "#FF5555"

        log_entry = f'<span style="color:#888888">[{timestamp}]</span> <b style="color:{color}">[{status}]</b> {message}'
        self.log_display.append(log_entry)
        
        # Stream into text document
        if self.log_file and not self.log_file.closed:
            try:
                self.log_file.write(f"[{timestamp}] [{status}] {message}\n")
                self.log_file.flush()
            except Exception:
                pass

    def on_finished(self, g, y, r):
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.status_label.setText(f"Done. Results -> Green: {g} | Yellow: {y} | Red: {r}")
        
        if self.log_file and not self.log_file.closed:
            self.log_file.close()

    def start_validation(self):
        self.save_current_config() 
        self.log_display.clear()
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.status_label.setText("Processing...")
        
        target = self.archive_selector.currentText()
        mode = "MOV" if "MOV" in self.mode_selector.currentText() else "BIK"
        log_name = "alov_sanity_log.txt" if target == "ALL" else f"alov_sanity_log_{target}.txt"
        
        try:
            self.log_file = open(log_name, "w", encoding="utf-8")
        except Exception as e:
            self.append_log("RED", f"Failed to initialize log file: {e}")
            self.log_file = None
        
        self.worker = ValidatorWorker(target, self.ignore_rounding_cb.isChecked(), 
                                      self.verbose_cb.isChecked(), mode, self.config)
        self.worker.log_signal.connect(self.append_log)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.start()

    def cancel_validation(self):
        if hasattr(self, 'worker') and self.worker.isRunning():
            self.status_label.setText("Cancelling processes safely. Please wait...")
            self.worker.stop()
            self.cancel_btn.setEnabled(False)

    def update_ui_for_mode(self):
        is_bik_mode = "BIK" in self.mode_selector.currentText()
        if is_bik_mode:
            self.ignore_rounding_cb.setChecked(True)
            self.ignore_rounding_cb.setEnabled(False)
            self.verbose_cb.setChecked(False)
            self.verbose_cb.setEnabled(False)
            self.status_label.setText("Settings Locked for BIK Mode")
        else:
            self.ignore_rounding_cb.setEnabled(True)
            self.verbose_cb.setEnabled(True)
            self.status_label.setText("Settings Unlocked for MOV Mode")


# --- Main Entry ---

if __name__ == "__main__":
    if sys.platform == 'win32':
        myappid = 'alov.sanitychecker.v2.3'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)

    app = QApplication(sys.argv)
    
    icon_path = resource_path("icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
        
    window = ALOVSanityChecker()
    window.show()
    sys.exit(app.exec())
