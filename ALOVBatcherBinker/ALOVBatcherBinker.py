# Build via this command: py -m PyInstaller --noconsole --onefile --icon="icon.ico" --add-data "icon.ico;." --add-data "banner.png;." ALOVBatchBinker1.3.py

import sys
import os
import glob
import ctypes
import subprocess
import json
import traceback
import time
from datetime import datetime
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QTextEdit, QLabel, 
                             QFileDialog, QGroupBox, QLineEdit, QSizePolicy,
                             QDialog, QFormLayout, QDialogButtonBox, QProgressBar,
                             QCheckBox, QComboBox, QSpinBox)
from PySide6.QtCore import QThread, Signal, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPixmap, QIcon

CONFIG_FILE = "alov_binker_config.json"

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except: pass
    return {
        "BINK_EXE_PATH": "", 
        "FFPROBE_EXE_PATH": "",
        "INPUT_DIR": "", 
        "OUTPUT_DIR": "",
        "DATA_RATE": "4500000",
        "PEAK_RATE": "8000000",
        "PREVIEW_FRAMES": 32,
        "BINK_VERSION": 200,
        "SHOW_WINDOW": False
    }

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

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
                Qt.KeepAspectRatio, 
                Qt.SmoothTransformation
            )
            self.setPixmap(scaled_pixmap)

class ConfigDialog(QDialog):
    def __init__(self, data_rate, peak_rate, preview_frames, bink_version, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bink Encoding Settings")
        layout = QFormLayout(self)

        self.data_rate_le = QLineEdit(str(data_rate))
        self.peak_rate_le = QLineEdit(str(peak_rate))

        self.preview_sb = QSpinBox()
        self.preview_sb.setRange(2, 64)
        self.preview_sb.setValue(int(preview_frames))

        self.version_cb = QComboBox()
        self.version_map = {
            "Bink 1 (Legacy)": 100,
            "Bink 2": 200,
            "Bink 2 + Bink Audio 1.1": 201,
            "Bink 2 HDR": 270,
            "Bink 2 HDR + Bink Audio 1.1": 281
        }
        for text, val in self.version_map.items():
            self.version_cb.addItem(text, val)

        # Set currently selected version
        idx = self.version_cb.findData(int(bink_version))
        if idx >= 0:
            self.version_cb.setCurrentIndex(idx)

        layout.addRow("Data Rate (/d):", self.data_rate_le)
        layout.addRow("Peak Rate (/m):", self.peak_rate_le)
        layout.addRow("Preview Frames (/p):", self.preview_sb)
        layout.addRow("Bink Version (/v):", self.version_cb)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def get_values(self):
        return (self.data_rate_le.text(), 
                self.peak_rate_le.text(), 
                self.preview_sb.value(), 
                self.version_cb.currentData())

class BinkerWorker(QThread):
    log_signal = Signal(str, str)
    finished_signal = Signal(int, int)
    progress_signal = Signal(int, int, int, int) # (Current File, Total Files, ETA Seconds, Percentage)

    def __init__(self, config, data_rate, peak_rate, preview_frames, bink_version, show_window):
        super().__init__()
        self.config = config
        self.data_rate = data_rate
        self.peak_rate = peak_rate
        self.preview_frames = preview_frames
        self.bink_version = bink_version
        self.show_window = show_window
        self.counts = {"SUCCESS": 0, "FAILED": 0}

    def run(self):
        try:
            exe_path = os.path.normpath(self.config.get("BINK_EXE_PATH", ""))
            input_dir = os.path.normpath(self.config.get("INPUT_DIR", ""))
            output_dir = os.path.normpath(self.config.get("OUTPUT_DIR", ""))

            if not all([os.path.exists(exe_path), os.path.exists(input_dir), os.path.exists(output_dir)]):
                self.log_signal.emit("RED", "Invalid paths. Please check your configuration.")
                return

            mov_files = sorted(glob.glob(os.path.join(input_dir, "*.mov")))
            total_files = len(mov_files)
            
            if not mov_files:
                self.log_signal.emit("YELLOW", f"No .mov files found in {input_dir}")
                return

            self.log_signal.emit("GREEN", f"Found {total_files} videos. Starting batch...")

            # --- DYNAMIC ETA PRE-CALCULATION (FFPROBE) ---
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE

            ffprobe_cmd_base = self.config.get("FFPROBE_EXE_PATH", "ffprobe") or "ffprobe"
            has_dynamic_eta = True
            total_video_duration = 0
            file_durations = {}

            self.log_signal.emit("GREEN", "Attempting to index durations to calculate ETA...")
            for mov_file in mov_files:
                try:
                    cmd = [ffprobe_cmd_base, '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', mov_file]
                    result = subprocess.run(cmd, capture_output=True, text=True, startupinfo=si)
                    
                    if result.returncode != 0:
                        raise Exception("ffprobe returned non-zero exit code (is it installed/in PATH?)")
                    
                    probe_data = json.loads(result.stdout)
                    dur = float(probe_data.get('format', {}).get('duration', 0))
                    
                    if dur <= 0:
                        raise ValueError("Invalid duration")
                    
                    file_durations[mov_file] = dur
                    total_video_duration += dur

                except Exception as e:
                    self.log_signal.emit("YELLOW", f"ffprobe failed ({str(e)}). Falling back to simplified progress bar.")
                    has_dynamic_eta = False
                    break

            # Initialize UI Progress (-1 ETA indicates 'Calculating...')
            self.progress_signal.emit(0, total_files, -1, 0 if has_dynamic_eta else -1)

            # Subprocess config for Bink
            bink_si = subprocess.STARTUPINFO()
            if not self.show_window:
                bink_si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                bink_si.wShowWindow = subprocess.SW_HIDE

            completed_video_duration = 0
            total_elapsed_time = 0

            for index, mov_file in enumerate(mov_files):
                if self.isInterruptionRequested():
                    self.log_signal.emit("YELLOW", "Batch compression aborted.")
                    break

                base_name = os.path.basename(mov_file)
                bik_name = os.path.splitext(base_name)[0] + ".bik"
                output_file = os.path.normpath(os.path.join(output_dir, bik_name))
                input_file = os.path.normpath(mov_file)

                # Adjusted CMD arguments based on config values
                cmd = [
                    exe_path, "Bink2c", input_file, output_file, 
                    f"/v{self.bink_version}", f"/d{self.data_rate}", f"/m{self.peak_rate}", 
                    "/l-1", f"/p{self.preview_frames}", "/#" 
                ]

                self.log_signal.emit("YELLOW", f"Compressing: {base_name}...")
                
                start_time = time.time()

                try:
                    result = subprocess.run(
                        cmd, 
                        stdout=subprocess.DEVNULL, 
                        stderr=subprocess.DEVNULL, 
                        startupinfo=bink_si, 
                        shell=False
                    )
                    
                    elapsed = time.time() - start_time

                    if result.returncode == 0:
                        self.counts["SUCCESS"] += 1
                        self.log_signal.emit("GREEN", f"Finished: {bik_name}")
                        
                        # --- DYNAMIC ETA UPDATE ---
                        if has_dynamic_eta and total_video_duration > 0:
                            completed_video_duration += file_durations[mov_file]
                            total_elapsed_time += elapsed

                            if total_elapsed_time > 0 and completed_video_duration > 0:
                                # Encode speed = Seconds of video processed per real second
                                speed = completed_video_duration / total_elapsed_time
                                remaining_video = total_video_duration - completed_video_duration
                                eta_seconds = int(remaining_video / speed) if speed > 0 else 0
                                percent = int((completed_video_duration / total_video_duration) * 100)
                                self.progress_signal.emit(index + 1, total_files, eta_seconds, percent)
                            else:
                                self.progress_signal.emit(index + 1, total_files, -1, -1)
                        else:
                            self.progress_signal.emit(index + 1, total_files, -1, -1)

                    else:
                        self.counts["FAILED"] += 1
                        self.log_signal.emit("RED", f"Failed: {base_name} (Exit Code: {result.returncode})")
                        self.progress_signal.emit(index + 1, total_files, -1, -1)

                except Exception as e:
                    self.counts["FAILED"] += 1
                    self.log_signal.emit("RED", f"Error on {base_name}: {str(e)}")
                    self.progress_signal.emit(index + 1, total_files, -1, -1)

        except Exception as e:
            self.log_signal.emit("RED", f"Fatal Crash: {str(e)}")
        finally:
            self.finished_signal.emit(self.counts["SUCCESS"], self.counts["FAILED"])

class ALOVBatchBinker(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ALOV Batch Binker v1.3")
        self.resize(1100, 850)
        
        self.config = load_config()
        self.data_rate = self.config.get("DATA_RATE", "4500000")
        self.peak_rate = self.config.get("PEAK_RATE", "8000000")
        self.preview_frames = self.config.get("PREVIEW_FRAMES", 32)
        self.bink_version = self.config.get("BINK_VERSION", 200)
        self.show_window = self.config.get("SHOW_WINDOW", False)

        # Setup state for the live countdown timer
        self.remaining_seconds = 0
        self.current_idx = 0
        self.total_count = 0
        self.current_percent = 0
        
        self.countdown_timer = QTimer(self)
        self.countdown_timer.timeout.connect(self.tick_eta)
        
        icon_path = resource_path("icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        
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
            ("BINK_EXE_PATH", "Bink2ForUnreal EXE:"),
            ("FFPROBE_EXE_PATH", "FFprobe EXE (Optional):"),
            ("INPUT_DIR", "Input Directory:"), 
            ("OUTPUT_DIR", "Output Directory:")
        ]
        
        for key, label in path_configs:
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            le = QLineEdit(self.config.get(key, ""))
            le.setPlaceholderText("Using ffprobe enables a more accurate progress bar" if "FFPROBE" in key else "")
            le.textChanged.connect(self.save_current_config)
            btn = QPushButton("Browse")
            btn.clicked.connect(lambda checked=False, k=key, l=le: self.browse_path(k, l))
            row.addWidget(le)
            row.addWidget(btn)
            self.paths[key] = le
            config_layout.addLayout(row)
            
        # Checkbox for Subprocess Window visibility
        self.show_window_cb = QCheckBox("Show Bink Compressor Processing Window")
        self.show_window_cb.setChecked(self.show_window)
        self.show_window_cb.stateChanged.connect(self.save_current_config)
        config_layout.addWidget(self.show_window_cb)

        layout.addWidget(config_group)
        config_group.setLayout(config_layout)

        # Controls
        ctrl_layout = QHBoxLayout()
        self.run_btn = QPushButton("Start Batch Compression")
        self.run_btn.setMinimumHeight(35)
        self.run_btn.clicked.connect(self.start_batch)

        self.config_btn = QPushButton("Configure")
        self.config_btn.setMinimumHeight(35)
        self.config_btn.clicked.connect(self.open_config_dialog)
        
        self.cancel_btn = QPushButton("Abort")
        self.cancel_btn.setMinimumHeight(35)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.abort_batch)

        self.clear_btn = QPushButton("Clear Log")
        self.clear_btn.setMinimumHeight(35)
        self.clear_btn.clicked.connect(lambda: self.log_display.clear())

        ctrl_layout.addStretch()
        ctrl_layout.addWidget(self.run_btn)
        ctrl_layout.addWidget(self.config_btn)
        ctrl_layout.addWidget(self.cancel_btn)
        ctrl_layout.addWidget(self.clear_btn)
        layout.addLayout(ctrl_layout)

        # Log
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setStyleSheet("background-color: #1e1e1e; color: #ffffff;")
        self.log_display.setFont(QFont("Consolas", 10))
        layout.addWidget(self.log_display)

        # Progress Bar 
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("Ready")
        self.progress_bar.setMinimumHeight(25)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("Ready")
        layout.addWidget(self.status_label)

    def open_config_dialog(self):
        dialog = ConfigDialog(self.data_rate, self.peak_rate, self.preview_frames, self.bink_version, self)
        if dialog.exec():
            self.data_rate, self.peak_rate, self.preview_frames, self.bink_version = dialog.get_values()
            self.save_current_config()
            self.status_label.setText(f"Rates updated: {self.data_rate} / {self.peak_rate} | v{self.bink_version} | {self.preview_frames} preview frames")

    def browse_path(self, key, line_edit):
        if "EXE" in key:
            path, _ = QFileDialog.getOpenFileName(self, "Select Executable", filter="Executables (*.exe)")
        else:
            path = QFileDialog.getExistingDirectory(self, "Select Directory")
        if path:
            line_edit.setText(path)
            self.save_current_config()

    def save_current_config(self):
        for key, le in self.paths.items():
            self.config[key] = le.text()
        self.config["DATA_RATE"] = self.data_rate
        self.config["PEAK_RATE"] = self.peak_rate
        self.config["PREVIEW_FRAMES"] = self.preview_frames
        self.config["BINK_VERSION"] = self.bink_version
        self.config["SHOW_WINDOW"] = self.show_window_cb.isChecked()
        self.show_window = self.show_window_cb.isChecked()
        save_config(self.config)

    def append_log(self, status, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        color = "#FFFFFF" 
        if status == "GREEN": color = "#55FF55"
        elif status == "YELLOW": color = "#FFFF55"
        elif status == "RED": color = "#FF5555"

        log_entry = f'<span style="color:#888888">[{timestamp}]</span> <b style="color:{color}">[{status}]</b> {message}'
        self.log_display.append(log_entry)
        self.log_display.verticalScrollBar().setValue(self.log_display.verticalScrollBar().maximum())

    def tick_eta(self):
        if self.remaining_seconds > 0:
            self.remaining_seconds -= 1
            self.refresh_progress_bar_text()

    def refresh_progress_bar_text(self):
        if self.remaining_seconds < 0:
            time_str = "Calculating..."
        else:
            m, s = divmod(self.remaining_seconds, 60)
            h, m = divmod(m, 60)
            time_str = f"{h:02d}:{m:02d}:{s:02d}"

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(self.current_percent)
        self.progress_bar.setFormat(f"Processing Video {self.current_idx} of {self.total_count} | ETA: {time_str} (%p%)")

    def update_progress(self, current, total, eta_secs, percent):
        if total > 0:
            self.current_idx = current
            self.total_count = total
            self.current_percent = percent

            if percent >= 0: # Dynamic ETA logic achieved
                self.remaining_seconds = eta_secs
                if not self.countdown_timer.isActive():
                    self.countdown_timer.start(1000) # Tick every 1 second
                self.refresh_progress_bar_text()
            else: # Fallback to standard tracking
                self.countdown_timer.stop()
                self.progress_bar.setRange(0, total)
                self.progress_bar.setValue(current)
                self.progress_bar.setFormat(f"Processing Video {current} of {total} (%p%)")

    def start_batch(self):
        self.save_current_config() 
        self.log_display.clear()
        self.run_btn.setEnabled(False)
        self.config_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        
        # Reset progress bar state
        self.remaining_seconds = 0
        self.current_idx = 0
        self.total_count = 0
        self.current_percent = 0
        self.countdown_timer.stop()
        
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Initializing...")

        self.worker = BinkerWorker(self.config, self.data_rate, self.peak_rate, self.preview_frames, self.bink_version, self.show_window)
        self.worker.log_signal.connect(self.append_log)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.start()

    def abort_batch(self):
        if hasattr(self, 'worker') and self.worker.isRunning():
            self.worker.requestInterruption()
            self.cancel_btn.setEnabled(False)
            self.countdown_timer.stop()
            self.progress_bar.setFormat("Aborting... waiting for current file to finish.")

    def on_finished(self, success, failed):
        self.run_btn.setEnabled(True)
        self.config_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.countdown_timer.stop()
        self.status_label.setText(f"Complete. Success: {success} | Failed: {failed}")
        
        if success + failed > 0:
            self.progress_bar.setFormat("Batch Complete")
            self.progress_bar.setValue(self.progress_bar.maximum())

if __name__ == "__main__":
    myappid = 'alov.batchbinker.v1.3' 
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    app = QApplication(sys.argv)
    
    icon_path = resource_path("icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
        
    window = ALOVBatchBinker()
    window.show()
    sys.exit(app.exec())
