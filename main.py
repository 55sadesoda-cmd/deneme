
import sys
import json
import struct
import zipfile
import tempfile
import shutil
import zlib
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QInputDialog,
    QFileDialog,
    QComboBox,
    QSplitter,
    QAbstractItemView,
)
from PySide6.QtCore import Qt, QUrl, QTimer, QSize
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget


if sys.platform == "win32":
    import winreg
    import ctypes


APP_NAME = "Reapsot"
VERSION = "2.0"

DESKTOP = Path.home() / "Desktop"

MAGIC = b"REAPV2\x00\x00"
HEADER_LENGTH_STRUCT = "<I"
UINT32_MAX = 0xFFFFFFFF

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"
}

VIDEO_EXTENSIONS = {
    ".mp4", ".avi", ".mkv", ".mov", ".webm"
}

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".ogg", ".flac", ".m4a"
}


# =========================================================
# WINDOWS .REAPSOT DOSYA İLİŞKİLENDİRMESİ
# =========================================================

def register_reapsot_file_association():
    if sys.platform != "win32":
        return

    try:
        exe_path = Path(sys.executable).resolve()

        if exe_path.suffix.lower() != ".exe":
            return

        exe_path_str = str(exe_path)
        prog_id = "ReapsotFile"

        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Classes\.reapsot"
        ) as key:
            winreg.SetValueEx(
                key, "", 0, winreg.REG_SZ, prog_id
            )

        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\{prog_id}"
        ) as key:
            winreg.SetValueEx(
                key, "", 0, winreg.REG_SZ, "Reapsot V2 Arşivi"
            )

        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\{prog_id}\shell\open\command"
        ) as key:
            winreg.SetValueEx(
                key,
                "",
                0,
                winreg.REG_SZ,
                f'"{exe_path_str}" "%1"'
            )

        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\{prog_id}\DefaultIcon"
        ) as key:
            winreg.SetValueEx(
                key,
                "",
                0,
                winreg.REG_SZ,
                f'"{exe_path_str}",0'
            )

        try:
            ctypes.windll.shell32.SHChangeNotify(
                0x08000000,
                0x0000,
                None,
                None
            )
        except Exception:
            pass

    except Exception:
        pass


# =========================================================
# REAPSOT BINARY CONTAINER
#
# Layout:
#
#   8 bytes   MAGIC = REAPV2\0\0
#   4 bytes   header length (uint32 LE)
#   N bytes   JSON header
#   ...       raw file payloads
#
# Header contains item offsets and sizes.
#
# The archive is NOT ZIP/RAR and does not use zipfile.
# =========================================================

class ReapsotContainer:

    def __init__(self, path=None):
        self.path = Path(path) if path else None

        self.data = {
            "format": "REAPSOT",
            "version": 2,
            "name": "",
            "background_music": None,
            "folders": [],
            "items": []
        }

        self._cache_dir = Path(
            tempfile.mkdtemp(prefix="reapsot_cache_")
        )
        self._extracted_cache = {}
        self.source_mode = "new"

    # -----------------------------------------------------
    # MEDYA TİPİ
    # -----------------------------------------------------

    @staticmethod
    def detect_type(path: Path):
        ext = path.suffix.lower()

        if ext in IMAGE_EXTENSIONS:
            return "image"

        if ext in VIDEO_EXTENSIONS:
            return "video"

        if ext in AUDIO_EXTENSIONS:
            return "audio"

        return "unknown"

    # -----------------------------------------------------
    # UNIQUE NAME
    # -----------------------------------------------------

    def _unique_filename(self, filename):
        used = {
            item["filename"]
            for item in self.data["items"]
        }

        candidate = filename
        counter = 1
        source = Path(filename)

        while candidate in used:
            candidate = (
                f"{source.stem}_{counter}"
                f"{source.suffix}"
            )
            counter += 1

        return candidate

    # -----------------------------------------------------
    # ADD FILE
    # -----------------------------------------------------

    def add_file(self, source, folder=""):
        source = Path(source)

        if not source.exists() or not source.is_file():
            raise FileNotFoundError(
                f"Dosya bulunamadı: {source}"
            )

        target_name = self._unique_filename(
            source.name
        )

        media_type = self.detect_type(source)

        self.data["items"].append({
            "filename": target_name,
            "original_name": source.name,
            "type": media_type,
            "folder": folder or "",
            "source_path": str(source.resolve())
        })

    # -----------------------------------------------------
    # ADD FOLDER
    # -----------------------------------------------------

    def add_folder(self, name):
        name = name.strip()

        if not name:
            return False

        if name in self.data["folders"]:
            return False

        self.data["folders"].append(name)
        return True

    # -----------------------------------------------------
    # SET FOLDER
    # -----------------------------------------------------

    def set_item_folder(self, index, folder):
        if 0 <= index < len(self.data["items"]):
            self.data["items"][index]["folder"] = (
                folder or ""
            )

    # -----------------------------------------------------
    # BACKGROUND MUSIC
    # -----------------------------------------------------

    def set_background_music(self, source):
        source = Path(source)

        if not source.exists() or not source.is_file():
            raise FileNotFoundError(
                f"Ses dosyası bulunamadı: {source}"
            )

        self.data["background_music"] = {
            "source_path": str(source.resolve()),
            "original_name": source.name
        }

    # -----------------------------------------------------
    # BUILD HEADER
    # -----------------------------------------------------

    def _build_payload_and_header(self):
        """
        Build a stable header and payload list.

        Header size depends on offsets.
        Offsets depend on header size.
        We solve that by iterating until the
        header length stabilizes.
        """

        items = self.data["items"]

        payloads = []
        for item in items:
            source_path = Path(item["source_path"])

            if not source_path.exists():
                raise FileNotFoundError(
                    f"Kaynak dosya artık bulunamıyor:\n{source_path}"
                )

            if not source_path.is_file():
                raise OSError(
                    f"Kaynak normal dosya değil:\n{source_path}"
                )

            payloads.append(source_path.read_bytes())

        background_payload = None
        background_meta = None

        bg = self.data.get("background_music")

        if bg:
            bg_path = Path(bg["source_path"])

            if not bg_path.exists():
                raise FileNotFoundError(
                    f"Arka plan müziği bulunamıyor:\n{bg_path}"
                )

            background_payload = bg_path.read_bytes()

        # Initial estimate.
        header_bytes = b"{}"

        for _ in range(20):
            base_offset = (
                len(MAGIC)
                + struct.calcsize(
                    HEADER_LENGTH_STRUCT
                )
                + len(header_bytes)
            )

            current_offset = base_offset
            item_records = []

            for item, payload in zip(
                items,
                payloads
            ):
                item_records.append({
                    "filename": item["filename"],
                    "original_name": item["original_name"],
                    "type": item["type"],
                    "folder": item.get("folder", ""),
                    "offset": current_offset,
                    "size": len(payload),
                    "crc32": zlib.crc32(payload) & UINT32_MAX
                })

                current_offset += len(payload)

            background_record = None

            if background_payload is not None:
                background_filename = (
                    bg.get(
                        "original_name",
                        "background_audio"
                    )
                )

                background_record = {
                    "filename": background_filename,
                    "offset": current_offset,
                    "size": len(background_payload),
                    "crc32": (
                        zlib.crc32(
                            background_payload
                        )
                        & UINT32_MAX
                    )
                }

                current_offset += len(
                    background_payload
                )

            header = {
                "format": "REAPSOT",
                "version": 2,
                "name": self.data.get(
                    "name",
                    ""
                ),
                "folders": self.data.get(
                    "folders",
                    []
                ),
                "items": item_records,
                "background_music": background_record,
                "file_count": len(item_records),
                "created_by": f"{APP_NAME} {VERSION}"
            }

            new_header_bytes = json.dumps(
                header,
                ensure_ascii=False,
                separators=(",", ":")
            ).encode("utf-8")

            if len(new_header_bytes) == len(header_bytes):
                header_bytes = new_header_bytes
                break

            header_bytes = new_header_bytes

        return (
            header_bytes,
            payloads,
            background_payload
        )

    # -----------------------------------------------------
    # SAVE
    # -----------------------------------------------------

    def save(self):
        if not self.path:
            raise ValueError(
                "Kaydedilecek dosya yolu bulunamadı."
            )

        self._ensure_source_paths_for_save()

        header_bytes, payloads, background_payload = (
            self._build_payload_and_header()
        )

        if len(header_bytes) > UINT32_MAX:
            raise ValueError(
                "Reapsot başlık alanı çok büyük."
            )

        self.path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        with open(self.path, "wb") as f:
            f.write(MAGIC)
            f.write(
                struct.pack(
                    HEADER_LENGTH_STRUCT,
                    len(header_bytes)
                )
            )
            f.write(header_bytes)

            for payload in payloads:
                f.write(payload)

            if background_payload is not None:
                f.write(background_payload)

        # Yeni kayıt artık V2 binary formatıdır.
        self.source_mode = "v2"
        # Yeni save sonrası önbelleği temizle.
        self.clear_cache()

    # -----------------------------------------------------
    # READ HEADER
    # -----------------------------------------------------

    def load(self):
        if not self.path or not self.path.exists():
            raise FileNotFoundError(
                "Reapsot dosyası bulunamadı."
            )

        # V2: our own binary format.
        with open(self.path, "rb") as f:
            prefix = f.read(len(MAGIC))

        if prefix == MAGIC:
            self._load_v2()
            self.source_mode = "v2"
            return

        # V1: previous ZIP-based format. Automatically detect it.
        if zipfile.is_zipfile(self.path):
            self._load_v1_zip()
            self.source_mode = "v1"
            return

        raise ValueError(
            "Geçerli bir Reapsot dosyası değil."
        )

    def _load_v2(self):
        with open(self.path, "rb") as f:
            magic = f.read(len(MAGIC))

            if magic != MAGIC:
                raise ValueError(
                    "Bu dosya geçerli bir Reapsot binary arşivi değil."
                )

            raw_length = f.read(
                struct.calcsize(HEADER_LENGTH_STRUCT)
            )

            if len(raw_length) != struct.calcsize(
                HEADER_LENGTH_STRUCT
            ):
                raise ValueError(
                    "Reapsot başlığı bozuk."
                )

            header_length = struct.unpack(
                HEADER_LENGTH_STRUCT,
                raw_length
            )[0]

            if header_length <= 0:
                raise ValueError(
                    "Geçersiz Reapsot başlık boyutu."
                )

            header_bytes = f.read(header_length)

            if len(header_bytes) != header_length:
                raise ValueError(
                    "Reapsot başlığı eksik."
                )

        try:
            header = json.loads(
                header_bytes.decode("utf-8")
            )
        except Exception as e:
            raise ValueError(
                f"Reapsot metadata okunamadı: {e}"
            ) from e

        if header.get("format") != "REAPSOT":
            raise ValueError(
                "Geçersiz Reapsot formatı."
            )

        if int(header.get("version", 0)) != 2:
            raise ValueError(
                "Desteklenmeyen Reapsot binary sürümü."
            )

        self.data = {
            "format": "REAPSOT",
            "version": 2,
            "name": header.get("name", ""),
            "folders": header.get("folders", []),
            "items": [],
            "background_music": header.get("background_music")
        }

        for item in header.get("items", []):
            clean_item = {
                "filename": item["filename"],
                "original_name": item["original_name"],
                "type": item["type"],
                "folder": item.get("folder", ""),
                "offset": int(item["offset"]),
                "size": int(item["size"]),
                "crc32": int(item.get("crc32", 0))
            }
            self.data["items"].append(clean_item)

        archive_size = self.path.stat().st_size

        for item in self.data["items"]:
            self._validate_range(
                item["offset"],
                item["size"],
                archive_size
            )

        bg = self.data.get("background_music")

        if bg:
            self._validate_range(
                int(bg["offset"]),
                int(bg["size"]),
                archive_size
            )

    def _load_v1_zip(self):
        temp = self._cache_dir / "v1"
        temp.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(self.path, "r") as archive:
                archive.extractall(temp)
        except Exception as e:
            raise ValueError(
                f"V1 Reapsot arşivi açılamadı: {e}"
            ) from e

        metadata = temp / "reapsot.json"

        if not metadata.exists():
            raise ValueError(
                "Eski Reapsot arşivinde reapsot.json bulunamadı."
            )

        try:
            with open(metadata, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception as e:
            raise ValueError(
                f"Eski Reapsot metadata okunamadı: {e}"
            ) from e

        self.data = loaded
        self.data.setdefault("format", "REAPSOT")
        self.data.setdefault("version", 1)
        self.data.setdefault("name", self.path.stem)
        self.data.setdefault("folders", [])
        self.data.setdefault("items", [])
        self.data.setdefault("background_music", None)

        for item in self.data["items"]:
            item.setdefault("folder", "")
            item.setdefault("source_path", str((temp / item["filename"]).resolve()))

            if not Path(item["source_path"]).exists():
                raise ValueError(
                    f"V1 arşivindeki dosya bulunamadı: {item['filename']}"
                )

        bg = self.data.get("background_music")
        if bg:
            # V1 stores only the filename, while V2 stores metadata.
            if isinstance(bg, str):
                bg_path = temp / bg
                if not bg_path.exists():
                    raise ValueError(
                        "V1 arşivindeki arka plan müziği bulunamadı."
                    )
                self.data["background_music"] = {
                    "source_path": str(bg_path.resolve()),
                    "original_name": bg_path.name
                }
            elif isinstance(bg, dict):
                bg_path = Path(bg.get("source_path", ""))
                if not bg_path.exists():
                    fallback = temp / bg.get("filename", bg.get("original_name", ""))
                    if fallback.exists():
                        bg["source_path"] = str(fallback.resolve())

    def _ensure_source_paths_for_save(self):
        """Make V2 items usable by the writer even after loading a V2 archive."""
        for index, item in enumerate(self.data.get("items", [])):
            source = item.get("source_path")
            if source and Path(source).exists():
                continue

            # A loaded V2 item is extracted only when needed.
            cached = self._extracted_cache.get(index)
            if not cached or not cached.exists():
                cached = self.extract_item_to_temp(index)

            item["source_path"] = str(cached.resolve())

        bg = self.data.get("background_music")
        if isinstance(bg, dict):
            source = bg.get("source_path")
            if source and Path(source).exists():
                return
            cached = self._extracted_cache.get("background")
            if not cached or not cached.exists():
                cached = self.extract_background_to_temp()
            if cached:
                bg["source_path"] = str(cached.resolve())

    # -----------------------------------------------------
    # RANGE VALIDATION
    # -----------------------------------------------------

    @staticmethod
    def _validate_range(offset, size, archive_size):
        if offset < 0 or size < 0:
            raise ValueError(
                "Reapsot arşivinde geçersiz offset/size."
            )

        end = offset + size

        if end < offset or end > archive_size:
            raise ValueError(
                "Reapsot arşivinde geçersiz dosya aralığı."
            )

    # -----------------------------------------------------
    # READ RANGE
    # -----------------------------------------------------

    def read_range(self, offset, size):
        if not self.path:
            raise ValueError(
                "Arşiv yolu yok."
            )

        with open(self.path, "rb") as f:
            f.seek(offset)
            data = f.read(size)

        if len(data) != size:
            raise IOError(
                "Reapsot içeriği eksik veya bozuk."
            )

        return data

    # -----------------------------------------------------
    # EXTRACT ONE TO TEMP
    # -----------------------------------------------------

    def extract_item_to_temp(self, index):
        if index < 0 or index >= len(
            self.data["items"]
        ):
            raise IndexError(
                "Geçersiz medya index'i."
            )

        item = self.data["items"][index]

        cached = self._extracted_cache.get(index)
        if cached and cached.exists():
            return cached

        # V1: file already extracted to cache by ZIP loader.
        source = item.get("source_path")
        if self.source_mode == "v1" and source:
            source_path = Path(source)
            if source_path.exists():
                self._extracted_cache[index] = source_path
                return source_path

        # V2: read only this byte range from the container.
        data = self.read_range(
            int(item["offset"]),
            int(item["size"])
        )

        expected_crc = int(
            item.get("crc32", 0)
        )

        if expected_crc:
            actual_crc = (
                zlib.crc32(data)
                & UINT32_MAX
            )

            if actual_crc != expected_crc:
                raise ValueError(
                    f"Arşiv doğrulaması başarısız: {item['original_name']}"
                )

        safe_name = Path(
            item["filename"]
        ).name

        output = self._cache_dir / (
            f"{index}_{safe_name}"
        )

        output.write_bytes(data)

        self._extracted_cache[index] = output
        return output

    # -----------------------------------------------------
    # BACKGROUND MUSIC TEMP
    # -----------------------------------------------------

    def extract_background_to_temp(self):
        bg = self.data.get("background_music")

        if not bg:
            return None

        cache_key = "background"

        cached = self._extracted_cache.get(cache_key)
        if cached and cached.exists():
            return cached

        # V1 compatibility.
        if self.source_mode == "v1" and isinstance(bg, dict):
            source = Path(bg.get("source_path", ""))
            if source.exists():
                self._extracted_cache[cache_key] = source
                return source

        # V2.
        data = self.read_range(
            int(bg["offset"]),
            int(bg["size"])
        )

        expected_crc = int(
            bg.get("crc32", 0)
        )

        if expected_crc:
            actual_crc = (
                zlib.crc32(data)
                & UINT32_MAX
            )

            if actual_crc != expected_crc:
                raise ValueError(
                    "Arka plan müziği bozuk."
                )

        safe_name = Path(
            bg["filename"]
        ).name

        output = self._cache_dir / (
            f"background_{safe_name}"
        )

        output.write_bytes(data)

        self._extracted_cache[cache_key] = output
        return output

    # -----------------------------------------------------
    # EXPORT
    # -----------------------------------------------------

    def export_item(self, index, destination):
        destination = Path(destination)

        if index < 0 or index >= len(
            self.data["items"]
        ):
            raise IndexError(
                "Geçersiz medya index'i."
            )

        item = self.data["items"][index]

        # V1: copy already-extracted source directly.
        source_path = item.get("source_path")
        if self.source_mode == "v1" and source_path:
            source = Path(source_path)
            if not source.exists():
                raise ValueError(
                    "Dışa aktarılacak V1 dosyası bulunamadı."
                )
            data = source.read_bytes()
        else:
            data = self.read_range(
                int(item["offset"]),
                int(item["size"])
            )

            expected_crc = int(
                item.get("crc32", 0)
            )

            if expected_crc:
                actual_crc = (
                    zlib.crc32(data)
                    & UINT32_MAX
                )

                if actual_crc != expected_crc:
                    raise ValueError(
                        "Dışa aktarılacak medya doğrulanamadı."
                    )

        destination = destination / item["original_name"]
        destination = self.unique_target_path(
            destination
        )

        destination.write_bytes(data)
        return destination

    @staticmethod
    def unique_target_path(path):
        path = Path(path)

        if not path.exists():
            return path

        counter = 1

        while True:
            candidate = path.with_name(
                f"{path.stem}_{counter}{path.suffix}"
            )

            if not candidate.exists():
                return candidate

            counter += 1

    # -----------------------------------------------------
    # CACHE
    # -----------------------------------------------------

    def clear_cache(self):
        self._extracted_cache.clear()

        for child in self._cache_dir.iterdir():
            try:
                if child.is_file():
                    child.unlink()
            except Exception:
                pass

    # -----------------------------------------------------
    # CLEANUP
    # -----------------------------------------------------

    def cleanup(self):
        try:
            shutil.rmtree(
                self._cache_dir,
                ignore_errors=True
            )
        except Exception:
            pass


# =========================================================
# MAIN WINDOW
# =========================================================

class ReapsotWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        self.reapsot = None
        self.current_media_index = -1
        self.is_fullscreen_mode = False

        self.media_player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)

        self.media_player.setAudioOutput(
            self.audio_output
        )

        self.video_widget = QVideoWidget()
        self.media_player.setVideoOutput(
            self.video_widget
        )

        self.background_player = QMediaPlayer(self)
        self.background_audio = QAudioOutput(self)

        self.background_player.setAudioOutput(
            self.background_audio
        )

        self.background_active = False

        self.image_timer = QTimer(self)
        self.image_timer.setSingleShot(True)
        self.image_timer.timeout.connect(
            self.next_media
        )

        self.media_player.mediaStatusChanged.connect(
            self.media_finished
        )

        self.setWindowTitle(
            f"{APP_NAME} v{VERSION}"
        )
        self.resize(1200, 760)

        self.create_main_menu()

    # =====================================================
    # COMMAND LINE
    # =====================================================

    def open_from_command_line(self):
        if len(sys.argv) < 2:
            return

        path = Path(
            sys.argv[1]
        )

        if not path.exists():
            return

        if path.suffix.lower() != ".reapsot":
            return

        try:
            if self.reapsot:
                self.reapsot.cleanup()

            self.reapsot = ReapsotContainer(
                path
            )
            self.reapsot.load()
            self.open_viewer()

        except Exception as e:
            QMessageBox.critical(
                self,
                "Reapsot Hatası",
                f"Dosya açılamadı:\n\n{e}"
            )

    # =====================================================
    # CLEAR
    # =====================================================

    def clear_window(self):
        old = self.centralWidget()

        if old:
            old.deleteLater()

    # =====================================================
    # MAIN MENU
    # =====================================================

    def create_main_menu(self):
        self.exit_fullscreen()

        self.image_timer.stop()
        self.media_player.stop()
        self.background_player.stop()

        self.clear_window()

        widget = QWidget()
        layout = QVBoxLayout()

        layout.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )

        title = QLabel("REAPSOT")
        title.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        title.setStyleSheet("""
            QLabel {
                font-size: 52px;
                font-weight: bold;
                margin-bottom: 20px;
            }
        """)

        subtitle = QLabel(
            "Medya arşiv sistemi"
        )
        subtitle.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        subtitle.setStyleSheet("""
            QLabel {
                font-size: 16px;
                margin-bottom: 25px;
            }
        """)

        new_button = QPushButton(
            "Yeni Reapsot Arşivi"
        )

        open_button = QPushButton(
            "Reapsot Aç"
        )

        new_button.setMinimumHeight(60)
        open_button.setMinimumHeight(60)

        new_button.clicked.connect(
            self.new_reapsot
        )
        open_button.clicked.connect(
            self.open_reapsot
        )

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(new_button)
        layout.addWidget(open_button)

        widget.setLayout(layout)
        self.setCentralWidget(widget)

    # =====================================================
    # NEW
    # =====================================================

    def new_reapsot(self):
        name, ok = QInputDialog.getText(
            self,
            "Yeni Reapsot V2",
            "Arşiv adı:"
        )

        if not ok:
            return

        name = name.strip()

        if not name:
            QMessageBox.warning(
                self,
                "Hata",
                "Arşiv adı boş bırakılamaz."
            )
            return

        if name.lower().endswith(
            ".reapsot"
        ):
            filename = name
        else:
            filename = name + ".reapsot"

        path = DESKTOP / filename

        if path.exists():
            answer = QMessageBox.question(
                self,
                "Dosya mevcut",
                "Bu dosya zaten var.\n"
                "Üzerine yazmak istiyor musun?"
            )

            if answer != QMessageBox.StandardButton.Yes:
                return

        if self.reapsot:
            self.reapsot.cleanup()

        self.reapsot = ReapsotContainer(
            path
        )

        self.reapsot.data["name"] = (
            path.stem
        )

        # Boş V2 archive.
        self.reapsot.save()

        self.open_editor()

    # =====================================================
    # OPEN
    # =====================================================

    def open_reapsot(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Reapsot Aç",
            str(DESKTOP),
            "Reapsot Dosyası (*.reapsot)"
        )

        if not path:
            return

        try:
            if self.reapsot:
                self.reapsot.cleanup()

            self.reapsot = ReapsotContainer(
                path
            )
            self.reapsot.load()
            self.open_editor()

        except Exception as e:
            QMessageBox.critical(
                self,
                "Hata",
                f"Reapsot dosyası açılamadı:\n\n{e}"
            )

    # =====================================================
    # EDITOR
    # =====================================================

    def open_editor(self):
        self.exit_fullscreen()

        self.image_timer.stop()
        self.media_player.stop()
        self.background_player.stop()

        self.clear_window()

        widget = QWidget()
        main_layout = QVBoxLayout()

        title = QLabel(
            f"{self.reapsot.data['name']} — Reapsot"
        )

        title.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )

        title.setStyleSheet("""
            QLabel {
                font-size: 28px;
                font-weight: bold;
                padding: 8px;
            }
        """)

        top_bar = QHBoxLayout()

        self.folder_combo = QComboBox()
        self.folder_combo.setMinimumWidth(220)

        self.refresh_folder_combo()

        self.folder_combo.currentIndexChanged.connect(
            self.refresh_editor_list
        )

        add_folder_button = QPushButton(
            "+ Klasör"
        )
        add_folder_button.clicked.connect(
            self.create_folder
        )

        all_folder_button = QPushButton(
            "Tümünü Göster"
        )
        all_folder_button.clicked.connect(
            self.show_all_editor_items
        )

        top_bar.addWidget(
            QLabel("Klasör:")
        )
        top_bar.addWidget(
            self.folder_combo
        )
        top_bar.addWidget(
            add_folder_button
        )
        top_bar.addWidget(
            all_folder_button
        )
        top_bar.addStretch()

        self.editor_list = QListWidget()

        self.editor_list.setIconSize(
            QSize(90, 70)
        )
        self.editor_list.setSpacing(4)

        self.editor_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )

        self.editor_list.currentRowChanged.connect(
            self.preview_editor_item
        )

        self.editor_preview = QLabel()
        self.editor_preview.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        self.editor_preview.setMinimumSize(
            520,
            420
        )
        self.editor_preview.setStyleSheet("""
            QLabel {
                background: #101010;
                color: white;
                border: 1px solid #444;
            }
        """)

        self.editor_video = QVideoWidget()
        self.editor_video.setMinimumSize(
            520,
            420
        )
        self.editor_video.setStyleSheet(
            "background: #000;"
        )
        self.editor_video.hide()

        preview_layout = QVBoxLayout()

        preview_layout.addWidget(
            self.editor_preview,
            1
        )

        preview_layout.addWidget(
            self.editor_video,
            1
        )

        preview_widget = QWidget()
        preview_widget.setLayout(
            preview_layout
        )

        splitter = QSplitter(
            Qt.Orientation.Horizontal
        )

        splitter.addWidget(
            self.editor_list
        )

        splitter.addWidget(
            preview_widget
        )

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        buttons1 = QHBoxLayout()

        add_button = QPushButton(
            "Fotoğraf / Video / Ses Ekle"
        )
        add_button.clicked.connect(
            self.add_media
        )

        music_button = QPushButton(
            "Arka Plan Müziği"
        )
        music_button.clicked.connect(
            self.add_background_music
        )

        delete_button = QPushButton(
            "Seçiliyi Sil"
        )
        delete_button.clicked.connect(
            self.delete_selected_media
        )

        move_button = QPushButton(
            "Klasöre Taşı"
        )
        move_button.clicked.connect(
            self.move_selected_to_folder
        )

        up_button = QPushButton(
            "↑ Yukarı"
        )
        up_button.clicked.connect(
            self.move_selected_up
        )

        down_button = QPushButton(
            "↓ Aşağı"
        )
        down_button.clicked.connect(
            self.move_selected_down
        )

        buttons1.addWidget(
            add_button
        )
        buttons1.addWidget(
            music_button
        )
        buttons1.addWidget(
            delete_button
        )
        buttons1.addWidget(
            move_button
        )
        buttons1.addWidget(
            up_button
        )
        buttons1.addWidget(
            down_button
        )

        buttons2 = QHBoxLayout()

        export_selected_button = QPushButton(
            "📤 Seçileni Dışa Aktar"
        )
        export_selected_button.clicked.connect(
            self.export_selected
        )

        export_all_button = QPushButton(
            "📦 Tümünü Dışa Aktar"
        )
        export_all_button.clicked.connect(
            self.export_all
        )

        save_button = QPushButton(
            "Kaydet"
        )
        save_button.clicked.connect(
            self.save_reapsot
        )

        back_button = QPushButton(
            "Ana Menü"
        )
        back_button.clicked.connect(
            self.create_main_menu
        )

        buttons2.addWidget(
            export_selected_button
        )
        buttons2.addWidget(
            export_all_button
        )
        buttons2.addWidget(
            save_button
        )
        buttons2.addWidget(
            back_button
        )

        main_layout.addWidget(title)
        main_layout.addLayout(top_bar)
        main_layout.addWidget(splitter, 1)
        main_layout.addLayout(buttons1)
        main_layout.addLayout(buttons2)

        widget.setLayout(main_layout)
        self.setCentralWidget(widget)

        self.refresh_editor_list()

    # =====================================================
    # FOLDERS
    # =====================================================

    def refresh_folder_combo(self):
        if not hasattr(
            self,
            "folder_combo"
        ):
            return

        current = self.folder_combo.currentText()

        self.folder_combo.blockSignals(True)

        self.folder_combo.clear()

        self.folder_combo.addItem(
            "Tüm Klasörler"
        )

        for folder in self.reapsot.data["folders"]:
            self.folder_combo.addItem(
                folder
            )

        index = self.folder_combo.findText(
            current
        )

        if index >= 0:
            self.folder_combo.setCurrentIndex(
                index
            )

        self.folder_combo.blockSignals(False)

    def create_folder(self):
        name, ok = QInputDialog.getText(
            self,
            "Yeni Klasör",
            "Klasör adı:"
        )

        if not ok:
            return

        name = name.strip()

        if not name:
            return

        if not self.reapsot.add_folder(name):
            QMessageBox.warning(
                self,
                "Hata",
                "Bu klasör zaten var veya isim geçersiz."
            )
            return

        self.refresh_folder_combo()

        self.folder_combo.setCurrentText(
            name
        )

        self.refresh_editor_list()

    def show_all_editor_items(self):
        self.folder_combo.setCurrentIndex(0)

    def current_editor_folder(self):
        value = self.folder_combo.currentText()

        if value == "Tüm Klasörler":
            return None

        return value

    # =====================================================
    # EDITOR LIST
    # =====================================================

    def refresh_editor_list(self):
        if not hasattr(
            self,
            "editor_list"
        ):
            return

        selected_global = None

        current_item = (
            self.editor_list.currentItem()
        )

        if current_item:
            selected_global = current_item.data(
                Qt.ItemDataRole.UserRole
            )

        self.editor_list.clear()

        filter_folder = (
            self.current_editor_folder()
        )

        icons = {
            "image": "📷",
            "video": "🎥",
            "audio": "🔊",
            "unknown": "📄"
        }

        for global_index, item in enumerate(
            self.reapsot.data["items"]
        ):
            folder = item.get(
                "folder",
                ""
            )

            if (
                filter_folder is not None
                and folder != filter_folder
            ):
                continue

            name = item["original_name"]

            if folder:
                name = (
                    f"[{folder}] {name}"
                )

            if item["type"] == "image":
                text = name
            else:
                text = (
                    f"{icons.get(item['type'], '📄')} "
                    f"{name}"
                )

            list_item = QListWidgetItem(
                text
            )

            list_item.setData(
                Qt.ItemDataRole.UserRole,
                global_index
            )

            if item["type"] == "image":
                try:
                    path = self.reapsot.extract_item_to_temp(
                        global_index
                    )

                    pixmap = QPixmap(
                        str(path)
                    )

                    if not pixmap.isNull():
                        thumb = pixmap.scaled(
                            90,
                            70,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation
                        )

                        list_item.setIcon(
                            QIcon(thumb)
                        )
                except Exception:
                    pass

            self.editor_list.addItem(
                list_item
            )

        if selected_global is not None:
            for row in range(
                self.editor_list.count()
            ):
                item = self.editor_list.item(row)

                if item.data(
                    Qt.ItemDataRole.UserRole
                ) == selected_global:
                    self.editor_list.setCurrentRow(
                        row
                    )
                    break

    def current_global_editor_index(self):
        item = self.editor_list.currentItem()

        if not item:
            return None

        return item.data(
            Qt.ItemDataRole.UserRole
        )

    # =====================================================
    # PREVIEW
    # =====================================================

    def preview_editor_item(self, row):
        index = self.current_global_editor_index()

        self.media_player.stop()
        self.editor_video.hide()
        self.editor_preview.show()

        if index is None:
            self.editor_preview.clear()
            self.editor_preview.setText(
                "Bir medya seçin."
            )
            return

        item = self.reapsot.data["items"][
            index
        ]

        try:
            path = self.reapsot.extract_item_to_temp(
                index
            )
        except Exception as e:
            self.editor_preview.setText(
                f"Dosya okunamadı:\n{e}"
            )
            return

        if item["type"] == "image":
            pixmap = QPixmap(str(path))

            if pixmap.isNull():
                self.editor_preview.setText(
                    "Fotoğraf açılamadı."
                )
                return

            self.set_editor_image(
                pixmap
            )

        elif item["type"] == "video":
            self.editor_preview.hide()
            self.editor_video.show()

            self.media_player.setVideoOutput(
                self.editor_video
            )

            self.media_player.setSource(
                QUrl.fromLocalFile(
                    str(path)
                )
            )

            self.media_player.play()

        elif item["type"] == "audio":
            self.editor_preview.setPixmap(
                QPixmap()
            )

            self.editor_preview.setText(
                "🔊\n\n"
                f"{item['original_name']}\n\n"
                "Ses dosyası seçildi."
            )

        else:
            self.editor_preview.setText(
                "Desteklenmeyen dosya."
            )

    def set_editor_image(self, pixmap):
        if pixmap.isNull():
            return

        scaled = pixmap.scaled(
            self.editor_preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )

        self.editor_preview.setPixmap(
            scaled
        )

    # =====================================================
    # ADD MEDIA
    # =====================================================

    def add_media(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Medya Ekle",
            "",
            "Medya Dosyaları "
            "(*.jpg *.jpeg *.png *.gif *.bmp *.webp "
            "*.mp4 *.avi *.mkv *.mov *.webm "
            "*.mp3 *.wav *.ogg *.flac *.m4a)"
        )

        if not files:
            return

        folder = self.current_editor_folder()

        if folder is None:
            folder = ""

        try:
            for file in files:
                self.reapsot.add_file(
                    file,
                    folder
                )

            self.refresh_editor_list()

        except Exception as e:
            QMessageBox.critical(
                self,
                "Hata",
                str(e)
            )

    # =====================================================
    # BACKGROUND MUSIC
    # =====================================================

    def add_background_music(self):
        file, _ = QFileDialog.getOpenFileName(
            self,
            "Arka Plan Müziği",
            "",
            "Ses Dosyaları "
            "(*.mp3 *.wav *.ogg *.flac *.m4a)"
        )

        if not file:
            return

        try:
            self.reapsot.set_background_music(
                file
            )

            QMessageBox.information(
                self,
                "Başarılı",
                "Arka plan müziği eklendi."
            )

        except Exception as e:
            QMessageBox.critical(
                self,
                "Hata",
                str(e)
            )

    # =====================================================
    # DELETE
    # =====================================================

    def delete_selected_media(self):
        indexes = sorted(
            [
                item.data(
                    Qt.ItemDataRole.UserRole
                )
                for item in
                self.editor_list.selectedItems()
            ],
            reverse=True
        )

        if not indexes:
            return

        for index in indexes:
            self.reapsot.data["items"].pop(
                index
            )

        self.reapsot.clear_cache()

        self.refresh_editor_list()

    # =====================================================
    # MOVE FOLDER
    # =====================================================

    def move_selected_to_folder(self):
        index = self.current_global_editor_index()

        if index is None:
            QMessageBox.warning(
                self,
                "Klasör",
                "Önce bir medya seç."
            )
            return

        folders = [
            "Ana Klasör"
        ] + list(
            self.reapsot.data["folders"]
        )

        folder, ok = QInputDialog.getItem(
            self,
            "Klasöre Taşı",
            "Hedef klasör:",
            folders,
            0,
            False
        )

        if not ok:
            return

        if folder == "Ana Klasör":
            folder = ""

        self.reapsot.set_item_folder(
            index,
            folder
        )

        self.refresh_editor_list()

    # =====================================================
    # REORDER
    # =====================================================

    def move_selected_up(self):
        index = self.current_global_editor_index()

        if index is None or index <= 0:
            return

        items = self.reapsot.data["items"]

        items[index - 1], items[index] = (
            items[index],
            items[index - 1]
        )

        self.refresh_editor_list()

    def move_selected_down(self):
        index = self.current_global_editor_index()

        if index is None:
            return

        items = self.reapsot.data["items"]

        if index >= len(items) - 1:
            return

        items[index + 1], items[index] = (
            items[index],
            items[index + 1]
        )

        self.refresh_editor_list()

    # =====================================================
    # EXPORT
    # =====================================================

    def choose_export_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Dışa Aktarma Klasörü Seç"
        )

        return Path(folder) if folder else None

    def export_selected(self):
        indexes = [
            item.data(
                Qt.ItemDataRole.UserRole
            )
            for item in
            self.editor_list.selectedItems()
        ]

        if not indexes:
            QMessageBox.warning(
                self,
                "Dışa Aktar",
                "Önce en az bir medya seç."
            )
            return

        destination = (
            self.choose_export_folder()
        )

        if not destination:
            return

        try:
            for index in indexes:
                item = self.reapsot.data["items"][
                    index
                ]

                folder = item.get(
                    "folder",
                    ""
                )

                target_folder = (
                    destination / folder
                    if folder
                    else destination
                )

                target_folder.mkdir(
                    parents=True,
                    exist_ok=True
                )

                self.reapsot.export_item(
                    index,
                    target_folder
                )

            QMessageBox.information(
                self,
                "Başarılı",
                f"{len(indexes)} medya dışa aktarıldı."
            )

        except Exception as e:
            QMessageBox.critical(
                self,
                "Dışa Aktarma Hatası",
                str(e)
            )

    def export_all(self):
        items = self.reapsot.data["items"]

        if not items:
            QMessageBox.information(
                self,
                "Dışa Aktar",
                "Arşivde medya yok."
            )
            return

        destination = (
            self.choose_export_folder()
        )

        if not destination:
            return

        try:
            exported = 0

            for index, item in enumerate(
                items
            ):
                folder = item.get(
                    "folder",
                    ""
                )

                target_folder = (
                    destination / folder
                    if folder
                    else destination
                )

                target_folder.mkdir(
                    parents=True,
                    exist_ok=True
                )

                self.reapsot.export_item(
                    index,
                    target_folder
                )

                exported += 1

            QMessageBox.information(
                self,
                "Başarılı",
                f"{exported} medya dışa aktarıldı."
            )

        except Exception as e:
            QMessageBox.critical(
                self,
                "Dışa Aktarma Hatası",
                str(e)
            )

    # =====================================================
    # SAVE
    # =====================================================

    def save_reapsot(self):
        try:
            self.reapsot.save()

            QMessageBox.information(
                self,
                "Kaydedildi",
                "Reapsot arşivi kaydedildi."
            )

        except Exception as e:
            QMessageBox.critical(
                self,
                "Kaydetme Hatası",
                str(e)
            )

    # =====================================================
    # VIEWER
    # =====================================================

    def open_viewer(self):
        self.image_timer.stop()
        self.media_player.stop()
        self.background_player.stop()

        self.clear_window()

        widget = QWidget()
        layout = QVBoxLayout()

        self.viewer_title = QLabel(
            self.reapsot.data["name"]
        )

        self.viewer_title.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )

        self.viewer_title.setStyleSheet("""
            QLabel {
                font-size: 30px;
                font-weight: bold;
            }
        """)

        self.viewer_list = QListWidget()
        self.viewer_list.setMinimumWidth(340)

        self.viewer_list.currentRowChanged.connect(
            self.show_media
        )

        self.viewer_preview = QLabel()
        self.viewer_preview.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        self.viewer_preview.setMinimumSize(
            500,
            400
        )
        self.viewer_preview.setStyleSheet("""
            QLabel {
                background: #101010;
                color: white;
                border: 1px solid #444;
            }
        """)

        self.video_widget.setMinimumSize(
            500,
            400
        )
        self.video_widget.setStyleSheet(
            "background: #000;"
        )
        self.video_widget.hide()

        right_layout = QVBoxLayout()

        right_layout.addWidget(
            self.viewer_preview,
            1
        )

        right_layout.addWidget(
            self.video_widget,
            1
        )

        content = QHBoxLayout()

        content.addWidget(
            self.viewer_list,
            1
        )

        content.addLayout(
            right_layout,
            4
        )

        controls = QHBoxLayout()

        self.previous_button = QPushButton(
            "◀ Önceki"
        )

        self.next_button = QPushButton(
            "Sonraki ▶"
        )

        self.fullscreen_button = QPushButton(
            "⛶ Büyüt / Tam Ekran"
        )

        self.previous_button.clicked.connect(
            self.previous_media
        )

        self.next_button.clicked.connect(
            self.next_media
        )

        self.fullscreen_button.clicked.connect(
            self.toggle_fullscreen
        )

        controls.addWidget(
            self.previous_button
        )

        controls.addWidget(
            self.next_button
        )

        controls.addWidget(
            self.fullscreen_button
        )

        layout.addWidget(
            self.viewer_title
        )

        layout.addLayout(
            content,
            1
        )

        layout.addLayout(
            controls
        )

        widget.setLayout(layout)

        self.setCentralWidget(widget)

        self.fill_viewer_list()
        self.start_background_music()

        if self.reapsot.data["items"]:
            QTimer.singleShot(
                300,
                lambda:
                self.viewer_list.setCurrentRow(0)
            )
        else:
            self.viewer_preview.setText(
                "Bu Reapsot arşivi boş."
            )

    def fill_viewer_list(self):
        self.viewer_list.clear()

        for index, item in enumerate(
            self.reapsot.data["items"]
        ):
            icon = {
                "image": "📷",
                "video": "🎥",
                "audio": "🔊",
                "unknown": "📄"
            }.get(
                item["type"],
                "📄"
            )

            folder = item.get(
                "folder",
                ""
            )

            text = item["original_name"]

            if folder:
                text = (
                    f"[{folder}] {text}"
                )

            list_item = QListWidgetItem(
                f"{icon}  {text}"
            )

            list_item.setData(
                Qt.ItemDataRole.UserRole,
                index
            )

            if item["type"] == "image":
                try:
                    path = self.reapsot.extract_item_to_temp(
                        index
                    )

                    pixmap = QPixmap(
                        str(path)
                    )

                    if not pixmap.isNull():
                        thumb = pixmap.scaled(
                            70,
                            55,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation
                        )

                        list_item.setIcon(
                            QIcon(thumb)
                        )
                except Exception:
                    pass

            self.viewer_list.addItem(
                list_item
            )

    # =====================================================
    # VIEW MEDIA
    # =====================================================

    def show_media(self, row):
        if row < 0:
            return

        if row >= len(
            self.reapsot.data["items"]
        ):
            return

        self.current_media_index = row

        self.image_timer.stop()
        self.media_player.stop()

        item = self.reapsot.data["items"][
            row
        ]

        try:
            path = (
                self.reapsot.extract_item_to_temp(
                    row
                )
            )
        except Exception as e:
            self.viewer_preview.show()
            self.video_widget.hide()
            self.viewer_preview.setText(
                f"Medya açılamadı:\n{e}"
            )
            return

        if item["type"] == "image":
            self.video_widget.hide()
            self.viewer_preview.show()

            pixmap = QPixmap(
                str(path)
            )

            if pixmap.isNull():
                self.viewer_preview.setText(
                    "Fotoğraf açılamadı."
                )

                self.image_timer.start(
                    5000
                )
                return

            self.set_viewer_image(
                pixmap
            )

            self.resume_background_music()

            self.image_timer.start(
                5000
            )

        elif item["type"] == "video":
            self.viewer_preview.hide()
            self.video_widget.show()

            self.pause_background_music()

            self.media_player.setVideoOutput(
                self.video_widget
            )

            self.media_player.setSource(
                QUrl.fromLocalFile(
                    str(path)
                )
            )

            self.media_player.play()

        elif item["type"] == "audio":
            self.video_widget.hide()
            self.viewer_preview.show()

            self.viewer_preview.setPixmap(
                QPixmap()
            )

            self.viewer_preview.setText(
                "🔊\n\n"
                f"{item['original_name']}\n\n"
                "Ses kaydı oynatılıyor..."
            )

            self.pause_background_music()

            self.media_player.setVideoOutput(
                None
            )

            self.media_player.setSource(
                QUrl.fromLocalFile(
                    str(path)
                )
            )

            self.media_player.play()

        else:
            self.video_widget.hide()
            self.viewer_preview.show()

            self.viewer_preview.setText(
                "Desteklenmeyen dosya."
            )

            self.image_timer.start(
                2000
            )

    def set_viewer_image(self, pixmap):
        if pixmap.isNull():
            return

        scaled = pixmap.scaled(
            self.viewer_preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )

        self.viewer_preview.setPixmap(
            scaled
        )

    def refresh_current_media(self):
        row = self.current_media_index

        if row < 0:
            return

        if row >= len(
            self.reapsot.data["items"]
        ):
            return

        item = self.reapsot.data["items"][
            row
        ]

        if item["type"] != "image":
            return

        try:
            path = self.reapsot.extract_item_to_temp(
                row
            )
        except Exception:
            return

        self.set_viewer_image(
            QPixmap(str(path))
        )

    # =====================================================
    # BACKGROUND MUSIC
    # =====================================================

    def start_background_music(self):
        bg = self.reapsot.data.get(
            "background_music"
        )

        if not bg:
            self.background_active = False
            return

        try:
            path = self.reapsot.extract_background_to_temp()

            if not path:
                self.background_active = False
                return

            self.background_player.stop()

            self.background_player.setSource(
                QUrl.fromLocalFile(
                    str(path)
                )
            )

            self.background_player.setLoops(
                QMediaPlayer.Loops.Infinite
            )

            self.background_player.play()

            self.background_active = True

        except Exception:
            self.background_active = False

    def pause_background_music(self):
        if self.background_active:
            self.background_player.pause()

    def resume_background_music(self):
        if self.background_active:
            self.background_player.play()

    # =====================================================
    # MEDIA FINISHED
    # =====================================================

    def media_finished(self, status):
        if (
            status ==
            QMediaPlayer.MediaStatus.EndOfMedia
        ):
            self.resume_background_music()
            self.next_media()

    # =====================================================
    # NEXT / PREVIOUS
    # =====================================================

    def next_media(self):
        count = self.viewer_list.count()

        if count == 0:
            return

        row = self.current_media_index + 1

        if row >= count:
            row = 0

        self.viewer_list.setCurrentRow(
            row
        )

    def previous_media(self):
        count = self.viewer_list.count()

        if count == 0:
            return

        self.image_timer.stop()

        row = self.current_media_index - 1

        if row < 0:
            row = count - 1

        self.viewer_list.setCurrentRow(
            row
        )

    # =====================================================
    # FULLSCREEN
    # =====================================================

    def toggle_fullscreen(self):
        if self.is_fullscreen_mode:
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self):
        self.is_fullscreen_mode = True

        self.viewer_list.hide()
        self.viewer_title.hide()
        self.previous_button.hide()
        self.next_button.hide()

        self.fullscreen_button.setText(
            "⛶ Tam Ekrandan Çık"
        )

        self.showFullScreen()

        QTimer.singleShot(
            100,
            self.refresh_current_media
        )

    def exit_fullscreen(self):
        if not hasattr(
            self,
            "viewer_list"
        ):
            return

        self.is_fullscreen_mode = False

        self.showNormal()

        self.viewer_list.show()
        self.viewer_title.show()
        self.previous_button.show()
        self.next_button.show()

        self.fullscreen_button.setText(
            "⛶ Büyüt / Tam Ekran"
        )

        QTimer.singleShot(
            100,
            self.refresh_current_media
        )

    # =====================================================
    # ESC
    # =====================================================

    def keyPressEvent(self, event):
        if (
            event.key() == Qt.Key.Key_Escape
            and self.is_fullscreen_mode
        ):
            self.exit_fullscreen()
            event.accept()
            return

        super().keyPressEvent(event)

    # =====================================================
    # RESIZE
    # =====================================================

    def resizeEvent(self, event):
        super().resizeEvent(event)

        QTimer.singleShot(
            50,
            self.refresh_current_media
        )

    # =====================================================
    # CLOSE
    # =====================================================

    def closeEvent(self, event):
        self.image_timer.stop()
        self.media_player.stop()
        self.background_player.stop()

        if self.reapsot:
            self.reapsot.cleanup()

        event.accept()


# =========================================================
# MAIN
# =========================================================

def main():
    app = QApplication(sys.argv)

    window = ReapsotWindow()

    if sys.platform == "win32":
        register_reapsot_file_association()

    window.show()
    window.open_from_command_line()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
