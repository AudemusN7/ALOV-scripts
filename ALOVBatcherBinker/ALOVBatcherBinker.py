# Build via this command: py -m PyInstaller --noconsole --onefile --icon="icon.ico" --add-data "icon.ico;." --add-data "banner.png;." ALOVBatchBinker.py

import sys
import os
import glob
import ctypes
import subprocess
import json
import traceback
from datetime import datetime
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QTextEdit, QLabel, 
                             QFileDialog, QGroupBox, QLineEdit, QSizePolicy,
                             QDialog, QFormLayout, QDialogButtonBox, QProgressBar)
from PySide6.QtCore import QThread, Signal, Qt
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
        "INPUT_DIR": "", 
        "OUTPUT_DIR": "",
        "DATA_RATE": "4500000",
        "PEAK_RATE": "8000000"
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
    def __init__(self, data_rate, peak_rate, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bink Encoding Settings")
        layout = QFormLayout(self)

        self.data_rate_le = QLineEdit(str(data_rate))
        self.peak_rate_le = QLineEdit(str(peak_rate))

        layout.addRow("Data Rate (/d):", self.data_rate_le)
        layout.addRow("Peak Rate (/m):", self.peak_rate_le)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def get_values(self):
        return self.data_rate_le.text(), self.peak_rate_le.text()

class BinkerWorker(QThread):
    log_signal = Signal(str, str)
    finished_signal = Signal(int, int)
    progress_signal = Signal(int, int) # (Current File, Total Files)

    def __init__(self, config, data_rate, peak_rate):
        super().__init__()
        self.config = config
        self.data_rate = data_rate
        self.peak_rate = peak_rate
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
            self.progress_signal.emit(0, total_files)

            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE

            for index, mov_file in enumerate(mov_files):
                if self.isInterruptionRequested():
                    self.log_signal.emit("YELLOW", "Batch compression aborted.")
                    break

                base_name = os.path.basename(mov_file)
                bik_name = os.path.splitext(base_name)[0] + ".bik"
                output_file = os.path.normpath(os.path.join(output_dir, bik_name))
                input_file = os.path.normpath(mov_file)

                cmd = [
                    exe_path, "Bink2c", input_file, output_file, 
                    "/v200", f"/d{self.data_rate}", f"/m{self.peak_rate}", "/l-1", "/p8", "/#" 
                ]

                self.log_signal.emit("YELLOW", f"Compressing: {base_name}...")

                try:
                    # FIX: Send stdout and stderr to DEVNULL to prevent pipe deadlocks
                    result = subprocess.run(
                        cmd, 
                        stdout=subprocess.DEVNULL, 
                        stderr=subprocess.DEVNULL, 
                        startupinfo=si, 
                        shell=False
                    )
                    
                    if result.returncode == 0:
                        self.counts["SUCCESS"] += 1
                        self.log_signal.emit("GREEN", f"Finished: {bik_name}")
                    else:
                        self.counts["FAILED"] += 1
                        self.log_signal.emit("RED", f"Failed: {base_name} (Exit Code: {result.returncode})")
                except Exception as e:
                    self.counts["FAILED"] += 1
                    self.log_signal.emit("RED", f"Error on {base_name}: {str(e)}")

                # Update progress after each file finishes
                self.progress_signal.emit(index + 1, total_files)

        except Exception as e:
            self.log_signal.emit("RED", f"Fatal Crash: {str(e)}")
        finally:
            self.finished_signal.emit(self.counts["SUCCESS"], self.counts["FAILED"])

class ALOVBatchBinker(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ALOV Batch Binker v1.0")
        self.resize(1100, 850)
        
        self.config = load_config()
        self.data_rate = self.config.get("DATA_RATE", "4500000")
        self.peak_rate = self.config.get("PEAK_RATE", "8000000")
        
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
        for key, label in [("BINK_EXE_PATH", "Bink2ForUnreal EXE:"), ("INPUT_DIR", "Input Directory:"), ("OUTPUT_DIR", "Output Directory:")]:
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

        # Progress Bar (New in v1.2)
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
        dialog = ConfigDialog(self.data_rate, self.peak_rate, self)
        if dialog.exec():
            self.data_rate, self.peak_rate = dialog.get_values()
            self.save_current_config()
            self.status_label.setText(f"Rates updated: {self.data_rate} / {self.peak_rate}")

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

    def update_progress(self, current, total):
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)
            self.progress_bar.setFormat(f"Processing Video {current} of {total} (%p%)")

    def start_batch(self):
        self.save_current_config() 
        self.log_display.clear()
        self.run_btn.setEnabled(False)
        self.config_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        
        # Reset progress bar
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Initializing...")

        self.worker = BinkerWorker(self.config, self.data_rate, self.peak_rate)
        self.worker.log_signal.connect(self.append_log)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.start()

    def abort_batch(self):
        if hasattr(self, 'worker') and self.worker.isRunning():
            self.worker.requestInterruption()
            self.cancel_btn.setEnabled(False)
            self.progress_bar.setFormat("Aborting... waiting for current file to finish.")

    def on_finished(self, success, failed):
        self.run_btn.setEnabled(True)
        self.config_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.status_label.setText(f"Complete. Success: {success} | Failed: {failed}")
        
        if success + failed > 0:
            self.progress_bar.setFormat("Batch Complete")

if __name__ == "__main__":
    myappid = 'alov.batchbinker.v1.0' 
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    app = QApplication(sys.argv)
    
    icon_path = resource_path("icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
        
    window = ALOVBatchBinker()
    window.show()
    sys.exit(app.exec())
