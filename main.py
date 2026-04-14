"""
PhotoSorter Pro  v1.1  — Android
Fixes:
  • Android 13/14 permissions (READ_MEDIA_IMAGES, READ_MEDIA_VIDEO)
  • MANAGE_EXTERNAL_STORAGE via Settings intent for Android 11+
  • Proper Kivy startup — no crash on launch
  • Graceful fallback when PIL/piexif/requests not installed yet
  • Tested against Android 14 (API 34) — Samsung S24 Ultra
"""

# ── Must set before any Kivy import ──────────────────────────────────
import os
os.environ.setdefault("KIVY_NO_ENV_CONFIG", "1")
os.environ.setdefault("KIVY_LOG_LEVEL", "warning")

import sys
import threading
import time
import re
import json
import shutil
import hashlib
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# ── Detect Android ────────────────────────────────────────────────────
IS_ANDROID = "ANDROID_ARGUMENT" in os.environ or os.path.exists("/system/build.prop")

# ── Kivy imports ──────────────────────────────────────────────────────
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.gridlayout import GridLayout
from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.progressbar import ProgressBar
from kivy.uix.spinner import Spinner
from kivy.uix.popup import Popup
from kivy.uix.filechooser import FileChooserListView
from kivy.clock import mainthread
from kivy.metrics import dp
from kivy.core.window import Window
from kivy.utils import get_color_from_hex

# ── Colours ───────────────────────────────────────────────────────────
BG     = get_color_from_hex("#0f0f0f")
CARD   = get_color_from_hex("#1a1a1a")
ACCENT = get_color_from_hex("#f0c040")
FG     = get_color_from_hex("#f0ede6")
MUTED  = get_color_from_hex("#888880")
GREEN  = get_color_from_hex("#6abf6a")
RED    = get_color_from_hex("#e05555")
BLUE   = get_color_from_hex("#5588cc")

Window.clearcolor = BG

# ── Optional heavy deps — loaded lazily so app doesn't crash if missing
PIL_OK     = False
HEIF_OK    = False
REQUESTS_OK = False
PIEXIF_OK  = False

def _load_optional_deps():
    global PIL_OK, HEIF_OK, REQUESTS_OK, PIEXIF_OK
    try:
        global Image, ImageOps, TAGS, GPSTAGS, ImageTk
        from PIL import Image, ImageOps
        from PIL.ExifTags import TAGS, GPSTAGS
        PIL_OK = True
    except ImportError:
        pass
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
        HEIF_OK = True
    except ImportError:
        pass
    try:
        global requests
        import requests
        REQUESTS_OK = True
    except ImportError:
        pass
    try:
        global piexif
        import piexif
        PIEXIF_OK = True
    except ImportError:
        pass

threading.Thread(target=_load_optional_deps, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════
#  ANDROID PERMISSIONS  (Android 6+ runtime, Android 11+ All Files)
# ══════════════════════════════════════════════════════════════════════

def request_android_permissions(callback=None):
    """
    Request all needed permissions for Android 13/14.
    On Android 11+ also opens the All Files Access settings page.
    """
    if not IS_ANDROID:
        if callback: callback(True)
        return

    try:
        from android.permissions import (
            request_permissions, check_permission, Permission
        )
        from android import mActivity
        from jnius import autoclass

        perms = [
            Permission.INTERNET,
            Permission.READ_EXTERNAL_STORAGE,
            Permission.WRITE_EXTERNAL_STORAGE,
        ]

        # Android 13+ (API 33) — granular media permissions
        Build = autoclass("android.os.Build")
        sdk   = Build.VERSION.SDK_INT
        if sdk >= 33:
            try:
                perms.append(Permission.READ_MEDIA_IMAGES)
                perms.append(Permission.READ_MEDIA_VIDEO)
            except AttributeError:
                perms.append("android.permission.READ_MEDIA_IMAGES")
                perms.append("android.permission.READ_MEDIA_VIDEO")

        def on_result(permissions, grants):
            all_ok = all(grants)
            # Android 11+ (API 30) — MANAGE_EXTERNAL_STORAGE via Settings
            if sdk >= 30:
                try:
                    Environment = autoclass("android.os.Environment")
                    if not Environment.isExternalStorageManager():
                        Intent = autoclass("android.content.Intent")
                        Settings = autoclass("android.provider.Settings")
                        Uri     = autoclass("android.net.Uri")
                        intent  = Intent(
                            Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
                        intent.setData(
                            Uri.parse(f"package:{mActivity.getPackageName()}"))
                        mActivity.startActivity(intent)
                except Exception:
                    pass
            if callback: callback(all_ok)

        request_permissions(perms, on_result)

    except Exception as e:
        print(f"Permission request error: {e}")
        if callback: callback(False)


def get_storage_root() -> str:
    """Return the best writable storage root on Android."""
    if not IS_ANDROID:
        return str(Path.home())
    try:
        from jnius import autoclass
        Environment = autoclass("android.os.Environment")
        return str(Environment.getExternalStorageDirectory().getPath())
    except Exception:
        for p in ["/sdcard", "/storage/emulated/0", "/storage/sdcard0"]:
            if os.path.exists(p):
                return p
        return str(Path.home())


# ══════════════════════════════════════════════════════════════════════
#  SHARED LOGIC
# ══════════════════════════════════════════════════════════════════════

PHOTO_EXT = {".jpg",".jpeg",".png",".heic",".heif",".tiff",".tif",
             ".webp",".dng",".raw",".bmp"}
VIDEO_EXT = {".mp4",".mkv",".mov",".avi",".wmv",".flv",".webm",
             ".m4v",".3gp",".ts",".mts",".m2ts"}
ALL_EXT   = PHOTO_EXT | VIDEO_EXT

MONTHS = ["01 - January","02 - February","03 - March","04 - April",
          "05 - May","06 - June","07 - July","08 - August",
          "09 - September","10 - October","11 - November","12 - December"]

_loc_cache: dict = {}


def get_photo_exif(path: Path) -> dict:
    if not PIL_OK: return {}
    try:
        img = Image.open(path)
        raw = None
        if hasattr(img, "_getexif"): raw = img._getexif()
        if raw is None and hasattr(img, "getexif"): raw = dict(img.getexif())
        return {TAGS.get(t, t): v for t, v in raw.items()} if raw else {}
    except Exception:
        return {}


def get_photo_gps(exif: dict):
    raw = exif.get("GPSInfo")
    if not raw: return None
    try:
        gps = {GPSTAGS.get(k, k): v for k, v in raw.items()}
    except Exception:
        return None

    def dec(dms, ref):
        try:
            vals = [x[0]/x[1] if isinstance(x, tuple) and x[1] else float(x)
                    for x in dms]
            d, m, s = vals
            v = d + m/60 + s/3600
            return -v if ref in ("S", "W") else v
        except Exception:
            return None

    lat = dec(gps.get("GPSLatitude", []), gps.get("GPSLatitudeRef", "N"))
    lon = dec(gps.get("GPSLongitude", []), gps.get("GPSLongitudeRef", "E"))
    return (lat, lon) if lat is not None and lon is not None else None


def get_photo_date(path: Path, exif: dict) -> datetime:
    s = exif.get("DateTimeOriginal") or exif.get("DateTime")
    if s:
        try: return datetime.strptime(str(s), "%Y:%m:%d %H:%M:%S")
        except Exception: pass
    return datetime.fromtimestamp(path.stat().st_mtime)


def find_ffmpeg() -> str:
    if IS_ANDROID:
        try:
            from jnius import autoclass
            mActivity = autoclass('org.kivy.android.PythonActivity').mActivity
            lib_dir = str(mActivity.getApplicationInfo().nativeLibraryDir)
            local_ff = os.path.join(lib_dir, "libffmpeg.so")
            if os.path.exists(local_ff):
                return local_ff
        except Exception:
            pass

    candidates = [
        "ffmpeg",
        "/data/data/com.termux/files/usr/bin/ffmpeg",
        "/data/user/0/com.termux/files/usr/bin/ffmpeg",
    ]
    for exe in candidates:
        try:
            import subprocess
            r = subprocess.run([exe, "-version"],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                return exe
        except Exception:
            pass
    return ""


def get_video_metadata(path: Path) -> dict:
    candidates = [
        "ffprobe",
        "/data/data/com.termux/files/usr/bin/ffprobe",
    ]
    probe = ""
    for exe in candidates:
        try:
            import subprocess
            r = subprocess.run([exe, "-version"],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                probe = exe; break
        except Exception:
            pass
    if not probe: return {}
    try:
        import subprocess
        r = subprocess.run(
            [probe, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=30)
        return json.loads(r.stdout) if r.returncode == 0 else {}
    except Exception:
        return {}


def get_video_gps(meta: dict):
    tags = {}
    tags.update(meta.get("format", {}).get("tags", {}))
    for s in meta.get("streams", []): tags.update(s.get("tags", {}))
    for key, val in tags.items():
        if not isinstance(val, str): continue
        if any(x in key.lower() for x in ("location", "gps", "©xyz")):
            m = re.match(r"([+-]\d{1,3}\.?\d*)([+-]\d{1,3}\.?\d*)", val.strip())
            if m:
                try:
                    lat, lon = float(m.group(1)), float(m.group(2))
                    if -90 <= lat <= 90 and -180 <= lon <= 180:
                        return (lat, lon)
                except Exception:
                    pass
    return None


def get_video_date(path: Path, meta: dict) -> datetime:
    tags = {}
    tags.update(meta.get("format", {}).get("tags", {}))
    for s in meta.get("streams", []): tags.update(s.get("tags", {}))
    for key, val in tags.items():
        if "creation_time" in key.lower() and isinstance(val, str):
            for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try: return datetime.strptime(val[:19], fmt[:19])
                except Exception: pass
    return datetime.fromtimestamp(path.stat().st_mtime)


def coords_to_city(lat: float, lon: float) -> str:
    if not REQUESTS_OK: return ""
    key = (round(lat, 2), round(lon, 2))
    if key in _loc_cache: return _loc_cache[key]
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 10},
            headers={"User-Agent": "PhotoSorterAndroid/1.1"},
            timeout=8)
        addr = r.json().get("address", {})
        city = (addr.get("city") or addr.get("town") or addr.get("village")
                or addr.get("county") or addr.get("state") or "")
        city = city.replace("/", "-").replace("\\", "-").strip()
        _loc_cache[key] = city
        time.sleep(1)
        return city
    except Exception:
        return ""


def sanitize(n: str) -> str:
    for ch in r'\/:*?"<>|': n = n.replace(ch, "-")
    return n.strip()


def date_folder_name(dt: datetime, cities: list) -> str:
    base = dt.strftime("%Y-%m-%d")
    if cities:
        loc  = ", ".join(cities)
        full = f"{base} {loc}"
        return sanitize(full[:180] if len(full) > 180 else full)
    return base


def photo_filename(city: str, dt: datetime, suffix: str) -> str:
    d, t = dt.strftime("%Y-%m-%d"), dt.strftime("%H-%M-%S")
    return sanitize(f"{city} {d} {t}" if city else f"{d} {t}") + suffix.lower()


def video_filename(city: str, dt: datetime, suffix: str) -> str:
    d, t = dt.strftime("%Y-%m-%d"), dt.strftime("%H-%M-%S")
    return sanitize(f"Video {city} {d} {t}" if city else f"Video {d} {t}") + suffix.lower()


def build_dest_dir(base: Path, dt: datetime, cities: list) -> Path:
    return base / str(dt.year) / MONTHS[dt.month-1] / date_folder_name(dt, cities)


def unique_path(dest_dir: Path, filename: str) -> Path:
    dest = dest_dir / filename
    if not dest.exists(): return dest
    stem, suf = Path(filename).stem, Path(filename).suffix
    i = 2
    while True:
        c = dest_dir / f"{stem} ({i}){suf}"
        if not c.exists(): return c
        i += 1


# ══════════════════════════════════════════════════════════════════════
#  UI HELPERS
# ══════════════════════════════════════════════════════════════════════

def mk_btn(text, on_press, bg=CARD, fg=ACCENT, height=dp(48), **kw):
    btn = Button(text=text, background_color=bg, color=fg,
                 size_hint_y=None, height=height, bold=True,
                 font_size=dp(14), **kw)
    btn.bind(on_press=on_press)
    return btn


def mk_label(text, color=MUTED, size=(1, None), height=dp(30),
             font_size=dp(12), bold=False, halign="left"):
    lbl = Label(text=text, color=color, size_hint=size,
                height=height, font_size=font_size, bold=bold,
                halign=halign, valign="middle")
    lbl.bind(size=lambda i, v: setattr(i, "text_size", (v[0], None)))
    return lbl


def mk_log():
    lbl = Label(text="", color=FG, size_hint_y=None,
                font_size=dp(11), halign="left", valign="top", markup=True)
    lbl.bind(texture_size=lbl.setter("size"))
    lbl.bind(size=lambda i, v: setattr(i, "text_size", (v[0], None)))
    return lbl


# ══════════════════════════════════════════════════════════════════════
#  FOLDER PICKER POPUP
# ══════════════════════════════════════════════════════════════════════

class FolderPicker(Popup):
    def __init__(self, callback, start="/sdcard", **kw):
        super().__init__(title="Select Folder", size_hint=(.95, .88), **kw)
        self.callback = callback

        root = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(8))

        # Current path label
        self._path_lbl = mk_label(f"Path: {start}", color=MUTED,
                                   height=dp(26), font_size=dp(11))
        root.add_widget(self._path_lbl)

        self.fc = FileChooserListView(
            path=start,
            dirselect=True,
            show_hidden=False,
        )
        self.fc.bind(path=lambda i, v: setattr(
            self._path_lbl, "text", f"Path: {v}"))
        root.add_widget(self.fc)

        btns = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(8))
        btns.add_widget(mk_btn("Cancel", self.dismiss, CARD, MUTED))
        btns.add_widget(mk_btn("Select This Folder", self._select, ACCENT, BG))
        root.add_widget(btns)
        self.content = root

    def _select(self, *a):
        path = self.fc.path
        if self.fc.selection:
            p = self.fc.selection[0]
            if os.path.isdir(p):
                path = p
        self.callback(path)
        self.dismiss()


# ══════════════════════════════════════════════════════════════════════
#  TAB 1 — SORT & RENAME
# ══════════════════════════════════════════════════════════════════════

class SortTab(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation="vertical",
                         spacing=dp(8), padding=dp(12), **kw)
        self._src = ""
        self._dst = ""
        self._cancel = False
        self._storage = get_storage_root()
        self._build()

    def _build(self):
        self.add_widget(mk_label("Sort & Rename", color=ACCENT,
                                  height=dp(44), font_size=dp(20), bold=True))
        self.add_widget(mk_label(
            "Sorts into  Year / Month / Date+City  and renames files",
            height=dp(28)))

        # Source
        r1 = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(6))
        self._src_lbl = mk_label("Source: not selected", height=dp(50),
                                  size=(.72, None))
        r1.add_widget(self._src_lbl)
        r1.add_widget(mk_btn("Browse", lambda *a: self._pick("src"),
                              size_hint_x=.28))
        self.add_widget(r1)

        # Dest
        r2 = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(6))
        self._dst_lbl = mk_label("Dest:    not selected", height=dp(50),
                                  size=(.72, None))
        r2.add_widget(self._dst_lbl)
        r2.add_widget(mk_btn("Browse", lambda *a: self._pick("dst"),
                              size_hint_x=.28))
        self.add_widget(r2)

        # Progress
        self._prog = ProgressBar(max=100, value=0,
                                  size_hint_y=None, height=dp(12))
        self.add_widget(self._prog)
        self._status = mk_label("Ready — select source and destination folders",
                                 color=FG, height=dp(32), font_size=dp(13))
        self.add_widget(self._status)

        # Log
        sv = ScrollView()
        self._log = mk_log()
        sv.add_widget(self._log)
        self.add_widget(sv)

        # Buttons
        br = BoxLayout(size_hint_y=None, height=dp(58), spacing=dp(8))
        self._start_btn = mk_btn("▶  Start Sorting", self._start, ACCENT, BG,
                                  height=dp(58))
        self._stop_btn  = mk_btn("⏹ Stop", self._stop, CARD, RED,
                                  height=dp(58), size_hint_x=.28)
        self._stop_btn.disabled = True
        br.add_widget(self._start_btn)
        br.add_widget(self._stop_btn)
        self.add_widget(br)

    def _pick(self, which):
        start = self._storage
        def cb(path):
            if which == "src":
                self._src = path
                self._src_lbl.text = f"Source: {os.path.basename(path) or path}"
                if not self._dst:
                    self._dst = os.path.join(
                        os.path.dirname(path), "Sorted_Photos")
                    self._dst_lbl.text = "Dest:    Sorted_Photos (auto)"
            else:
                self._dst = path
                self._dst_lbl.text = f"Dest:    {os.path.basename(path) or path}"
        FolderPicker(callback=cb, start=start).open()

    def _start(self, *a):
        if not self._src:
            self._set_status("⚠ Select a source folder first"); return
        if not Path(self._src).exists():
            self._set_status("⚠ Source folder not found"); return
        if self._src == self._dst:
            self._set_status("⚠ Source and destination must be different"); return
        self._cancel = False
        self._start_btn.disabled = True
        self._stop_btn.disabled  = False
        self._log.text = ""
        self._prog.value = 0
        threading.Thread(target=self._run,
                         args=(Path(self._src), Path(self._dst)),
                         daemon=True).start()

    def _stop(self, *a):
        self._cancel = True
        self._stop_btn.disabled = True

    @mainthread
    def _set_status(self, txt, color=None):
        self._status.text = txt
        if color: self._status.color = color

    @mainthread
    def _add_log(self, line, color="ffffff"):
        self._log.text += f"[color=#{color}]{line}[/color]\n"

    @mainthread
    def _set_prog(self, v):
        self._prog.value = v

    def _run(self, src: Path, dst: Path):
        try:
            files = [f for f in src.rglob("*")
                     if f.is_file() and f.suffix.lower() in ALL_EXT]
            total = len(files)
            if not files:
                self._set_status("⚠ No photos or videos found"); return

            self._set_status(f"Found {total} files — reading GPS… (pass 1/2)")

            file_meta   = {}
            date_cities = defaultdict(dict)

            for i, f in enumerate(files):
                if self._cancel: break
                try:
                    is_photo = f.suffix.lower() in PHOTO_EXT
                    if is_photo:
                        exif   = get_photo_exif(f)
                        dt     = get_photo_date(f, exif)
                        coords = get_photo_gps(exif)
                    else:
                        meta   = get_video_metadata(f)
                        dt     = get_video_date(f, meta)
                        coords = get_video_gps(meta)
                    city = coords_to_city(*coords) if coords else ""
                    file_meta[f] = (dt, city, is_photo)
                    date_key = (dt.year, dt.month, dt.strftime("%Y-%m-%d"))
                    if city: date_cities[date_key][city] = None
                except Exception as ex:
                    file_meta[f] = None
                    self._add_log(f"⚠ {f.name}: {ex}", "ffaa44")
                self._set_prog(int((i+1)/total*45))

            self._set_status("Moving & renaming files… (pass 2/2)")
            moved = errors = 0

            for i, f in enumerate(files):
                if self._cancel: break
                info = file_meta.get(f)
                if not info:
                    errors += 1; continue
                try:
                    dt, city, is_photo = info
                    date_key = (dt.year, dt.month, dt.strftime("%Y-%m-%d"))
                    cities   = list(date_cities[date_key].keys())
                    dest_dir = build_dest_dir(dst, dt, cities)
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    fname = (photo_filename(city, dt, f.suffix) if is_photo
                             else video_filename(city, dt, f.suffix))
                    dest  = unique_path(dest_dir, fname)
                    shutil.move(str(f), str(dest))
                    icon = "📷" if is_photo else "🎬"
                    self._add_log(f"{icon} {f.name} → {dest.name}", "6abf6a")
                    moved += 1
                except Exception as ex:
                    self._add_log(f"✗ {f.name}: {ex}", "e05555")
                    errors += 1
                self._set_prog(45 + int((i+1)/total*55))

            if self._cancel:
                self._set_status(f"Stopped — {moved} moved so far")
            else:
                self._set_status(f"✓ Done — {moved} sorted, {errors} error(s)",
                                  GREEN)
            self._set_prog(100)

        except Exception as ex:
            self._set_status(f"Fatal error: {ex}", RED)
        finally:
            self._start_btn.disabled = False
            self._stop_btn.disabled  = True


# ══════════════════════════════════════════════════════════════════════
#  TAB 2 — VIDEO COMPRESSOR
# ══════════════════════════════════════════════════════════════════════

PRESETS = {
    "Balanced H.265":    ("hevc", "28", "original"),
    "Space saver 720p":  ("hevc", "32", "1280:720"),
    "High quality H.265":("hevc", "24", "original"),
    "H.264 720p (compat)":("h264","32", "1280:720"),
}


class VideoTab(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation="vertical",
                         spacing=dp(8), padding=dp(12), **kw)
        self._folder  = ""
        self._entries = []
        self._cancel  = False
        self._proc    = None
        self._storage = get_storage_root()
        self._build()

    def _build(self):
        self.add_widget(mk_label("Video Compressor", color=ACCENT,
                                  height=dp(44), font_size=dp(20), bold=True))

        ff   = find_ffmpeg()
        col  = GREEN if ff else RED
        txt  = f"✓ ffmpeg: {ff}" if ff else "✗ ffmpeg not found — install Termux + pkg install ffmpeg"
        self.add_widget(mk_label(txt, color=col, height=dp(28)))

        # Folder
        fr = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(6))
        self._fold_lbl = mk_label("Folder: not selected",
                                   height=dp(50), size=(.72, None))
        fr.add_widget(self._fold_lbl)
        fr.add_widget(mk_btn("Browse", self._pick, size_hint_x=.28))
        self.add_widget(fr)

        # Preset + Load
        pr = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(6))
        self._preset = Spinner(
            text="Balanced H.265",
            values=list(PRESETS.keys()),
            background_color=CARD, color=FG,
            size_hint_x=.68, font_size=dp(13))
        load_btn = mk_btn("📂 Load", self._load,
                           BLUE, FG, size_hint_x=.32)
        pr.add_widget(self._preset)
        pr.add_widget(load_btn)
        self.add_widget(pr)

        # Start / Stop
        br = BoxLayout(size_hint_y=None, height=dp(58), spacing=dp(8))
        self._comp_btn = mk_btn("▶  Compress Selected", self._start,
                                 ACCENT, BG, height=dp(58))
        self._stop_btn = mk_btn("⏹", self._stop, CARD, RED,
                                 height=dp(58), size_hint_x=.2)
        self._stop_btn.disabled = True
        br.add_widget(self._comp_btn)
        br.add_widget(self._stop_btn)
        self.add_widget(br)

        self._prog = ProgressBar(max=100, value=0,
                                  size_hint_y=None, height=dp(12))
        self.add_widget(self._prog)
        self._status = mk_label("Ready", color=FG, height=dp(30),
                                  font_size=dp(13))
        self.add_widget(self._status)

        sv = ScrollView()
        self._list_grid = GridLayout(cols=1, spacing=dp(2), size_hint_y=None)
        self._list_grid.bind(minimum_height=self._list_grid.setter("height"))
        sv.add_widget(self._list_grid)
        self.add_widget(sv)

    def _pick(self, *a):
        def cb(path):
            self._folder = path
            self._fold_lbl.text = f"Folder: {os.path.basename(path) or path}"
        FolderPicker(callback=cb, start=self._storage).open()

    def _load(self, *a):
        if not self._folder:
            self._status.text = "⚠ Browse a folder first"; return
        p = Path(self._folder)
        if not p.exists():
            self._status.text = "⚠ Folder not found"; return
        self._entries = sorted(
            [f for f in p.rglob("*")
             if f.is_file() and f.suffix.lower() in VIDEO_EXT
             and "_compressed" not in f.stem],
            key=lambda x: str(x).lower())
        if not self._entries:
            self._status.text = "⚠ No videos found"; return
        self._list_grid.clear_widgets()
        for f in self._entries:
            try: kb = f.stat().st_size // 1024
            except: kb = 0
            sz = f"{kb:,}KB" if kb < 1024 else f"{kb/1024:.1f}MB"
            row = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(4))
            row.add_widget(mk_label(f.name, height=dp(42),
                                     size=(.55, None), font_size=dp(11)))
            row.add_widget(mk_label(sz, color=MUTED, height=dp(42),
                                     size=(.2, None), font_size=dp(11),
                                     halign="right"))
            stat = mk_label("—", color=MUTED, height=dp(42),
                             size=(.25, None), font_size=dp(11),
                             halign="center")
            row.add_widget(stat)
            row._stat = stat
            row._path = f
            self._list_grid.add_widget(row)
        self._status.text = f"Loaded {len(self._entries)} video(s)"

    def _get_stat(self, path):
        for row in self._list_grid.children:
            if hasattr(row, "_path") and row._path == path:
                return row._stat
        return None

    @mainthread
    def _upd_row(self, path, txt, col=None):
        s = self._get_stat(path)
        if s:
            s.text  = txt
            if col: s.color = col

    @mainthread
    def _upd(self, status=None, prog=None):
        if status: self._status.text = status
        if prog is not None: self._prog.value = prog

    def _start(self, *a):
        if not find_ffmpeg():
            self._status.text = ("⚠ ffmpeg not found.\n"
                                  "Install Termux from F-Droid → run: pkg install ffmpeg")
            return
        if not self._entries:
            self._status.text = "⚠ Load videos first"; return
        self._cancel = False
        self._comp_btn.disabled = True
        self._stop_btn.disabled = False
        threading.Thread(target=self._run, daemon=True).start()

    def _stop(self, *a):
        self._cancel = True
        if self._proc:
            try: self._proc.terminate()
            except Exception: pass
        self._stop_btn.disabled = True

    def _run(self):
        import subprocess
        ff     = find_ffmpeg()
        preset = self._preset.text
        codec, crf, scale = PRESETS[preset]
        total  = len(self._entries)
        done   = saved_mb = 0

        for i, vid in enumerate(self._entries):
            if self._cancel: break
            if not vid.exists(): done += 1; continue

            orig_mb = vid.stat().st_size / (1024*1024)
            self._upd(f"[{i+1}/{total}] {vid.name}", int(i/total*100))
            self._upd_row(vid, "compressing…", BLUE)

            out = vid.parent / f"{vid.stem}_compressed.mp4"
            ct  = 2
            while out.exists():
                out = vid.parent / f"{vid.stem}_compressed_{ct}.mp4"; ct += 1

            vc    = "libx265" if codec == "hevc" else "libx264"
            extra = ["-tag:v", "hvc1"] if codec == "hevc" else []
            cmd   = [ff, "-i", str(vid),
                     "-c:v", vc, "-crf", crf, "-preset", "medium",
                     "-c:a", "aac", "-b:a", "128k",
                     "-map_metadata", "0",
                     "-movflags", "+faststart+use_metadata_tags"] + extra
            if scale != "original":
                cmd += ["-vf",
                        f"scale={scale}:force_original_aspect_ratio=decrease"]
            cmd += ["-y", str(out)]

            ok = False
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._proc.wait(timeout=7200)
                ok = self._proc.returncode == 0 and out.exists()
                if not ok and codec == "hevc" and not self._cancel:
                    cmd2 = [ff, "-i", str(vid), "-c:v", "libx264",
                            "-crf", "28", "-preset", "medium",
                            "-c:a", "aac", "-b:a", "128k",
                            "-map_metadata", "0",
                            "-movflags", "+faststart"] + ["-y", str(out)]
                    self._proc = subprocess.Popen(
                        cmd2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self._proc.wait(timeout=7200)
                    ok = self._proc.returncode == 0 and out.exists()
            except Exception:
                ok = False
            finally:
                self._proc = None

            if ok:
                new_mb = out.stat().st_size / (1024*1024)
                if new_mb >= orig_mb:
                    try: out.unlink()
                    except Exception: pass
                    self._upd_row(vid, "already small", MUTED)
                else:
                    pct = int((1 - new_mb/orig_mb)*100)
                    saved_mb += orig_mb - new_mb
                    self._upd_row(vid, f"✓ {pct}% saved", GREEN)
            else:
                if out.exists():
                    try: out.unlink()
                    except Exception: pass
                self._upd_row(vid, "error", RED)

            done += 1
            self._upd(prog=int((i+1)/total*100))

        final = (f"Stopped — {done} done" if self._cancel
                 else f"✓ Done — {done} processed, ~{saved_mb:.1f}MB freed")
        self._upd(final, 100)
        self._comp_btn.disabled = False
        self._stop_btn.disabled = True


# ══════════════════════════════════════════════════════════════════════
#  TAB 3 — IMAGE CONVERTER
# ══════════════════════════════════════════════════════════════════════

IMG_EXT = {".heic", ".heif", ".png", ".bmp", ".tiff", ".tif",
           ".webp", ".jpg", ".jpeg", ".raw", ".dng"}


class ImageTab(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation="vertical",
                         spacing=dp(8), padding=dp(12), **kw)
        self._folder  = ""
        self._entries = []
        self._cancel  = False
        self._storage = get_storage_root()
        self._build()

    def _build(self):
        self.add_widget(mk_label("Image Converter", color=ACCENT,
                                  height=dp(44), font_size=dp(20), bold=True))

        # PIL status
        col = GREEN if PIL_OK else RED
        txt = "✓ Pillow ready" if PIL_OK else "✗ Pillow missing — pip install pillow"
        self.add_widget(mk_label(txt, color=col, height=dp(26)))

        # Folder
        fr = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(6))
        self._fold_lbl = mk_label("Folder: not selected",
                                   height=dp(50), size=(.72, None))
        fr.add_widget(self._fold_lbl)
        fr.add_widget(mk_btn("Browse", self._pick, size_hint_x=.28))
        self.add_widget(fr)

        # Settings
        sr = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(6))
        sr.add_widget(mk_label("Format:", height=dp(50),
                                size=(.22, None), font_size=dp(13)))
        self._fmt = Spinner(text="JPG", values=["JPG", "PNG", "WEBP"],
                            background_color=CARD, color=FG,
                            size_hint_x=.3, font_size=dp(13))
        sr.add_widget(self._fmt)
        sr.add_widget(mk_label("Quality:", height=dp(50),
                                size=(.22, None), font_size=dp(13)))
        self._qual = TextInput(text="85", multiline=False,
                               background_color=CARD, foreground_color=FG,
                               size_hint_x=.26, font_size=dp(14))
        sr.add_widget(self._qual)
        self.add_widget(sr)

        # Buttons
        br = BoxLayout(size_hint_y=None, height=dp(58), spacing=dp(8))
        br.add_widget(mk_btn("📂 Load Images", self._load,
                              BLUE, FG, height=dp(58), size_hint_x=.4))
        self._conv_btn = mk_btn("▶  Convert", self._start,
                                 ACCENT, BG, height=dp(58))
        br.add_widget(self._conv_btn)
        stop = mk_btn("⏹", lambda *a: setattr(self, "_cancel", True),
                       CARD, RED, height=dp(58), size_hint_x=.18)
        br.add_widget(stop)
        self.add_widget(br)

        self._prog = ProgressBar(max=100, value=0,
                                  size_hint_y=None, height=dp(12))
        self.add_widget(self._prog)
        self._status = mk_label("Ready", color=FG, height=dp(30),
                                  font_size=dp(13))
        self.add_widget(self._status)

        sv = ScrollView()
        self._grid = GridLayout(cols=1, spacing=dp(2), size_hint_y=None)
        self._grid.bind(minimum_height=self._grid.setter("height"))
        sv.add_widget(self._grid)
        self.add_widget(sv)

    def _pick(self, *a):
        def cb(path):
            self._folder = path
            self._fold_lbl.text = f"Folder: {os.path.basename(path) or path}"
        FolderPicker(callback=cb, start=self._storage).open()

    def _load(self, *a):
        if not self._folder:
            self._status.text = "⚠ Browse a folder first"; return
        p = Path(self._folder)
        self._entries = sorted(
            [f for f in p.rglob("*")
             if f.is_file() and f.suffix.lower() in IMG_EXT
             and "_converted" not in f.stem],
            key=lambda x: str(x).lower())
        if not self._entries:
            self._status.text = "⚠ No images found"; return
        self._grid.clear_widgets()
        for f in self._entries:
            try: kb = f.stat().st_size // 1024
            except: kb = 0
            sz = f"{kb:,}KB" if kb < 1024 else f"{kb/1024:.1f}MB"
            row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(4))
            row.add_widget(mk_label(f.name, height=dp(40),
                                     size=(.55, None), font_size=dp(11)))
            row.add_widget(mk_label(sz, color=MUTED, height=dp(40),
                                     size=(.2, None), font_size=dp(11),
                                     halign="right"))
            stat = mk_label("—", color=MUTED, height=dp(40),
                             size=(.25, None), font_size=dp(11),
                             halign="center")
            row.add_widget(stat)
            row._stat = stat
            row._path = f
            self._grid.add_widget(row)
        self._status.text = f"Loaded {len(self._entries)} image(s)"

    @mainthread
    def _upd_row(self, path, txt, col=None):
        for row in self._grid.children:
            if hasattr(row, "_path") and row._path == path:
                row._stat.text = txt
                if col: row._stat.color = col
                break

    @mainthread
    def _upd(self, status=None, prog=None):
        if status: self._status.text = status
        if prog is not None: self._prog.value = prog

    def _start(self, *a):
        if not PIL_OK:
            self._status.text = "⚠ Pillow not available"; return
        if not self._entries:
            self._status.text = "⚠ Load images first"; return
        self._cancel = False
        self._conv_btn.disabled = True
        try:    qual = int(self._qual.text)
        except: qual = 85
        threading.Thread(target=self._run,
                         args=(self._fmt.text, qual),
                         daemon=True).start()

    def _run(self, fmt, qual):
        from PIL import ImageOps as _IOps
        ext_map = {"JPG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
        pil_fmt = {"JPG": "JPEG", "PNG": "PNG", "WEBP": "WEBP"}
        out_ext = ext_map[fmt]
        out_fmt = pil_fmt[fmt]
        total = len(self._entries)
        done  = saved = 0

        for i, src in enumerate(self._entries):
            if self._cancel: break
            self._upd(f"[{i+1}/{total}] {src.name}",
                      int(i/total*100))
            self._upd_row(src, "converting…", BLUE)

            out = src.parent / (src.stem + "_converted" + out_ext)
            ct  = 2
            while out.exists():
                out = src.parent / (src.stem + f"_converted_{ct}" + out_ext)
                ct += 1
            try:
                img = Image.open(src)
                # Preserve EXIF
                exif_bytes = None
                try:
                    eo = img.getexif()
                    exif_bytes = eo.tobytes() if eo else None
                except Exception: pass
                if not exif_bytes and "exif" in img.info:
                    exif_bytes = img.info["exif"]

                img = _IOps.exif_transpose(img)  # fix orientation

                if exif_bytes and PIEXIF_OK:
                    try:
                        ed = piexif.load(exif_bytes)
                        if "0th" in ed:
                            ed["0th"][piexif.ImageIFD.Orientation] = 1
                        exif_bytes = piexif.dump(ed)
                    except Exception: pass

                if out_fmt in ("JPEG", "WEBP") and img.mode in ("RGBA", "P", "LA"):
                    bg = Image.new("RGB", img.size, (255, 255, 255))
                    try:
                        mask = img.split()[-1] if img.mode in ("RGBA","LA") else None
                        bg.paste(img, mask=mask)
                    except Exception:
                        bg.paste(img)
                    img = bg
                elif img.mode not in ("RGB", "L", "RGBA"):
                    img = img.convert("RGB")

                kw = {}
                if out_fmt == "JPEG":
                    kw = {"quality": qual, "optimize": True}
                elif out_fmt == "WEBP":
                    kw = {"quality": qual, "method": 4}
                elif out_fmt == "PNG":
                    comp = max(0, min(9, int((100-qual)/10)))
                    kw   = {"compress_level": comp, "optimize": True}
                if exif_bytes:
                    kw["exif"] = exif_bytes

                img.save(out, out_fmt, **kw)
                try:
                    st = src.stat()
                    os.utime(str(out), (st.st_atime, st.st_mtime))
                except Exception: pass

                orig_kb = src.stat().st_size // 1024
                new_kb  = out.stat().st_size // 1024
                pct     = int((1 - new_kb/orig_kb)*100) if orig_kb > 0 else 0
                saved  += (orig_kb - new_kb) / 1024
                self._upd_row(src, f"✓ {pct}% smaller", GREEN)
            except Exception as ex:
                self._upd_row(src, f"error: {str(ex)[:18]}", RED)
            done += 1
            self._upd(prog=int((i+1)/total*100))

        final = (f"Stopped" if self._cancel
                 else f"✓ Done — {done} converted, ~{saved:.1f}MB freed")
        self._upd(final, 100)
        self._conv_btn.disabled = False


# ══════════════════════════════════════════════════════════════════════
#  TAB 4 — DUPLICATE FINDER
# ══════════════════════════════════════════════════════════════════════

class DuplicateTab(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation="vertical", spacing=dp(8), padding=dp(12), **kw)
        self._folder = ""
        self._groups = []
        self._cancel = False
        self._storage = get_storage_root()
        self._build()

    def _build(self):
        self.add_widget(mk_label("Duplicate Finder", color=ACCENT, height=dp(44), font_size=dp(20), bold=True))

        # Folder selection
        fr = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(6))
        self._fold_lbl = mk_label("Folder: not selected", height=dp(50), size=(.72, None))
        fr.add_widget(self._fold_lbl)
        fr.add_widget(mk_btn("Browse", self._pick, size_hint_x=.28))
        self.add_widget(fr)

        br = BoxLayout(size_hint_y=None, height=dp(58), spacing=dp(8))
        self._scan_btn = mk_btn("🔍 Scan Duplicates", self._scan, ACCENT, BG, height=dp(58))
        br.add_widget(self._scan_btn)
        stop_btn = mk_btn("⏹", lambda *a: setattr(self, "_cancel", True), CARD, RED, height=dp(58), size_hint_x=.2)
        br.add_widget(stop_btn)
        self.add_widget(br)

        self._prog = ProgressBar(max=100, value=0, size_hint_y=None, height=dp(12))
        self.add_widget(self._prog)

        self._status = mk_label("Ready", color=FG, height=dp(30), font_size=dp(13))
        self.add_widget(self._status)

        sv = ScrollView()
        self._grid = GridLayout(cols=1, spacing=dp(4), size_hint_y=None)
        self._grid.bind(minimum_height=self._grid.setter("height"))
        sv.add_widget(self._grid)
        self.add_widget(sv)

    def _pick(self, *a):
        def cb(path):
            self._folder = path
            self._fold_lbl.text = f"Folder: {os.path.basename(path) or path}"
        FolderPicker(callback=cb, start=self._storage).open()

    def _scan(self, *a):
        if not self._folder:
            self._status.text = "⚠ Browse a folder first"
            return
        self._cancel = False
        self._scan_btn.disabled = True
        self._grid.clear_widgets()
        threading.Thread(target=self._run_scan, daemon=True).start()

    def _run_scan(self):
        try:
            p = Path(self._folder)
            files = [f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in ALL_EXT]
            total = len(files)
            if total == 0:
                self._upd("⚠ No matching media found", 0)
                return

            hashes = defaultdict(list)

            for i, f in enumerate(files):
                if self._cancel: break
                self._upd(f"Scanning [{i+1}/{total}] {f.name[:20]}", int((i/total)*90))

                is_photo = f.suffix.lower() in PHOTO_EXT
                h = None
                
                # Fast hash matching: size + prefix bytes
                try:
                    sz = f.stat().st_size
                    with open(f, 'rb') as fp:
                        prefix = fp.read(1024 * 256)
                    hasher = hashlib.md5()
                    hasher.update(f"{sz}".encode('utf-8'))
                    hasher.update(prefix)
                    h = hasher.hexdigest()
                except Exception:
                    pass

                if h is not None:
                    hashes[h].append(f)

            # Filter groups > 1
            groups = [grp for h, grp in hashes.items() if len(grp) > 1]
            self._groups = sorted(groups, key=lambda g: sum(x.stat().st_size for x in g), reverse=True)

            if self._cancel:
                self._upd("Stopped scanning", 100)
            else:
                self._upd(f"Found {len(self._groups)} duplicate groups", 100)
            self._render_groups()
        except Exception as e:
             self._upd(f"Error: {e}", 0)
        finally:
             self._scan_btn.disabled = False

    @mainthread
    def _upd(self, status, prog):
            self._status.text = status
            self._prog.value = prog

    @mainthread
    def _render_groups(self):
        self._grid.clear_widgets()
        for i, grp in enumerate(self._groups):
            box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(2), padding=dp(4))
            box.height = dp(30) + len(grp) * dp(34)

            # Sub-header
            total_sz = sum(f.stat().st_size for f in grp) / (1024*1024)
            lbl = mk_label(f"Group {i+1} ({len(grp)} files, {total_sz:.1f}MB)", color=ACCENT, height=dp(26))
            box.add_widget(lbl)

            for f in grp:
                row = BoxLayout(size_hint_y=None, height=dp(30), spacing=dp(4))
                sz = f.stat().st_size / (1024*1024)
                row.add_widget(mk_label(f" {f.name} ({sz:.1f}MB)", size=(.75, None), height=dp(30), font_size=dp(11)))
                del_btn = mk_btn("🗑 Del", lambda inst, path=f, r=row: self._del_file(path, r), CARD, RED, height=dp(30))
                del_btn.size_hint_x = .25
                row.add_widget(del_btn)
                box.add_widget(row)

            self._grid.add_widget(box)

    def _del_file(self, path, row_widget):
        try:
            path.unlink()
            row_widget.parent.remove_widget(row_widget)
        except Exception as e:
            self._status.text = f"⚠ Could not delete: {e}"


# ══════════════════════════════════════════════════════════════════════
#  MAIN APP — startup, permissions, tab setup
# ══════════════════════════════════════════════════════════════════════

class PhotoSorterApp(App):
    def build(self):
        self.title = "PhotoSorter Pro"
        # Build UI immediately so there's no blank screen
        self._root = self._build_ui()
        # Request permissions after UI is shown
        from kivy.clock import Clock
        Clock.schedule_once(self._request_perms, 0.5)
        return self._root

    def _build_ui(self):
        root = BoxLayout(orientation="vertical")

        # Title bar
        bar = BoxLayout(size_hint_y=None, height=dp(44),
                        padding=(dp(12), dp(6)))
        bar.add_widget(Label(text="[b]PhotoSorter Pro[/b]",
                              markup=True, color=ACCENT,
                              font_size=dp(18), halign="left"))
        root.add_widget(bar)

        # Permission status label (shown until permissions granted)
        self._perm_lbl = Label(
            text="Requesting storage permission…",
            color=ACCENT, size_hint_y=None, height=dp(28),
            font_size=dp(12))
        root.add_widget(self._perm_lbl)

        # Tabs
        tp = TabbedPanel(do_default_tab=False)
        tp.tab_width = Window.width / 3

        tabs = [
            ("Sort", SortTab),
            ("Video", VideoTab),
            ("Images", ImageTab),
            ("Duplicates", DuplicateTab),
        ]
        for title, cls in tabs:
            item = TabbedPanelItem(text=title, font_size=dp(14))
            item.add_widget(cls())
            tp.add_widget(item)

        root.add_widget(tp)
        return root

    def _request_perms(self, *a):
        def on_result(ok):
            txt = "✓ Storage access granted" if ok else \
                  "⚠ Storage permission denied — go to Settings → Apps → PhotoSorter → Permissions"
            col = GREEN if ok else RED
            self._perm_lbl.text  = txt
            self._perm_lbl.color = col
        request_android_permissions(callback=on_result)


# ══════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        PhotoSorterApp().run()
    except Exception as e:
        # Last-resort crash log to /sdcard/photosorter_crash.txt
        try:
            import traceback
            crash_path = os.path.join(get_storage_root(), "photosorter_crash.txt")
            with open(crash_path, "w") as f:
                traceback.print_exc(file=f)
        except Exception:
            pass
        raise
