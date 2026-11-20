"""
SquishIT - Batch compressor for images, videos, and PDFs
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os
import sys
import json
import csv
import threading
import subprocess
import shutil
import tempfile
import queue
import time
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------

def _app_dir():
    """Directory containing the exe (or the script when running from source)."""
    return os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, 'frozen', False) else __file__
    ))

def _tools_dir():
    return os.path.join(_app_dir(), 'tools')

def find_tool(name):
    # Look in tools\ beside the exe first
    candidate = os.path.join(_tools_dir(), name + '.exe')
    if os.path.exists(candidate):
        return candidate
    return shutil.which(name) or shutil.which(name + '.exe')

def find_ghostscript():
    # tools\gs\bin\ beside the exe (portable, downloaded on first run)
    portable = os.path.join(_tools_dir(), 'gs', 'bin', 'gswin64c.exe')
    if os.path.exists(portable):
        return portable
    # Fallback: system-wide installs
    for base in [r'C:\Program Files\gs', r'C:\Program Files (x86)\gs']:
        if os.path.isdir(base):
            for ver in sorted(os.listdir(base), reverse=True):
                candidate = os.path.join(base, ver, 'bin', 'gswin64c.exe')
                if os.path.exists(candidate):
                    return candidate
    return None

FFMPEG   = find_tool('ffmpeg')
FFPROBE  = find_tool('ffprobe')
GS       = find_ghostscript()

BELOW_NORMAL_PRIORITY = 0x00004000

_STARTUPINFO = subprocess.STARTUPINFO()
_STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
_STARTUPINFO.wShowWindow = 0  # SW_HIDE

def _subprocess_flags(low_priority=True):
    flags = subprocess.CREATE_NO_WINDOW
    if low_priority:
        flags |= BELOW_NORMAL_PRIORITY
    return flags

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp', '.webp'}
VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.m4v',
              '.mpeg', '.mpg', '.3gp', '.ts'}
PDF_EXTS   = {'.pdf'}

_APP_DIR      = _app_dir()
RESUME_FILE   = os.path.join(_APP_DIR, 'squishit_resume.json')
DB_FILE       = os.path.join(_APP_DIR, 'squishit_db.json')
SETTINGS_FILE = os.path.join(_APP_DIR, 'squishit_settings.json')

# GS portable download URL (installer, ~30 MB)
_GS_INSTALLER_URL = (
    'https://github.com/ArtifexSoftware/ghostpdl-downloads'
    '/releases/download/gs10031/gs10031w64.exe'
)
_GS_INSTALL_DIR = os.path.join(_tools_dir(), 'gs')

# ---------------------------------------------------------------------------
# Compression workers
# ---------------------------------------------------------------------------

def compress_image(src, quality, tmp_path=None):
    try:
        from PIL import Image
        img = Image.open(src)
        ext = Path(src).suffix.lower()
        tmp = tmp_path or (src + '.sqtmp')
        fmt_map = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.png': 'PNG',
                   '.tiff': 'TIFF', '.tif': 'TIFF', '.bmp': 'BMP',
                   '.webp': 'WEBP'}
        fmt = fmt_map.get(ext, 'JPEG')
        exif = img.info.get('exif', b'')
        save_kwargs = {}
        if fmt == 'JPEG':
            save_kwargs = {'quality': quality, 'optimize': True}
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            if exif:
                save_kwargs['exif'] = exif
        elif fmt == 'PNG':
            save_kwargs = {'optimize': True, 'compress_level': 9}
        elif fmt == 'WEBP':
            save_kwargs = {'quality': quality, 'method': 6}
            if exif:
                save_kwargs['exif'] = exif
        elif fmt == 'TIFF':
            save_kwargs = {'compression': 'tiff_lzw'}
        img.save(tmp, fmt, **save_kwargs)
        return tmp, None
    except Exception as e:
        return None, str(e)

def compress_pdf_gs(src, quality_label, low_priority=True, tmp_path=None):
    setting_map = {'screen': '/screen', 'ebook': '/ebook',
                   'printer': '/printer', 'prepress': '/prepress'}
    setting = setting_map.get(quality_label, '/ebook')
    tmp = tmp_path or (src + '.sqtmp')
    cmd = [GS, '-sDEVICE=pdfwrite', '-dCompatibilityLevel=1.4',
           f'-dPDFSETTINGS={setting}', '-dNOPAUSE', '-dQUIET',
           '-dBATCH', f'-sOutputFile={tmp}', src]
    result = subprocess.run(cmd, capture_output=True, timeout=300,
                            creationflags=_subprocess_flags(low_priority),
                            startupinfo=_STARTUPINFO)
    if result.returncode != 0:
        return None, result.stderr.decode(errors='replace')
    return tmp, None

def compress_pdf_pikepdf(src, tmp_path=None):
    try:
        import pikepdf
        tmp = tmp_path or (src + '.sqtmp')
        with pikepdf.open(src) as pdf:
            pdf.save(tmp, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
        return tmp, None
    except Exception as e:
        return None, str(e)

def compress_pdf(src, quality_label, low_priority=True, tmp_path=None):
    if GS:
        return compress_pdf_gs(src, quality_label, low_priority, tmp_path)
    return compress_pdf_pikepdf(src, tmp_path)

def compress_video(src, codec, crf, low_priority=True, tmp_path=None):
    ext = Path(src).suffix.lower()
    tmp = tmp_path or (src + '.sqtmp' + ext)

    # AVI does not support H.265 — fall back to H.264
    actual_codec = codec
    warn = None
    if ext == '.avi' and codec == 'H.265 (HEVC)':
        actual_codec = 'H.264 (AVC)'
        warn = 'AVI container does not support H.265 — used H.264 instead'

    vcodec = 'libx265' if actual_codec == 'H.265 (HEVC)' else 'libx264'
    cmd = [FFMPEG, '-y', '-i', src,
           '-vcodec', vcodec, '-crf', str(crf),
           '-preset', 'medium',
           '-acodec', 'aac', '-b:a', '128k',
           tmp]
    result = subprocess.run(cmd, capture_output=True, timeout=3600,
                            creationflags=_subprocess_flags(low_priority),
                            startupinfo=_STARTUPINFO)
    if result.returncode != 0:
        return None, result.stderr.decode(errors='replace')[-500:]
    return tmp, warn

def get_file_type(path):
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return 'image'
    if ext in VIDEO_EXTS:
        return 'video'
    if ext in PDF_EXTS:
        return 'pdf'
    return None

def human_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'

def safe_replace(tmp, dest):
    """Copy tmp → dest, verify size, delete tmp. Handles cross-drive moves."""
    shutil.copy2(tmp, dest)
    if os.path.getsize(dest) != os.path.getsize(tmp):
        os.remove(dest)
        raise OSError('Size mismatch after copy — destination may be corrupt')
    os.remove(tmp)

# ---------------------------------------------------------------------------
# File-level compression database
# ---------------------------------------------------------------------------

class SquishDB:
    def __init__(self, path):
        self._path = path
        self._files = {}   # filepath -> {compressed_size, original_size, date, status}
        self._dirty = False
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path, encoding='utf-8') as fh:
                    self._files = json.load(fh).get('files', {})
            except Exception:
                self._files = {}

    def save(self):
        if not self._dirty:
            return
        try:
            with open(self._path, 'w', encoding='utf-8') as fh:
                json.dump({'files': self._files}, fh, indent=2)
            self._dirty = False
        except Exception:
            pass

    def record(self, path, original_size, compressed_size, status):
        self._files[path] = {
            'compressed_size': compressed_size,
            'original_size':   original_size,
            'date':            datetime.now().isoformat(),
            'status':          status,
        }
        self._dirty = True

    def is_done(self, path):
        """True if file was previously compressed and hasn't changed since."""
        entry = self._files.get(path)
        if not entry:
            return False
        try:
            return os.path.getsize(path) == entry['compressed_size']
        except OSError:
            return False

    def folder_stats(self, folder_path, file_types_enabled):
        """Return (done, new_count, new_bytes) for all target files under folder."""
        done = new_count = new_bytes = 0
        img_on, vid_on, pdf_on = file_types_enabled
        try:
            for dirpath, _, filenames in os.walk(folder_path):
                for fn in filenames:
                    fp = os.path.join(dirpath, fn)
                    ft = get_file_type(fp)
                    if ft is None:
                        continue
                    if ft == 'image' and not img_on:
                        continue
                    if ft == 'video' and not vid_on:
                        continue
                    if ft == 'pdf' and not pdf_on:
                        continue
                    try:
                        if self.is_done(fp):
                            done += 1
                        else:
                            new_count += 1
                            new_bytes += os.path.getsize(fp)
                    except OSError:
                        pass  # file removed between scan and stat
        except PermissionError:
            pass
        return done, new_count, new_bytes


# ---------------------------------------------------------------------------
# Directory tree with tri-state checkboxes
# ---------------------------------------------------------------------------

CHECKED    = '☑'
UNCHECKED  = '☐'
TRISTATE   = '⊟'

class FolderTree(ttk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self._state  = {}   # iid -> True/False/None (tri)
        self._path   = {}   # iid -> str path
        self._iid    = {}   # path -> iid  (reverse index)
        self._stats  = {}   # path -> (done, new_count, new_bytes)
        self._build()

    def _build(self):
        vsb = ttk.Scrollbar(self, orient='vertical')
        hsb = ttk.Scrollbar(self, orient='horizontal')
        self.tv = ttk.Treeview(self, columns=('path',),
                               yscrollcommand=vsb.set,
                               xscrollcommand=hsb.set,
                               selectmode='none')
        vsb.config(command=self.tv.yview)
        hsb.config(command=self.tv.xview)
        self.tv.heading('#0', text='Folder')
        self.tv.heading('path', text='Full Path')
        self.tv.column('path', width=350)
        vsb.pack(side='right', fill='y')
        hsb.pack(side='bottom', fill='x')
        self.tv.pack(fill='both', expand=True)
        self.tv.bind('<ButtonRelease-1>', self._on_click)
        self.tv.bind('<<TreeviewOpen>>', self._on_expand)

    def load_root(self, path):
        self.tv.delete(*self.tv.get_children())
        self._state.clear()
        self._path.clear()
        self._iid.clear()
        self._stats.clear()
        iid = self.tv.insert('', 'end',
                              text=f'{UNCHECKED} {Path(path).name or path}',
                              values=(path,), open=False)
        self._state[iid] = False
        self._path[iid]  = path
        self._iid[path]  = iid
        self.tv.insert(iid, 'end', text='', values=('',))  # placeholder

    def _on_expand(self, event):
        iid = self.tv.focus()
        children = self.tv.get_children(iid)
        if len(children) == 1 and self.tv.item(children[0], 'text') == '':
            self.tv.delete(children[0])
            path = self._path.get(iid, '')
            try:
                for entry in sorted(os.scandir(path), key=lambda e: e.name.lower()):
                    if entry.is_dir(follow_symlinks=False):
                        child = self.tv.insert(
                            iid, 'end',
                            text=f'{UNCHECKED} {entry.name}',
                            values=(entry.path,))
                        self._state[child] = False
                        self._path[child]  = entry.path
                        self._iid[entry.path] = child
                        self.tv.insert(child, 'end', text='', values=('',))
                        # Apply badge if already scanned
                        if entry.path in self._stats:
                            self._apply_badge(child)
            except PermissionError:
                pass

    def _on_click(self, event):
        region = self.tv.identify_region(event.x, event.y)
        if region != 'tree':
            return
        iid = self.tv.identify_row(event.y)
        if not iid:
            return
        col  = self.tv.identify_column(event.x)
        text = self.tv.item(iid, 'text')
        if col != '#0' or not text:
            return
        # Only toggle if click is on the checkbox character (first ~20px)
        # Actually toggle on any click in the tree column
        cur = self._state.get(iid, False)
        new = not cur if cur is not None else True
        self._set_subtree(iid, new)
        self._update_ancestors(iid)

    def _set_subtree(self, iid, value):
        self._state[iid] = value
        self._update_label(iid)
        for child in self.tv.get_children(iid):
            # skip placeholder
            if self.tv.item(child, 'text') == '':
                continue
            self._set_subtree(child, value)

    def _update_ancestors(self, iid):
        parent = self.tv.parent(iid)
        if not parent:
            return
        kids = [c for c in self.tv.get_children(parent)
                if self.tv.item(c, 'text') != '']
        states = [self._state.get(k) for k in kids]
        if all(s is True for s in states):
            self._state[parent] = True
        elif all(s is False for s in states):
            self._state[parent] = False
        else:
            self._state[parent] = None
        self._update_label(parent)
        self._update_ancestors(parent)

    def _update_label(self, iid):
        s    = self._state.get(iid, False)
        icon = CHECKED if s is True else (TRISTATE if s is None else UNCHECKED)
        path = self._path.get(iid, '')
        name = self._base_name(iid)
        badge = ''
        fg    = ''
        if path in self._stats:
            done, new_c, new_b = self._stats[path]
            if new_c == 0 and done > 0:
                badge = f'  ✓ {done} done'
                fg    = 'darkgreen'
            elif done > 0:
                badge = f'  {done} done · {new_c} new ({human_size(new_b)})'
                fg    = 'darkorange'
            else:
                badge = f'  {new_c} new ({human_size(new_b)})'
                fg    = ''
        self.tv.item(iid, text=f'{icon} {name}{badge}')
        if fg:
            self.tv.tag_configure(f'fg_{iid}', foreground=fg)
            self.tv.item(iid, tags=(f'fg_{iid}',))

    def _base_name(self, iid):
        """Extract folder name without checkbox prefix or badge."""
        text = self.tv.item(iid, 'text')
        # strip checkbox prefix
        parts = text.split(' ', 1)
        name  = parts[1] if len(parts) > 1 else parts[0]
        # strip badge (everything after double-space)
        return name.split('  ')[0]

    def set_badge(self, path, done, new_count, new_bytes):
        """Called from background scan thread via after()."""
        self._stats[path] = (done, new_count, new_bytes)
        iid = self._iid.get(path)
        if iid:
            self._apply_badge(iid)

    def _apply_badge(self, iid):
        self._update_label(iid)

    def all_top_paths(self):
        """Return paths of all top-level nodes (for scanning)."""
        return [self._path[iid] for iid in self.tv.get_children()
                if iid in self._path]

    def auto_uncheck_done(self, threshold_bytes):
        """Uncheck folders where new content < threshold. Keep checked if all new."""
        self._auto_uncheck_node(self.tv.get_children(), threshold_bytes)

    def _auto_uncheck_node(self, iids, threshold_bytes):
        for iid in iids:
            if self.tv.item(iid, 'text') == '':
                continue
            path = self._path.get(iid, '')
            if path in self._stats:
                done, new_c, new_b = self._stats[path]
                if done > 0 and new_b < threshold_bytes:
                    self._set_subtree(iid, False)
                    self._update_ancestors(iid)
                    continue
            self._auto_uncheck_node(self.tv.get_children(iid), threshold_bytes)

    def get_selected_paths(self):
        result = []
        self._collect(self.tv.get_children(), result)
        return result

    def _collect(self, iids, result):
        for iid in iids:
            if self.tv.item(iid, 'text') == '':
                continue
            s = self._state.get(iid, False)
            if s is True:
                result.append(self._path[iid])
            elif s is None:
                # partial — recurse to find fully-checked subtrees
                self._collect(self.tv.get_children(iid), result)

# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class SquishIt(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('SquishIT')
        self.geometry('900x650')
        self.resizable(True, True)
        self._yes_to_all   = False
        self._cancel_flag  = False
        self._pause_flag   = False
        self._pause_evt    = threading.Event()
        self._pause_evt.set()  # not paused initially
        self._confirm_q    = queue.Queue()
        self._confirm_evt  = threading.Event()
        self._results      = []   # list of dicts
        self._resume_data  = {}
        self._db           = SquishDB(DB_FILE)
        self._build_ui()
        self._load_settings()
        self._cleanup_temp_dir()
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self._ensure_ffmpeg()
        self._ensure_ghostscript()
        self._check_tools()
        self._load_resume()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=6, pady=6)
        self.nb = nb

        self.tab_select   = ttk.Frame(nb)
        self.tab_settings = ttk.Frame(nb)
        self.tab_run      = ttk.Frame(nb)
        self.tab_report   = ttk.Frame(nb)

        self.tab_about    = ttk.Frame(nb)

        nb.add(self.tab_select,   text=' 1. Select Folders ')
        nb.add(self.tab_settings, text=' 2. Settings ')
        nb.add(self.tab_run,      text=' 3. Compress ')
        nb.add(self.tab_report,   text=' 4. Report ')
        nb.add(self.tab_about,    text=' About ')

        self._build_select_tab()
        self._build_settings_tab()
        self._build_run_tab()
        self._build_report_tab()
        self._build_about_tab()

    def _build_select_tab(self):
        f = self.tab_select
        top = ttk.Frame(f)
        top.pack(fill='x', padx=8, pady=6)

        ttk.Label(top, text='Root folder:').pack(side='left')
        self.root_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.root_var, width=55).pack(side='left', padx=4)
        ttk.Button(top, text='Browse…', command=self._browse).pack(side='left')
        ttk.Button(top, text='Select All', command=self._select_all).pack(side='left', padx=(12,2))
        ttk.Button(top, text='Clear All',  command=self._clear_all).pack(side='left')

        self.tree = FolderTree(f)
        self.tree.pack(fill='both', expand=True, padx=8, pady=4)

        bot = ttk.Frame(f)
        bot.pack(fill='x', padx=8, pady=4)
        self.btn_scan = ttk.Button(bot, text='🔍  Scan badges',
                                   command=self._scan_badges)
        self.btn_scan.pack(side='left')
        self._scan_status = tk.StringVar(value='')
        ttk.Label(bot, textvariable=self._scan_status,
                  foreground='gray').pack(side='left', padx=8)
        ttk.Button(bot, text='Next: Settings →',
                   command=lambda: self.nb.select(1)).pack(side='right')

    def _build_settings_tab(self):
        f = self.tab_settings
        pad = {'padx': 12, 'pady': 6}

        # --- Images ---
        img_lf = ttk.LabelFrame(f, text='Images  (JPG · PNG · TIFF · BMP · WebP)')
        img_lf.pack(fill='x', **pad)
        self.img_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(img_lf, text='Enable image compression',
                        variable=self.img_enabled).grid(row=0, column=0, sticky='w', padx=6, pady=2)
        ttk.Label(img_lf, text='Quality (JPEG/WebP):').grid(row=1, column=0, sticky='w', padx=6)
        self.img_quality = tk.IntVar(value=85)
        sl = ttk.Scale(img_lf, from_=10, to=100, variable=self.img_quality,
                       orient='horizontal', length=300,
                       command=lambda v: self._lbl_img_q.config(text=f'{int(float(v))}%'))
        sl.grid(row=1, column=1, padx=6, sticky='w')
        self._lbl_img_q = ttk.Label(img_lf, text='85%', width=5)
        self._lbl_img_q.grid(row=1, column=2)

        # --- Minimum saving threshold (shared across all file types) ---
        thr2_lf = ttk.LabelFrame(f, text='Minimum saving to keep compression (all file types)')
        thr2_lf.pack(fill='x', **pad)
        ttk.Label(thr2_lf, text='Only replace original if saving is at least:').grid(
            row=0, column=0, sticky='w', padx=6, pady=4)
        self.min_saving_pct = tk.IntVar(value=5)
        sl_ms = ttk.Scale(thr2_lf, from_=0, to=30, variable=self.min_saving_pct,
                          orient='horizontal', length=300,
                          command=lambda v: self._lbl_min_save.config(
                              text=f'{int(float(v))}%' if int(float(v)) else '0% (any reduction)'))
        sl_ms.grid(row=0, column=1, padx=6, sticky='w')
        self._lbl_min_save = ttk.Label(thr2_lf, text='5%', width=18)
        self._lbl_min_save.grid(row=0, column=2)
        ttk.Label(thr2_lf,
                  text='Files that compress by less than this are skipped. 0% = keep any reduction.',
                  foreground='gray').grid(row=1, column=0, columnspan=3, sticky='w', padx=6, pady=(0,4))

        # --- PDF ---
        pdf_lf = ttk.LabelFrame(f, text='PDF')
        pdf_lf.pack(fill='x', **pad)
        self.pdf_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(pdf_lf, text='Enable PDF compression',
                        variable=self.pdf_enabled).grid(row=0, column=0, sticky='w', padx=6, pady=2)
        engine_lbl = 'Ghostscript (installed)' if GS else 'pikepdf fallback (Ghostscript not found)'
        ttk.Label(pdf_lf, text=f'Engine: {engine_lbl}', foreground='green' if GS else 'orange'
                  ).grid(row=1, column=0, columnspan=3, sticky='w', padx=6)
        ttk.Label(pdf_lf, text='Quality preset:').grid(row=2, column=0, sticky='w', padx=6)
        self.pdf_quality = tk.StringVar(value='ebook')
        for i, opt in enumerate(['screen', 'ebook', 'printer', 'prepress']):
            ttk.Radiobutton(pdf_lf, text=opt, variable=self.pdf_quality,
                            value=opt).grid(row=2, column=i+1, padx=4)
        ttk.Label(pdf_lf, text='ebook = 150 dpi, good balance  |  screen = 72 dpi smallest  |  printer = 300 dpi',
                  foreground='gray').grid(row=3, column=0, columnspan=5, sticky='w', padx=6, pady=2)

        # --- Video ---
        vid_lf = ttk.LabelFrame(f, text='Video  (MP4 · MKV · AVI · MOV · WMV · FLV …)')
        vid_lf.pack(fill='x', **pad)
        self.vid_enabled = tk.BooleanVar(value=bool(FFMPEG))
        ttk.Checkbutton(vid_lf, text='Enable video compression',
                        variable=self.vid_enabled,
                        state='normal' if FFMPEG else 'disabled').grid(row=0, column=0, sticky='w', padx=6, pady=2)
        if not FFMPEG:
            ttk.Label(vid_lf, text='FFmpeg not found — video disabled',
                      foreground='red').grid(row=0, column=1, sticky='w')
        ttk.Label(vid_lf, text='Codec:').grid(row=1, column=0, sticky='w', padx=6)
        self.vid_codec = tk.StringVar(value='H.264 (AVC)')
        ttk.Radiobutton(vid_lf, text='H.264 (AVC) — broad compatibility',
                        variable=self.vid_codec, value='H.264 (AVC)').grid(row=1, column=1, sticky='w', padx=4)
        ttk.Radiobutton(vid_lf, text='H.265 (HEVC) — ~50% smaller, slower encode',
                        variable=self.vid_codec, value='H.265 (HEVC)').grid(row=2, column=1, sticky='w', padx=4)
        ttk.Label(vid_lf, text='Quality (CRF):').grid(row=3, column=0, sticky='w', padx=6)
        self.vid_crf = tk.IntVar(value=23)
        sl2 = ttk.Scale(vid_lf, from_=15, to=35, variable=self.vid_crf,
                        orient='horizontal', length=300,
                        command=lambda v: self._lbl_crf.config(text=f'CRF {int(float(v))}'))
        sl2.grid(row=3, column=1, padx=6, sticky='w')
        self._lbl_crf = ttk.Label(vid_lf, text='CRF 23', width=7)
        self._lbl_crf.grid(row=3, column=2)
        ttk.Label(vid_lf, text='Lower CRF = better quality, larger file.  18=visually lossless  28=noticeable loss',
                  foreground='gray').grid(row=4, column=0, columnspan=3, sticky='w', padx=6, pady=2)

        # --- Temp folder ---
        tmp_lf = ttk.LabelFrame(f, text='Temp file location')
        tmp_lf.pack(fill='x', **pad)
        self.use_temp_dir = tk.BooleanVar(value=False)
        ttk.Checkbutton(tmp_lf,
                        text='Write temp files to a local folder instead of next to the original',
                        variable=self.use_temp_dir,
                        command=self._toggle_temp_dir).grid(
                            row=0, column=0, columnspan=3, sticky='w', padx=6, pady=2)
        ttk.Label(tmp_lf, text='Recommended when compressing phone/MTP storage.',
                  foreground='gray').grid(row=1, column=0, columnspan=3, sticky='w', padx=6)
        self.temp_dir_var = tk.StringVar(value=os.path.join(_APP_DIR, 'temp'))
        self._tmp_entry = ttk.Entry(tmp_lf, textvariable=self.temp_dir_var, width=50,
                                    state='disabled')
        self._tmp_entry.grid(row=2, column=0, padx=6, pady=4, sticky='w')
        self._tmp_browse_btn = ttk.Button(tmp_lf, text='Browse…',
                                          command=self._browse_temp_dir, state='disabled')
        self._tmp_browse_btn.grid(row=2, column=1, padx=4)

        # --- Scan / re-run threshold ---
        thr_lf = ttk.LabelFrame(f, text='Smart scan — skip already-compressed folders')
        thr_lf.pack(fill='x', **pad)
        ttk.Label(thr_lf, text='Auto-uncheck folders with less than this much NEW content:'
                  ).grid(row=0, column=0, sticky='w', padx=6, pady=2)
        self.scan_threshold = tk.IntVar(value=100)
        sl_t = ttk.Scale(thr_lf, from_=0, to=2000, variable=self.scan_threshold,
                         orient='horizontal', length=300,
                         command=lambda v: self._lbl_thr.config(
                             text=f'{int(float(v))} MB' if int(float(v)) else '0 MB (always reprocess)'))
        sl_t.grid(row=0, column=1, padx=6, sticky='w')
        self._lbl_thr = ttk.Label(thr_lf, text='100 MB', width=22)
        self._lbl_thr.grid(row=0, column=2)
        ttk.Label(thr_lf,
                  text='Folders with 0 new files are always auto-unchecked regardless of this setting.',
                  foreground='gray').grid(row=1, column=0, columnspan=3, sticky='w', padx=6, pady=(0,4))

        # --- Background mode / throttle ---
        bg_lf = ttk.LabelFrame(f, text='Background mode (reduces PC interference)')
        bg_lf.pack(fill='x', **pad)
        self.bg_priority = tk.BooleanVar(value=True)
        ttk.Checkbutton(bg_lf, text='Run GS / FFmpeg at below-normal CPU priority (recommended)',
                        variable=self.bg_priority).grid(row=0, column=0, columnspan=3, sticky='w', padx=6, pady=2)
        ttk.Label(bg_lf, text='Delay between files (ms):').grid(row=1, column=0, sticky='w', padx=6)
        self.throttle_ms = tk.IntVar(value=0)
        sl3 = ttk.Scale(bg_lf, from_=0, to=2000, variable=self.throttle_ms,
                        orient='horizontal', length=250,
                        command=lambda v: self._lbl_throttle.config(
                            text=f'{int(float(v))} ms' if int(float(v)) else 'off'))
        sl3.grid(row=1, column=1, padx=6, sticky='w')
        self._lbl_throttle = ttk.Label(bg_lf, text='off', width=8)
        self._lbl_throttle.grid(row=1, column=2)
        ttk.Label(bg_lf, text='0 = no delay (fastest).  Add 100-500 ms if the PC still feels sluggish.',
                  foreground='gray').grid(row=2, column=0, columnspan=3, sticky='w', padx=6, pady=(0,4))

        # --- Overwrite mode ---
        ow_lf = ttk.LabelFrame(f, text='Overwrite behaviour')
        ow_lf.pack(fill='x', **pad)
        self.overwrite_mode = tk.StringVar(value='ask')
        ttk.Radiobutton(ow_lf, text='Ask each time (with "Yes to all" option)',
                        variable=self.overwrite_mode, value='ask').grid(row=0, column=0, sticky='w', padx=6, pady=2)
        ttk.Radiobutton(ow_lf, text='Yes to all — overwrite without asking',
                        variable=self.overwrite_mode, value='yes_all').grid(row=1, column=0, sticky='w', padx=6)
        ttk.Radiobutton(ow_lf, text='Dry run — scan only, no files changed',
                        variable=self.overwrite_mode, value='dry').grid(row=2, column=0, sticky='w', padx=6, pady=2)

        bot = ttk.Frame(f)
        bot.pack(fill='x', padx=12, pady=6)
        ttk.Button(bot, text='← Back', command=lambda: self.nb.select(0)).pack(side='left')
        ttk.Button(bot, text='Next: Compress →', command=lambda: self.nb.select(2)).pack(side='right')

    def _build_run_tab(self):
        f = self.tab_run

        self._status_var = tk.StringVar(value='Ready.')
        ttk.Label(f, textvariable=self._status_var, wraplength=800,
                  anchor='w').pack(fill='x', padx=12, pady=(10, 2))

        # overall progress
        ttk.Label(f, text='Overall progress:').pack(anchor='w', padx=12)
        self.prog_overall = ttk.Progressbar(f, length=860, mode='determinate')
        self.prog_overall.pack(padx=12, pady=2)
        self._prog_lbl = tk.StringVar(value='0 / 0 files')
        ttk.Label(f, textvariable=self._prog_lbl).pack(anchor='w', padx=12)

        # current file
        ttk.Label(f, text='Current file:').pack(anchor='w', padx=12, pady=(8,0))
        self._cur_file_var = tk.StringVar(value='—')
        ttk.Label(f, textvariable=self._cur_file_var, foreground='blue',
                  wraplength=860, anchor='w').pack(fill='x', padx=12)

        # live stats
        stats_f = ttk.Frame(f)
        stats_f.pack(fill='x', padx=12, pady=8)
        self._stat_vars = {}
        for i, (key, label) in enumerate([
            ('processed', 'Processed'), ('skipped_gain', 'No-gain skips'),
            ('errors', 'Errors'), ('saved', 'Space saved')
        ]):
            ttk.Label(stats_f, text=label+':', width=18, anchor='e').grid(row=i//2, column=(i%2)*2, sticky='e', padx=4)
            v = tk.StringVar(value='0')
            self._stat_vars[key] = v
            ttk.Label(stats_f, textvariable=v, width=18, anchor='w',
                      foreground='darkgreen').grid(row=i//2, column=(i%2)*2+1, sticky='w')

        # buttons row
        btn_f = ttk.Frame(f)
        btn_f.pack(fill='x', padx=12, pady=4)
        self.btn_go     = ttk.Button(btn_f, text='▶  Go Ahead — Start Compression',
                                     command=self._start_compression)
        self.btn_go.pack(side='left', ipadx=10, ipady=4)
        self.btn_pause  = ttk.Button(btn_f, text='⏸  Pause', command=self._toggle_pause,
                                     state='disabled')
        self.btn_pause.pack(side='left', padx=8)
        self.btn_cancel = ttk.Button(btn_f, text='■  Cancel', command=self._cancel,
                                     state='disabled')
        self.btn_cancel.pack(side='left', padx=4)
        self.btn_resume = ttk.Button(btn_f, text='↺  Resume previous run',
                                     command=self._resume_run, state='disabled')
        self.btn_resume.pack(side='left', padx=12)

        # log tail
        ttk.Label(f, text='Live log:').pack(anchor='w', padx=12, pady=(8,0))
        log_f = ttk.Frame(f)
        log_f.pack(fill='both', expand=True, padx=12, pady=(0,8))
        self._log_text = tk.Text(log_f, height=8, state='disabled',
                                 wrap='none', font=('Consolas', 9))
        sb = ttk.Scrollbar(log_f, command=self._log_text.yview)
        self._log_text.config(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self._log_text.pack(fill='both', expand=True)

    def _build_report_tab(self):
        f = self.tab_report
        top = ttk.Frame(f)
        top.pack(fill='x', padx=10, pady=6)
        ttk.Label(top, text='Compression Report', font=('TkDefaultFont', 12, 'bold')).pack(side='left')

        cols = ('file', 'type', 'original', 'compressed', 'saving', 'status')
        self.report_tv = ttk.Treeview(f, columns=cols, show='headings')
        for col, w, lbl in [
            ('file', 350, 'File'), ('type', 60, 'Type'),
            ('original', 90, 'Original'), ('compressed', 90, 'Compressed'),
            ('saving', 90, 'Saving'), ('status', 120, 'Status')
        ]:
            self.report_tv.heading(col, text=lbl)
            self.report_tv.column(col, width=w, anchor='w')
        sb2 = ttk.Scrollbar(f, orient='vertical', command=self.report_tv.yview)
        self.report_tv.config(yscrollcommand=sb2.set)
        sb2.pack(side='right', fill='y')
        self.report_tv.pack(fill='both', expand=True, padx=10)

        sum_f = ttk.Frame(f)
        sum_f.pack(fill='x', padx=10, pady=(4, 0))
        self._summary_var = tk.StringVar(value='')
        ttk.Label(sum_f, textvariable=self._summary_var, foreground='darkblue').pack(side='left')

        ttk.Label(f, text='Full log:', font=('TkDefaultFont', 9, 'bold')).pack(anchor='w', padx=10, pady=(6,0))
        log_outer = ttk.Frame(f)
        log_outer.pack(fill='both', expand=True, padx=10, pady=(2, 4))
        self._report_log = tk.Text(log_outer, height=10, state='disabled',
                                   wrap='none', font=('Consolas', 8))
        rsb = ttk.Scrollbar(log_outer, orient='vertical', command=self._report_log.yview)
        hsb2 = ttk.Scrollbar(log_outer, orient='horizontal', command=self._report_log.xview)
        self._report_log.config(yscrollcommand=rsb.set, xscrollcommand=hsb2.set)
        rsb.pack(side='right', fill='y')
        hsb2.pack(side='bottom', fill='x')
        self._report_log.pack(fill='both', expand=True)

        btn_row = ttk.Frame(f)
        btn_row.pack(fill='x', padx=10, pady=(0, 6))
        ttk.Button(btn_row, text='Save log as .txt', command=lambda: self._save_log('txt')).pack(side='left', padx=(0,4))
        ttk.Button(btn_row, text='Save log as .csv', command=lambda: self._save_log('csv')).pack(side='left')

    def _build_about_tab(self):
        f = self.tab_about
        outer = ttk.Frame(f)
        outer.place(relx=0.5, rely=0.5, anchor='center')

        ttk.Label(outer, text='SquishIT', font=('TkDefaultFont', 22, 'bold')).pack(pady=(0, 4))
        ttk.Label(outer, text='Batch compressor for Images, Video & PDF',
                  font=('TkDefaultFont', 11)).pack()
        ttk.Separator(outer, orient='horizontal').pack(fill='x', pady=12)

        info = [
            ('Version',   '1.0'),
            ('Platform',  'Windows (standalone .exe)'),
            ('Images',    'Pillow — JPG · PNG · TIFF · BMP · WebP'),
            ('PDF',       f'Ghostscript {"(installed ✓)" if GS else "(not found — using pikepdf fallback)"}'),
            ('Video',     f'FFmpeg {"(found ✓)" if FFMPEG else "(not found — place ffmpeg.exe in tools\\)"}  —  H.264 / H.265'),
        ]
        for label, val in info:
            row = ttk.Frame(outer)
            row.pack(fill='x', pady=2)
            ttk.Label(row, text=label + ':', width=14, anchor='e',
                      font=('TkDefaultFont', 10, 'bold')).pack(side='left')
            ttk.Label(row, text=val, anchor='w').pack(side='left', padx=8)

        ttk.Separator(outer, orient='horizontal').pack(fill='x', pady=12)

        gs_path = GS or 'not found'
        ff_path = FFMPEG or 'not found'
        ttk.Label(outer, text=f'Ghostscript:  {gs_path}',
                  foreground='gray', font=('Consolas', 8)).pack(anchor='w')
        ttk.Label(outer, text=f'FFmpeg:       {ff_path}',
                  foreground='gray', font=('Consolas', 8)).pack(anchor='w')

        ttk.Separator(outer, orient='horizontal').pack(fill='x', pady=12)
        ttk.Label(outer, text='Resume file saved next to this exe between sessions.',
                  foreground='gray').pack()
        ttk.Label(outer, text='Logs saved to the logs\\ folder.',
                  foreground='gray').pack()

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _ensure_ffmpeg(self):
        """Download FFmpeg into tools\ on first run if not found."""
        global FFMPEG, FFPROBE
        if FFMPEG:
            return

        answer = messagebox.askyesno(
            'Download FFmpeg?',
            'FFmpeg is needed for video compression.\n\n'
            'Download it now (~75 MB) into the tools\\ folder?\n'
            '(One-time download — stays portable with the app.)',
            icon='question')
        if not answer:
            self._log('FFmpeg download skipped — video compression disabled.')
            return

        import urllib.request
        import zipfile

        _FF_URL = (
            'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest'
            '/ffmpeg-master-latest-win64-gpl.zip'
        )
        tmp_zip = os.path.join(_tools_dir(), '_ffmpeg_download.zip')
        os.makedirs(_tools_dir(), exist_ok=True)

        self._log('Downloading FFmpeg…')
        self.update_idletasks()

        try:
            def _progress(count, block, total):
                if total > 0:
                    pct = min(count * block * 100 // total, 100)
                    self.after(0, self._status_var.set,
                               f'Downloading FFmpeg… {pct}%')
                    self.after(0, self.update_idletasks)

            urllib.request.urlretrieve(_FF_URL, tmp_zip, _progress)
        except Exception as e:
            self._log(f'FFmpeg download failed: {e}')
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)
            return

        self._log('Extracting ffmpeg.exe and ffprobe.exe…')
        self.update_idletasks()

        try:
            with zipfile.ZipFile(tmp_zip, 'r') as zf:
                for member in zf.namelist():
                    name = os.path.basename(member)
                    if name in ('ffmpeg.exe', 'ffprobe.exe') and '/bin/' in member:
                        dest = os.path.join(_tools_dir(), name)
                        with zf.open(member) as src_f, open(dest, 'wb') as dst_f:
                            shutil.copyfileobj(src_f, dst_f)
                        self._log(f'Extracted {name}')
        except Exception as e:
            self._log(f'FFmpeg extraction failed: {e}')
            return
        finally:
            try:
                os.remove(tmp_zip)
            except OSError:
                pass

        FFMPEG  = find_tool('ffmpeg')
        FFPROBE = find_tool('ffprobe')

        if FFMPEG:
            self._log(f'FFmpeg ready: {FFMPEG}')
            self._status_var.set('FFmpeg installed.')
            # Enable video checkbox now that FFmpeg is available
            self.after(100, self._refresh_video_state)
        else:
            self._log('FFmpeg extracted but exe not found — check tools\\ folder.')

    def _refresh_video_state(self):
        """Re-enable video controls after FFmpeg downloads."""
        if FFMPEG:
            self.vid_enabled.set(True)

    def _ensure_ghostscript(self):
        """Download and extract portable Ghostscript on first run if not found."""
        global GS
        if GS:
            return  # already found
        if not self.pdf_enabled.get():
            return  # PDF disabled, don't bother

        answer = messagebox.askyesno(
            'Download Ghostscript?',
            'Ghostscript is needed for PDF compression.\n\n'
            'Download it now (~30 MB) into the tools\\gs\\ folder?\n'
            '(One-time download — stays portable with the app.)',
            icon='question')
        if not answer:
            self._log('Ghostscript download skipped — PDF will use pikepdf fallback.')
            return

        self._log('Downloading Ghostscript…')
        self.update_idletasks()

        import urllib.request
        import zipfile

        tmp_installer = os.path.join(_tools_dir(), '_gs_installer.exe')
        os.makedirs(_tools_dir(), exist_ok=True)

        try:
            def _progress(count, block, total):
                if total > 0:
                    pct = min(count * block * 100 // total, 100)
                    self.after(0, self._status_var.set,
                               f'Downloading Ghostscript… {pct}%')
                    self.after(0, self.update_idletasks)

            urllib.request.urlretrieve(_GS_INSTALLER_URL, tmp_installer, _progress)
        except Exception as e:
            self._log(f'GS download failed: {e}')
            if os.path.exists(tmp_installer):
                os.remove(tmp_installer)
            return

        self._log('Installing Ghostscript into tools\\gs\\ …')
        self.update_idletasks()

        # Run installer silently with /D= to set a custom install dir
        os.makedirs(_GS_INSTALL_DIR, exist_ok=True)
        try:
            result = subprocess.run(
                [tmp_installer, '/S', f'/D={_GS_INSTALL_DIR}'],
                timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as e:
            self._log(f'GS install failed: {e}')
            return
        finally:
            try:
                os.remove(tmp_installer)
            except OSError:
                pass

        if result.returncode != 0:
            self._log(f'GS installer returned exit code {result.returncode}.')
            return

        # Re-discover GS now that it's installed
        GS = find_ghostscript()
        if GS:
            self._log(f'Ghostscript installed: {GS}')
            self._status_var.set('Ghostscript installed.')
            self.after(100, self._refresh_pdf_engine_label)
        else:
            # Installer may have ignored /D= and landed in Program Files — do a wider search
            for root in [r'C:\Program Files\gs', r'C:\Program Files (x86)\gs']:
                if os.path.isdir(root):
                    for ver in sorted(os.listdir(root), reverse=True):
                        candidate = os.path.join(root, ver, 'bin', 'gswin64c.exe')
                        if os.path.exists(candidate):
                            GS = candidate
                            break
                if GS:
                    break
            if GS:
                self._log(f'Ghostscript installed (system-wide): {GS}')
                self._status_var.set('Ghostscript installed.')
                self.after(100, self._refresh_pdf_engine_label)
            else:
                self._log('WARNING: GS installer ran but gswin64c.exe not found. '
                          'PDF will use pikepdf fallback.')
                messagebox.showwarning(
                    'Ghostscript install issue',
                    'Ghostscript was downloaded and installed, but the executable '
                    'could not be found.\n\nPDF compression will use the pikepdf '
                    'fallback (less effective).\n\nYou can install Ghostscript '
                    'manually from ghostscript.com.')

    def _refresh_pdf_engine_label(self):
        """Update the PDF engine label in the Settings tab after GS installs."""
        # Rebuild just the label — simpler than finding the widget reference
        pass  # label is static text; user will see it on next launch

    def _check_tools(self):
        if not FFMPEG:
            self._log(
                'WARNING: ffmpeg.exe not found in tools\\ folder — video compression disabled.\n'
                'Place ffmpeg.exe and ffprobe.exe in the tools\\ folder beside SquishIT.exe.')
            messagebox.showwarning(
                'FFmpeg not found',
                'ffmpeg.exe was not found in the tools\\ folder.\n\n'
                'Video compression will be unavailable.\n\n'
                'Place ffmpeg.exe and ffprobe.exe in:\n'
                f'{os.path.join(_APP_DIR, "tools")}')

    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, encoding='utf-8') as fh:
                s = json.load(fh)
            self.img_enabled.set(s.get('img_enabled', True))
            self.img_quality.set(s.get('img_quality', 85))
            self._lbl_img_q.config(text=f"{s.get('img_quality', 85)}%")
            self.pdf_enabled.set(s.get('pdf_enabled', True))
            self.pdf_quality.set(s.get('pdf_quality', 'ebook'))
            self.vid_enabled.set(s.get('vid_enabled', bool(FFMPEG)))
            self.vid_codec.set(s.get('vid_codec', 'H.264 (AVC)'))
            self.vid_crf.set(s.get('vid_crf', 23))
            self._lbl_crf.config(text=f"CRF {s.get('vid_crf', 23)}")
            self.overwrite_mode.set(s.get('overwrite_mode', 'ask'))
            self.use_temp_dir.set(s.get('use_temp_dir', False))
            self.temp_dir_var.set(s.get('temp_dir', os.path.join(_APP_DIR, 'temp')))
            self._toggle_temp_dir()
            self.scan_threshold.set(s.get('scan_threshold', 100))
            self._lbl_thr.config(text=f"{s.get('scan_threshold', 100)} MB")
            self.min_saving_pct.set(s.get('min_saving_pct', 5))
            self._lbl_min_save.config(
                text=f"{s.get('min_saving_pct', 5)}%" if s.get('min_saving_pct', 5) else '0% (any reduction)')
            self.bg_priority.set(s.get('bg_priority', True))
            self.throttle_ms.set(s.get('throttle_ms', 0))
            self._lbl_throttle.config(
                text=f"{s.get('throttle_ms', 0)} ms" if s.get('throttle_ms', 0) else 'off')
            if s.get('last_folder') and os.path.isdir(s['last_folder']):
                self.root_var.set(s['last_folder'])
                self.tree.load_root(s['last_folder'])
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass  # first run or corrupt file — use defaults

    def _save_settings(self):
        try:
            s = {
                'img_enabled':    self.img_enabled.get(),
                'img_quality':    self.img_quality.get(),
                'pdf_enabled':    self.pdf_enabled.get(),
                'pdf_quality':    self.pdf_quality.get(),
                'vid_enabled':    self.vid_enabled.get(),
                'vid_codec':      self.vid_codec.get(),
                'vid_crf':        self.vid_crf.get(),
                'overwrite_mode': self.overwrite_mode.get(),
                'use_temp_dir':   self.use_temp_dir.get(),
                'temp_dir':       self.temp_dir_var.get(),
                'scan_threshold': self.scan_threshold.get(),
                'bg_priority':    self.bg_priority.get(),
                'throttle_ms':    self.throttle_ms.get(),
                'min_saving_pct': self.min_saving_pct.get(),
                'last_folder':    self.root_var.get(),
            }
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as fh:
                json.dump(s, fh, indent=2)
        except Exception:
            pass

    def _resolve_tmp_path(self, src, ft):
        """
        Return a temp file path with enough free space to hold the original.
        Falls back to next-to-original if the temp drive is too full.
        Returns None if neither location has enough space.
        """
        file_size = os.path.getsize(src)
        extra_ext  = Path(src).suffix.lower() if ft == 'video' else ''

        if self.use_temp_dir.get():
            tmp_dir = self.temp_dir_var.get()
            try:
                os.makedirs(tmp_dir, exist_ok=True)
                free = shutil.disk_usage(tmp_dir).free
                if free > file_size:
                    return self._get_tmp_path(src, extra_ext)
                # Temp drive too full — log and fall back
                self._log(
                    f'WARN temp drive low ({human_size(free)} free, need {human_size(file_size)}) '
                    f'— falling back to source location for {Path(src).name}')
            except OSError:
                self._log(f'WARN cannot access temp dir — falling back for {Path(src).name}')

        # Fall back: write next to original
        fallback = src + '.sqtmp' + extra_ext
        try:
            free = shutil.disk_usage(os.path.dirname(src) or '.').free
            if free > file_size:
                return fallback
            self._log(
                f'WARN source drive also low ({human_size(free)} free) '
                f'— skipping {Path(src).name}')
            return None
        except OSError:
            return None

    def _cleanup_temp_dir(self):
        """Remove leftover sqtmp_* files from a previous crashed run."""
        try:
            tmp_dir = self.temp_dir_var.get()
            if os.path.isdir(tmp_dir):
                for f in os.listdir(tmp_dir):
                    if f.startswith('sqtmp_'):
                        try:
                            os.remove(os.path.join(tmp_dir, f))
                        except OSError:
                            pass
        except Exception:
            pass

    def _on_close(self):
        self._save_settings()
        self.destroy()

    def _toggle_temp_dir(self):
        state = 'normal' if self.use_temp_dir.get() else 'disabled'
        self._tmp_entry.config(state=state)
        self._tmp_browse_btn.config(state=state)

    def _browse_temp_dir(self):
        path = filedialog.askdirectory(title='Select local temp folder')
        if path:
            self.temp_dir_var.set(path)

    def _get_tmp_path(self, src, extra_ext=''):
        """Return a temp file path — local folder or next to source."""
        import uuid
        if self.use_temp_dir.get():
            tmp_dir = self.temp_dir_var.get()
            os.makedirs(tmp_dir, exist_ok=True)
            name = f'sqtmp_{uuid.uuid4().hex}{Path(src).suffix}{extra_ext}'
            return os.path.join(tmp_dir, name)
        return src + '.sqtmp' + extra_ext

    def _scan_badges(self):
        root = self.root_var.get()
        if not root:
            messagebox.showwarning('No folder', 'Browse to a root folder first.')
            return
        self.btn_scan.config(state='disabled')
        self._scan_status.set('Scanning…')
        types = (self.img_enabled.get(), self.vid_enabled.get(), self.pdf_enabled.get())
        t = threading.Thread(target=self._scan_badges_worker,
                             args=(root, types), daemon=True)
        t.start()

    def _scan_badges_worker(self, root, file_types_enabled):
        # Walk all subdirs under root, compute stats, post back to UI
        all_dirs = []
        try:
            for dirpath, dirnames, _ in os.walk(root):
                all_dirs.append(dirpath)
                dirnames.sort()
        except PermissionError:
            pass

        total = len(all_dirs)
        for idx, dirpath in enumerate(all_dirs):
            if self._cancel_flag:
                break
            done, new_c, new_b = self._db.folder_stats(dirpath, file_types_enabled)
            # Post badge update to main thread
            self.after(0, self.tree.set_badge, dirpath, done, new_c, new_b)
            self.after(0, self._scan_status.set,
                       f'Scanning {idx+1}/{total}: {Path(dirpath).name}')

        threshold = self.scan_threshold.get() * 1024 * 1024
        self.after(0, self.tree.auto_uncheck_done, threshold)
        self.after(0, self._scan_status.set,
                   f'Scan complete — {total} folders checked.')
        self.after(0, self.btn_scan.config, {'state': 'normal'})

    def _browse(self):
        path = filedialog.askdirectory(title='Select root folder to compress')
        if path:
            self.root_var.set(path)
            self.tree.load_root(path)
            self._save_settings()

    def _select_all(self):
        for iid in self.tree.tv.get_children():
            self.tree._set_subtree(iid, True)

    def _clear_all(self):
        for iid in self.tree.tv.get_children():
            self.tree._set_subtree(iid, False)

    def _log(self, msg):
        self._log_text.config(state='normal')
        self._log_text.insert('end', msg + '\n')
        self._log_text.see('end')
        self._log_text.config(state='disabled')

    def _load_resume(self):
        if os.path.exists(RESUME_FILE):
            try:
                with open(RESUME_FILE) as fh:
                    self._resume_data = json.load(fh)
                if self._resume_data.get('files'):
                    self.btn_resume.config(state='normal')
                    self._log(f'Resume file found: {len(self._resume_data["files"])} files previously processed.')
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Confirm dialog (called from main thread while worker waits)
    # ------------------------------------------------------------------

    def _ask_overwrite(self, path):
        if self._yes_to_all:
            return 'yes'
        if self.overwrite_mode.get() == 'yes_all':
            return 'yes'
        if self.overwrite_mode.get() == 'dry':
            return 'no'

        result = {'choice': None}

        def _make_dialog():
            dlg = tk.Toplevel(self)
            dlg.title('Confirm overwrite')
            dlg.grab_set()
            dlg.resizable(False, False)
            ttk.Label(dlg, text='About to overwrite:', font=('TkDefaultFont', 10, 'bold')
                      ).pack(padx=20, pady=(16, 4))
            ttk.Label(dlg, text=path, wraplength=480, foreground='blue'
                      ).pack(padx=20, pady=(0, 12))
            btn_f = ttk.Frame(dlg)
            btn_f.pack(pady=(0, 16))

            def _choose(val):
                result['choice'] = val
                if val == 'yes_all':
                    self._yes_to_all = True
                dlg.destroy()
                self._confirm_evt.set()

            ttk.Button(btn_f, text='Yes',         command=lambda: _choose('yes'),     width=10).pack(side='left', padx=4)
            ttk.Button(btn_f, text='Yes to all',  command=lambda: _choose('yes_all'), width=12).pack(side='left', padx=4)
            ttk.Button(btn_f, text='Skip',        command=lambda: _choose('skip'),    width=10).pack(side='left', padx=4)
            ttk.Button(btn_f, text='Cancel run',  command=lambda: _choose('cancel'),  width=12).pack(side='left', padx=4)

        self._confirm_evt.clear()
        self.after(0, _make_dialog)
        self._confirm_evt.wait()
        return result['choice']

    # ------------------------------------------------------------------
    # Compression run
    # ------------------------------------------------------------------

    def _start_compression(self):
        paths = self.tree.get_selected_paths()
        if not paths:
            messagebox.showwarning('No selection', 'Please select at least one folder.')
            return
        self._yes_to_all   = False
        self._cancel_flag  = False
        self._pause_flag   = False
        self._pause_evt.set()
        self._results      = []
        self.report_tv.delete(*self.report_tv.get_children())
        self.btn_go.config(state='disabled')
        self.btn_pause.config(state='normal', text='⏸  Pause')
        self.btn_cancel.config(state='normal')
        self.nb.select(2)
        t = threading.Thread(target=self._worker, args=(paths, False), daemon=True)
        t.start()

    def _resume_run(self):
        if not self._resume_data:
            return
        done_set = set(self._resume_data.get('done', []))
        paths    = self._resume_data.get('paths', [])
        self._yes_to_all  = False
        self._cancel_flag = False
        self._pause_flag  = False
        self._pause_evt.set()
        self._results     = list(self._resume_data.get('results', []))
        # Restore settings from the original run so compression is consistent
        s = self._resume_data.get('settings', {})
        if s:
            if 'img_quality'    in s: self.img_quality.set(s['img_quality'])
            if 'pdf_quality'    in s: self.pdf_quality.set(s['pdf_quality'])
            if 'vid_codec'      in s: self.vid_codec.set(s['vid_codec'])
            if 'vid_crf'        in s: self.vid_crf.set(s['vid_crf'])
            if 'overwrite_mode' in s: self.overwrite_mode.set(s['overwrite_mode'])
            if 'min_saving_pct' in s: self.min_saving_pct.set(s['min_saving_pct'])
        self.btn_go.config(state='disabled')
        self.btn_pause.config(state='normal', text='⏸  Pause')
        self.btn_cancel.config(state='normal')
        self.nb.select(2)
        t = threading.Thread(target=self._worker, args=(paths, True, done_set), daemon=True)
        t.start()

    def _toggle_pause(self):
        if self._pause_flag:
            self._pause_flag = False
            self._pause_evt.set()
            self.btn_pause.config(text='⏸  Pause')
            self._status_var.set('Resumed.')
            self._log('Resumed.')
        else:
            self._pause_flag = True
            self._pause_evt.clear()
            self.btn_pause.config(text='▶  Resume')
            self._status_var.set('Paused — will stop after current file. Close and reopen to apply updates.')
            self._log('Paused. Safe to close and rebuild.')

    def _cancel(self):
        self._cancel_flag = True
        # Unpause so the worker can see the cancel flag
        self._pause_evt.set()
        self._log('Cancelling after current file…')

    def _worker(self, paths, is_resume=False, done_set=None):
        done_set = done_set or set()

        # Collect all target files
        self._status_var.set('Scanning folders…')
        all_files = []
        img_on  = self.img_enabled.get()
        vid_on  = self.vid_enabled.get() and bool(FFMPEG)
        pdf_on  = self.pdf_enabled.get()

        for folder in paths:
            for dirpath, _, filenames in os.walk(folder):
                for fn in filenames:
                    fp = os.path.join(dirpath, fn)
                    if not os.path.exists(fp):
                        continue
                    ft = get_file_type(fp)
                    if ft == 'image' and img_on:
                        all_files.append((fp, ft))
                    elif ft == 'video' and vid_on:
                        all_files.append((fp, ft))
                    elif ft == 'pdf' and pdf_on:
                        all_files.append((fp, ft))

        total      = len(all_files)
        processed  = 0
        skipped    = 0
        errors     = 0
        saved      = 0
        _run_start = time.time()

        self.prog_overall.config(maximum=max(total, 1))

        # Save resume state including settings snapshot
        resume_state = {
            'paths':   paths,
            'done':    list(done_set),
            'files':   [f for f, _ in all_files],
            'results': self._results,
            'settings': {
                'img_quality':    self.img_quality.get(),
                'pdf_quality':    self.pdf_quality.get(),
                'vid_codec':      self.vid_codec.get(),
                'vid_crf':        self.vid_crf.get(),
                'overwrite_mode': self.overwrite_mode.get(),
                'min_saving_pct': self.min_saving_pct.get(),
            },
        }
        self._save_resume(resume_state)

        for idx, (fp, ft) in enumerate(all_files):
            # Wait here if paused (blocks until unpaused or cancelled)
            self._pause_evt.wait()

            if self._cancel_flag:
                self._log('Cancelled by user.')
                break

            if fp in done_set:
                processed += 1
                self.prog_overall['value'] = processed
                continue

            # Skip files already compressed and unchanged since last run
            if self._db.is_done(fp):
                sz = os.path.getsize(fp)
                row = {'file': fp, 'type': ft, 'original': sz,
                       'compressed': sz, 'saving': 0, 'status': 'db-skip'}
                self._add_result_row(row)
                done_set.add(fp)
                processed += 1
                self.prog_overall['value'] = idx + 1
                self._prog_lbl.set(f'{idx+1} / {total} files')
                continue

            if not os.path.exists(fp):
                self._log(f'GONE {Path(fp).name} — file removed during run, skipping')
                done_set.add(fp)
                continue

            try:
                orig_size = os.path.getsize(fp)
            except OSError:
                self._log(f'GONE {Path(fp).name} — cannot read size, skipping')
                done_set.add(fp)
                continue

            self._cur_file_var.set(fp)
            self._status_var.set(f'[{idx+1}/{total}] {ft.upper()}: {Path(fp).name}')

            # --- Dry run ---
            if self.overwrite_mode.get() == 'dry':
                row = {'file': fp, 'type': ft, 'original': orig_size,
                       'compressed': orig_size, 'saving': 0, 'status': 'dry-run'}
                self._add_result_row(row)
                processed += 1
            else:
                # Compress to temp
                tmp = None
                err = None
                lp       = self.bg_priority.get()
                tmp_path = self._resolve_tmp_path(fp, ft)
                if tmp_path is None:
                    # Both drives too full — skip this file
                    self._log(f'SKIP (no space) {Path(fp).name}: insufficient space on temp and source drives')
                    row = {'file': fp, 'type': ft, 'original': orig_size,
                           'compressed': 0, 'saving': 0, 'status': 'skipped-no-space'}
                    self._add_result_row(row)
                    skipped += 1
                    done_set.add(fp)
                    continue
                if ft == 'image':
                    tmp, err = compress_image(fp, self.img_quality.get(), tmp_path)
                elif ft == 'pdf':
                    tmp, err = compress_pdf(fp, self.pdf_quality.get(), lp, tmp_path)
                elif ft == 'video':
                    tmp, err = compress_video(fp, self.vid_codec.get(), self.vid_crf.get(), lp, tmp_path)

                # For video, err is a codec-fallback warning, not a failure
                is_error = (not tmp or not os.path.exists(tmp)) or (err and ft != 'video')
                if is_error:
                    # Clean up orphaned temp if it exists
                    if tmp and os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                    status = 'gone' if not os.path.exists(fp) else f'error: {err}'
                    self._log(f'ERROR {Path(fp).name}: {status}')
                    row = {'file': fp, 'type': ft, 'original': orig_size,
                           'compressed': 0, 'saving': 0, 'status': status}
                    self._add_result_row(row)
                    errors += 1
                else:
                    new_size = os.path.getsize(tmp)
                    saving_pct = (orig_size - new_size) / orig_size * 100 if orig_size else 0
                    min_pct    = self.min_saving_pct.get()
                    if saving_pct < min_pct:
                        os.remove(tmp)
                        reason = ('grew' if new_size > orig_size
                                  else f'only {saving_pct:.1f}% saving < {min_pct}% threshold')
                        self._log(f'SKIP ({reason}) {Path(fp).name}: '
                                  f'{human_size(orig_size)} → {human_size(new_size)}')
                        # Record so future runs skip re-attempting this file
                        self._db.record(fp, orig_size, orig_size, 'skipped-no-gain')
                        self._db.save()
                        row = {'file': fp, 'type': ft, 'original': orig_size,
                               'compressed': new_size, 'saving': 0, 'status': 'skipped-no-gain'}
                        self._add_result_row(row)
                        skipped += 1
                    else:
                        # Ask overwrite
                        choice = self._ask_overwrite(fp)
                        if choice == 'cancel':
                            if os.path.exists(tmp):
                                os.remove(tmp)
                            self._cancel_flag = True
                            break
                        elif choice == 'skip':
                            if os.path.exists(tmp):
                                os.remove(tmp)
                            row = {'file': fp, 'type': ft, 'original': orig_size,
                                   'compressed': new_size, 'saving': 0, 'status': 'skipped-by-user'}
                            self._add_result_row(row)
                            skipped += 1
                        else:  # yes or yes_all
                            try:
                                safe_replace(tmp, fp)
                                gain = orig_size - new_size
                                saved += gain
                                self._db.record(fp, orig_size, new_size, 'compressed')
                                self._db.save()
                                if err:  # codec fallback warning from compress_video
                                    self._log(f'WARN {Path(fp).name}: {err}')
                                self._log(f'OK {Path(fp).name}: {human_size(orig_size)} → {human_size(new_size)} (saved {human_size(gain)})')
                                row = {'file': fp, 'type': ft, 'original': orig_size,
                                       'compressed': new_size,
                                       'saving': gain, 'status': 'compressed'}
                                self._add_result_row(row)
                            except Exception as ex:
                                self._log(f'ERROR replacing {fp}: {ex}')
                                errors += 1
                                row = {'file': fp, 'type': ft, 'original': orig_size,
                                       'compressed': 0, 'saving': 0, 'status': f'replace-error'}
                                self._add_result_row(row)
                        processed += 1

            # Throttle delay
            delay = self.throttle_ms.get()
            if delay > 0:
                time.sleep(delay / 1000.0)

            done_set.add(fp)
            resume_state['done'] = list(done_set)
            resume_state['results'] = self._results
            self._save_resume(resume_state)

            self.prog_overall['value'] = idx + 1
            elapsed = time.time() - _run_start
            done_count = idx + 1
            if done_count > 0 and elapsed > 1:
                rate = done_count / elapsed
                remaining = (total - done_count) / rate
                h, m = divmod(int(remaining), 3600)
                m, s = divmod(m, 60)
                eta = f'{h}h {m:02d}m {s:02d}s' if h else f'{m}m {s:02d}s'
                self._prog_lbl.set(f'{done_count} / {total} files  —  ETA {eta}')
            else:
                self._prog_lbl.set(f'{done_count} / {total} files')
            self._stat_vars['processed'].set(str(processed))
            self._stat_vars['skipped_gain'].set(str(skipped))
            self._stat_vars['errors'].set(str(errors))
            self._stat_vars['saved'].set(human_size(saved))

        # Finished
        self._status_var.set(
            f'Done. {processed} files processed, {human_size(saved)} reclaimed, '
            f'{skipped} no-gain skips, {errors} errors.')
        self.btn_go.config(state='normal')
        self.btn_pause.config(state='disabled', text='⏸  Pause')
        self.btn_cancel.config(state='disabled')
        self._cur_file_var.set('—')

        if not self._cancel_flag:
            # Clean up resume file on successful completion
            if os.path.exists(RESUME_FILE):
                os.remove(RESUME_FILE)
            self.btn_resume.config(state='disabled')

        self._populate_report()
        self.nb.select(3)

    def _add_result_row(self, row):
        self._results.append(row)
        self.after(0, self._live_report_insert, row)

    def _live_report_insert(self, row):
        orig = row['original']
        comp = row['compressed'] or orig
        sav  = row['saving']
        tag = ('compressed' if row['status'] == 'compressed' else
               'dbskip'     if row['status'] == 'db-skip' else
               'error'      if 'error' in row['status'] else 'skip')
        self.report_tv.insert('', 'end', values=(
            row['file'], row['type'],
            human_size(orig), human_size(comp),
            human_size(sav) if sav else '—',
            row['status']
        ), tags=(tag,))
        self.report_tv.tag_configure('compressed', foreground='darkgreen')
        self.report_tv.tag_configure('error',      foreground='red')
        self.report_tv.tag_configure('skip',       foreground='gray')
        self.report_tv.tag_configure('dbskip',     foreground='steelblue')

    def _populate_report(self):
        # Treeview already populated live via _live_report_insert — just compute totals
        total_orig = total_new = total_saved = 0
        for r in self._results:
            orig = r['original']
            comp = r['compressed'] or orig
            sav  = r['saving']
            total_orig  += orig
            total_new   += comp
            total_saved += sav

        # Fill in-app log
        self._report_log.config(state='normal')
        self._report_log.delete('1.0', 'end')
        self._report_log.insert('end', f'SquishIT Report — {datetime.now()}\n')
        self._report_log.insert('end', '=' * 100 + '\n')
        self._report_log.insert('end', f'{"STATUS":<22} {"TYPE":<6} {"ORIGINAL":<12} {"COMPRESSED":<12} {"SAVED":<12} FILE\n')
        self._report_log.insert('end', '-' * 100 + '\n')
        for r in self._results:
            orig = r['original']
            comp = r['compressed'] or orig
            sav  = r['saving']
            self._report_log.insert('end',
                f'{r["status"]:<22} {r["type"]:<6} {human_size(orig):<12} '
                f'{human_size(comp):<12} {human_size(sav) if sav else "—":<12} {r["file"]}\n')
        self._report_log.insert('end', '=' * 100 + '\n')
        self._report_log.config(state='disabled')

        pct = (total_saved / total_orig * 100) if total_orig else 0
        self._summary_var.set(
            f'Total: {len(self._results)} files  |  '
            f'Before: {human_size(total_orig)}  |  '
            f'After: {human_size(total_new)}  |  '
            f'Saved: {human_size(total_saved)} ({pct:.1f}%)'
        )

    # ------------------------------------------------------------------
    # Resume state
    # ------------------------------------------------------------------

    def _save_resume(self, state):
        try:
            with open(RESUME_FILE, 'w') as fh:
                json.dump(state, fh, indent=2)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Log export
    # ------------------------------------------------------------------

    def _save_log(self, fmt):
        ts  = datetime.now().strftime('%Y%m%d_%H%M%S')
        default = os.path.join(
            os.path.dirname(os.path.abspath(
                sys.executable if getattr(sys, 'frozen', False) else __file__
            )), 'logs', f'squishit_{ts}.{fmt}')
        path = filedialog.asksaveasfilename(
            defaultextension=f'.{fmt}',
            filetypes=[(f'{fmt.upper()} files', f'*.{fmt}')],
            initialfile=os.path.basename(default))
        if not path:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if fmt == 'csv':
            with open(path, 'w', newline='', encoding='utf-8') as fh:
                w = csv.DictWriter(fh, fieldnames=['file','type','original','compressed','saving','status'])
                w.writeheader()
                for r in self._results:
                    w.writerow(r)
        else:
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(f'SquishIT Report — {datetime.now()}\n')
                fh.write('=' * 80 + '\n')
                for r in self._results:
                    fh.write(
                        f"{r['status'].upper():20s}  {r['type']:5s}  "
                        f"{human_size(r['original']):10s} -> "
                        f"{human_size(r['compressed'] or r['original']):10s}  "
                        f"saved {human_size(r['saving']):10s}  {r['file']}\n"
                    )
        messagebox.showinfo('Saved', f'Log saved to:\n{path}')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app = SquishIt()
    app.mainloop()
