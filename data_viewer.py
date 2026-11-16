r"""
HA-130TR 로그 뷰어 / 구간 분석

- 왼쪽: data 폴더의 CSV 목록 (클릭하면 열림, 최신 파일이 위)
- 가운데: Torque / RPM 그래프 (툴바로 확대/이동, 홈 버튼으로 전체 보기)
- 그래프에서 마우스로 좌우 드래그 -> 구간 선택 -> 오른쪽에 분석 결과
- 선택 구간을 CSV로 저장 / 분석 결과를 클립보드로 복사 (Excel에 붙여넣기 가능)

실행:
    .venv\Scripts\python.exe data_viewer.py              # 기본: ./data 폴더
    .venv\Scripts\python.exe data_viewer.py D:\logs      # 다른 폴더
"""
import csv
import math
import queue
import sys
import threading
import warnings
from array import array
from datetime import datetime
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox

import numpy as np
import matplotlib

matplotlib.use("TkAgg")
from matplotlib.figure import Figure                                  # noqa: E402
from matplotlib.backends.backend_tkagg import (FigureCanvasTkAgg,     # noqa: E402
                                               NavigationToolbar2Tk)
from matplotlib.widgets import SpanSelector                           # noqa: E402

GAP_MS = 2000               # 이 간격보다 크면 수신 끊김으로 보고 선을 끊음 (로거와 동일 기준)
DISPLAY_BUCKETS = 3000      # 화면에 그릴 최대 구간 수 (그 이상은 최소/최대 포락선으로 축약)

# 팔레트 (로거와 동일)
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#6250d6"]
SELECT_COLOR = "#2a78d6"


# ----------------------------------------------------------------------------------------
# 데이터 로드 / 분석 (GUI와 무관한 순수 로직)
# ----------------------------------------------------------------------------------------
def series_label(col):
    """CSV 열 이름 -> 그래프/표 표시 이름"""
    legacy = {"S1_Value": "Torque (S1)", "S2_Value": "RPM (S2)"}   # 초기 버전 로거 형식
    if col in legacy:
        return legacy[col]
    if col.startswith("Torque_"):
        return f"Torque [{col.split('_', 1)[1]}]"
    return col


class LogData:
    """로거 CSV 한 개. t = 시작부터 경과 초(float), series = {표시이름: 값 배열}"""

    def __init__(self, path):
        self.path = Path(path)
        ts = []
        with open(self.path, newline="", encoding="utf-8-sig") as f:
            r = csv.reader(f)
            self.header = next(r)
            ti = self.header.index("Timestamp")
            ii = self.header.index("Interval_ms") if "Interval_ms" in self.header else None
            self.cols = [(i, h) for i, h in enumerate(self.header)
                         if i not in (ti, ii) and not h.endswith("_Status")]
            vals = [array("d") for _ in self.cols]
            ivs = array("d")
            ncol = len(self.header)
            self.skipped = 0
            for row in r:
                if len(row) != ncol:            # 기록 중 잘린 마지막 줄 등
                    self.skipped += 1
                    continue
                try:
                    v = [float(row[i]) if row[i] else math.nan for i, _ in self.cols]
                    iv = float(row[ii]) if ii is not None and row[ii] else math.nan
                except ValueError:
                    self.skipped += 1
                    continue
                ts.append(row[ti])
                for k, x in enumerate(v):
                    vals[k].append(x)
                ivs.append(iv)

        if not ts:
            raise ValueError("데이터 행이 없습니다.")
        self.t_abs = np.array(ts, dtype="datetime64[ms]")
        self.t = (self.t_abs - self.t_abs[0]).astype("int64") / 1000.0
        self.interval = np.frombuffer(ivs, dtype=float)
        self.series = {series_label(h): np.frombuffer(vals[k], dtype=float)
                       for k, (_, h) in enumerate(self.cols)}

        # 그래프용: 끊김 위치에 NaN을 넣어 선을 끊음
        dt = np.diff(self.t) * 1000
        gi = np.where(dt > GAP_MS)[0] + 1
        self.gap_count = len(gi)
        self.tp = np.insert(self.t, gi, self.t[gi - 1])
        self.yp = {k: np.insert(v, gi, np.nan) for k, v in self.series.items()}

    @property
    def n(self):
        return len(self.t)

    def time_str(self, i):
        return np.datetime_as_string(self.t_abs[i], unit="ms").replace("T", " ")

    def index_range(self, x0, x1):
        lo = int(np.searchsorted(self.t, x0, side="left"))
        hi = int(np.searchsorted(self.t, x1, side="right"))
        return lo, hi

    def export(self, path, lo, hi):
        """원본 형식(헤더 그대로, Status 열 제외)으로 [lo, hi) 구간 저장"""
        ts = np.datetime_as_string(self.t_abs[lo:hi], unit="ms")
        names = list(self.series)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Timestamp", "Interval_ms"] + [h for _, h in self.cols])
            for k in range(hi - lo):
                iv = self.interval[lo + k]
                row = [ts[k].replace("T", " "), "" if math.isnan(iv) else f"{iv:.0f}"]
                for name in names:
                    v = self.series[name][lo + k]
                    row.append("" if math.isnan(v) else f"{v:.1f}")
                w.writerow(row)


def analyze(d, lo, hi):
    """[lo, hi) 구간 통계 -> (일반 항목 리스트, {시리즈: [(항목, 값)]})"""
    n = hi - lo
    t = d.t[lo:hi]
    dt = np.diff(t) * 1000
    normal = dt[dt <= GAP_MS]
    period = normal.mean() if normal.size else math.nan
    general = [
        ("시작 시각", d.time_str(lo)),
        ("종료 시각", d.time_str(hi - 1)),
        ("구간 길이", f"{t[-1] - t[0]:,.3f} s"),
        ("구간 위치", f"{t[0]:,.3f} ~ {t[-1]:,.3f} s"),
        ("샘플 수", f"{n:,}"),
        ("평균 주기", f"{period:.1f} ms" if normal.size else "-"),
        ("샘플링", f"{1000 / period:.2f} Hz" if normal.size else "-"),
        ("수신 끊김", f"{int((dt > GAP_MS).sum())} 회"),
    ]
    stats = {}
    for name, y in d.series.items():
        ys = y[lo:hi]
        m = np.isfinite(ys)
        v, tv = ys[m], t[m]
        if not v.size:
            stats[name] = []
            continue
        i_min, i_max = int(np.argmin(v)), int(np.argmax(v))
        stats[name] = [
            ("평균", v.mean()),
            ("표준편차", v.std()),
            ("최소", v[i_min]),
            ("최소 위치 [s]", tv[i_min]),
            ("최대", v[i_max]),
            ("최대 위치 [s]", tv[i_max]),
            ("피크-피크", v[i_max] - v[i_min]),
            ("RMS", float(np.sqrt(np.mean(v * v)))),
            ("시작값", v[0]),
            ("끝값", v[-1]),
            ("변화량 (끝-시작)", v[-1] - v[0]),
            ("유효 샘플", v.size),
        ]
    return general, stats


def decimate(t, y, x0, x1, buckets=DISPLAY_BUCKETS):
    """보이는 구간만 잘라서, 점이 많으면 구간별 최소/최대 포락선으로 축약 (피크가 사라지지 않음)"""
    lo = max(int(np.searchsorted(t, x0, side="left")) - 1, 0)
    hi = min(int(np.searchsorted(t, x1, side="right")) + 1, len(t))
    ts, ys = t[lo:hi], y[lo:hi]
    n = len(ts)
    if n <= buckets * 2:
        return ts, ys
    k = n // buckets
    m = k * buckets
    yb = ys[:m].reshape(buckets, k)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # 끊김(NaN)만 있는 구간
        ymin, ymax = np.nanmin(yb, axis=1), np.nanmax(yb, axis=1)
    tb = ts[:m:k]
    xs = np.concatenate([np.repeat(tb, 2), ts[m:]])
    yv = np.concatenate([np.column_stack([ymin, ymax]).ravel(), ys[m:]])
    return xs, yv


def fit_ylim(y, min_pad=0.1, frac=0.08):
    """값 배열에 맞춘 (ymin, ymax) + 여백. 유효값이 없으면 None"""
    fin = y[np.isfinite(y)]
    if not fin.size:
        return None
    lo, hi = float(fin.min()), float(fin.max())
    pad = max((hi - lo) * frac, min_pad)
    return lo - pad, hi + pad


def fmt_num(v):
    if isinstance(v, (int, np.integer)):
        return f"{v:,}"
    if math.isnan(v):
        return "-"
    return f"{v:,.3f}"


# ----------------------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------------------
class Viewer:
    def __init__(self, root, folder):
        self.root = root
        self.folder = Path(folder)
        self.d = None
        self.sel = None                 # (lo, hi) 선택 구간 인덱스
        self.sel_patches = []
        self.spans = []
        self.lines = {}
        self.loading = False
        self.q = queue.Queue()

        root.title("HA-130TR 데이터 뷰어")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{int(sw * 0.88)}x{int(sh * 0.85)}+{int(sw * 0.06)}+{int(sh * 0.04)}")
        style = ttk.Style()
        # 화면 배율(DPI)에 맞춰 표 줄 높이 결정 (작으면 '_' 등이 잘림)
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        style.configure("Treeview", rowheight=line + 6)
        style.configure("Head.TLabel", font=("Malgun Gothic", 11, "bold"))
        self.em = tkfont.nametofont("TkDefaultFont").measure("가")   # 글자 폭 기준 열 너비

        self._build_ui()
        self.refresh_files()

    # ---------------- UI 구성 ----------------
    def _build_ui(self):
        root = self.root
        paned = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        # 왼쪽: 파일 목록
        left = ttk.Frame(paned, padding=6)
        ttk.Label(left, text="로그 파일", style="Head.TLabel").pack(anchor="w")
        self.folder_lbl = ttk.Label(left, text="", foreground=INK2, wraplength=self.em * 18)
        self.folder_lbl.pack(anchor="w", pady=(0, 4))
        btns = ttk.Frame(left)
        btns.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(btns, text="새로고침", command=self.refresh_files).pack(side=tk.LEFT)
        ttk.Button(btns, text="폴더 변경...", command=self.change_folder).pack(side=tk.LEFT, padx=4)

        lf = ttk.Frame(left)
        lf.pack(fill=tk.BOTH, expand=True)
        self.files = ttk.Treeview(lf, columns=("size", "mtime"), selectmode="browse")
        self.files.heading("#0", text="파일")
        self.files.heading("size", text="크기")
        self.files.heading("mtime", text="수정 시각")
        em = self.em
        self.files.column("#0", width=em * 16, stretch=True)
        self.files.column("size", width=em * 4, anchor="e", stretch=False)
        self.files.column("mtime", width=em * 6, anchor="center", stretch=False)
        sb = ttk.Scrollbar(lf, orient=tk.VERTICAL, command=self.files.yview)
        self.files.configure(yscrollcommand=sb.set)
        self.files.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.files.bind("<<TreeviewSelect>>", self.on_file_select)
        paned.add(left, weight=0)

        # 가운데: 그래프
        center = ttk.Frame(paned)
        self.fig = Figure(figsize=(9, 6), facecolor=SURFACE, layout="constrained")
        self.canvas = FigureCanvasTkAgg(self.fig, master=center)
        tb_frame = ttk.Frame(center)
        tb_frame.pack(side=tk.TOP, fill=tk.X)
        self.toolbar = NavigationToolbar2Tk(self.canvas, tb_frame, pack_toolbar=False)
        self.toolbar.pack(side=tk.LEFT)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self._placeholder("왼쪽 목록에서 파일을 선택하세요")
        paned.add(center, weight=1)

        # 오른쪽: 분석
        right = ttk.Frame(paned, padding=6)
        ttk.Label(right, text="구간 분석", style="Head.TLabel").pack(anchor="w")
        em = self.em
        self.range_lbl = ttk.Label(right, text="파일을 열면 전체 구간이 분석됩니다.",
                                   foreground=INK2, wraplength=em * 22)
        self.range_lbl.pack(anchor="w", pady=(0, 6))

        self.gen = ttk.Treeview(right, columns=("k", "v"), show="headings", height=8)
        self.gen.heading("k", text="항목")
        self.gen.heading("v", text="값")
        self.gen.column("k", width=em * 6, stretch=False)
        self.gen.column("v", width=em * 15)
        self.gen.pack(fill=tk.X)

        self.stats = ttk.Treeview(right, columns=("k",), show="headings", height=12)
        self.stats.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        b = ttk.Frame(right)
        b.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(b, text="선택 구간 확대 (X·Y 맞춤)", command=self.zoom_selection).pack(fill=tk.X)
        ttk.Button(b, text="전체 보기 / 선택 해제", command=self.reset_view).pack(fill=tk.X, pady=(3, 0))
        ttk.Button(b, text="분석 결과 복사 (Excel 붙여넣기)", command=self.copy_result).pack(fill=tk.X, pady=3)
        ttk.Button(b, text="선택 구간 CSV로 저장...", command=self.export_sel).pack(fill=tk.X)
        ttk.Label(right, foreground=INK2, wraplength=em * 22, justify=tk.LEFT,
                  text="• 그래프를 좌우로 드래그 → 구간 선택\n"
                       "• 선택 구간 확대 후 툴바 ← 버튼으로 이전 보기 복귀\n"
                       "• 툴바: 돋보기 = 확대, 십자 = 이동, 집 = 처음 보기\n"
                       "• 확대/이동이 켜져 있으면 구간 선택 불가 (버튼 다시 눌러 해제)"
                  ).pack(anchor="w", pady=(8, 0))
        paned.add(right, weight=0)

        self.status = ttk.Label(root, text="", anchor="w", foreground=INK2, padding=(6, 2))
        self.status.pack(side=tk.BOTTOM, fill=tk.X)

    def _placeholder(self, msg):
        self.fig.clear()
        self.fig.text(0.5, 0.5, msg, ha="center", va="center", color=INK2, fontsize=12,
                      fontfamily=["Malgun Gothic", "DejaVu Sans"])
        self.canvas.draw_idle()

    # ---------------- 파일 목록 ----------------
    def refresh_files(self):
        self.folder_lbl.config(text=str(self.folder))
        self.files.delete(*self.files.get_children())
        if not self.folder.is_dir():
            self.status.config(text=f"폴더가 없습니다: {self.folder}")
            return
        items = sorted(self.folder.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in items:
            st = p.stat()
            size = st.st_size
            size_s = f"{size / 1024:,.0f} KB" if size < 1024 ** 2 else f"{size / 1024 ** 2:,.1f} MB"
            mt = datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M")
            self.files.insert("", tk.END, iid=str(p), text=p.name, values=(size_s, mt))
        self.status.config(text=f"{len(items)}개 파일")

    def change_folder(self):
        d = filedialog.askdirectory(initialdir=self.folder, title="CSV 폴더 선택")
        if d:
            self.folder = Path(d)
            self.refresh_files()

    def on_file_select(self, _):
        sel = self.files.selection()
        if sel and not self.loading:
            self.load(Path(sel[0]))

    # ---------------- 로드 (백그라운드) ----------------
    def load(self, path):
        self.loading = True
        self.status.config(text=f"불러오는 중... {path.name}")
        self.root.config(cursor="watch")

        def work():
            try:
                self.q.put(LogData(path))
            except Exception as e:      # noqa: BLE001 - 사용자에게 그대로 표시
                self.q.put(e)

        threading.Thread(target=work, daemon=True).start()
        self.root.after(50, self._poll_load)

    def _poll_load(self):
        try:
            res = self.q.get_nowait()
        except queue.Empty:
            self.root.after(50, self._poll_load)
            return
        self.loading = False
        self.root.config(cursor="")
        if isinstance(res, Exception):
            self.status.config(text="불러오기 실패")
            messagebox.showerror("불러오기 실패", f"{res}")
            return
        self.show(res)

    # ---------------- 그래프 ----------------
    def show(self, d):
        self.d = d
        self.sel = None
        self.sel_patches = []
        self.fig.clear()
        names = list(d.series)
        axes = self.fig.subplots(len(names), 1, sharex=True, squeeze=False)[:, 0]
        self.axes = axes
        self.lines = {}
        self.full_ylim = {}                 # 전체 보기 y범위 (전체 보기 버튼으로 복원)
        self.spans = []
        for k, (ax, name) in enumerate(zip(axes, names)):
            ax.set_facecolor(SURFACE)
            ax.grid(True, color=GRID, linewidth=0.6)
            ax.set_axisbelow(True)
            for s in ("top", "right"):
                ax.spines[s].set_visible(False)
            for s in ("left", "bottom"):
                ax.spines[s].set_color(GRID)
            ax.tick_params(colors=INK2, labelsize=9)
            ax.set_title(name, loc="left", color=INK, fontsize=11,
                         fontfamily=["Malgun Gothic", "DejaVu Sans"])
            (self.lines[name],) = ax.plot([], [], color=SERIES_COLORS[k % len(SERIES_COLORS)],
                                          linewidth=1.3)
            ylim = fit_ylim(d.series[name], min_pad=0.5)
            if ylim:
                ax.set_ylim(*ylim)
            self.full_ylim[name] = ax.get_ylim()
            self.spans.append(SpanSelector(
                ax, self.on_span, "horizontal", useblit=True, minspan=1e-6,
                props=dict(facecolor=SELECT_COLOR, alpha=0.12)))
        start = d.time_str(0)[:19]
        axes[-1].set_xlabel(f"경과 시간 [s]   (시작 {start})", color=INK2,
                            fontfamily=["Malgun Gothic", "DejaVu Sans"])
        t_end = d.t[-1] if d.t[-1] > 0 else 1.0
        axes[0].set_xlim(0, t_end)
        axes[0].callbacks.connect("xlim_changed", lambda ax: self._redraw_lines())
        self._redraw_lines()
        self.toolbar.update()               # 홈(처음 보기) 위치를 현재 전체 보기로
        self.canvas.draw_idle()

        self.root.title(f"HA-130TR 데이터 뷰어 - {d.path.name}")
        dur = d.t[-1]
        msg = (f"{d.path.name}   |   {d.n:,} 행   |   {dur:,.1f} s ({dur / 3600:.2f} h)"
               f"   |   수신 끊김 {d.gap_count}회")
        if d.skipped:
            msg += f"   |   읽지 못한 줄 {d.skipped}"
        self.status.config(text=msg)
        self.update_analysis(0, d.n)

    def _redraw_lines(self):
        if not self.d:
            return
        x0, x1 = self.axes[0].get_xlim()
        for name, line in self.lines.items():
            xs, ys = decimate(self.d.tp, self.d.yp[name], x0, x1)
            line.set_data(xs, ys)
        self.canvas.draw_idle()

    # ---------------- 구간 선택 / 분석 ----------------
    def on_span(self, x0, x1):
        if not self.d:
            return
        lo, hi = self.d.index_range(x0, x1)
        if hi - lo < 2:
            self.status.config(text="선택 구간에 데이터가 2개 미만입니다. 더 넓게 드래그하세요.")
            return
        for p in self.sel_patches:
            p.remove()
        self.sel_patches = [ax.axvspan(self.d.t[lo], self.d.t[hi - 1], color=SELECT_COLOR,
                                       alpha=0.12, linewidth=0) for ax in self.axes]
        self.canvas.draw_idle()
        self.update_analysis(lo, hi)

    def update_analysis(self, lo, hi):
        self.sel = (lo, hi)
        whole = (lo == 0 and hi == self.d.n)
        self.range_lbl.config(text="분석 범위: 전체 파일" if whole else
                              "분석 범위: 선택 구간 (그래프의 파란 영역)")
        general, stats = analyze(self.d, lo, hi)
        self.gen.delete(*self.gen.get_children())
        for k, v in general:
            self.gen.insert("", tk.END, values=(k, v))

        names = list(stats)
        cols = ["k"] + [f"s{i}" for i in range(len(names))]
        self.stats.configure(columns=cols)
        self.stats.heading("k", text="통계")
        self.stats.column("k", width=self.em * 8, stretch=False)
        for i, name in enumerate(names):
            self.stats.heading(f"s{i}", text=name)
            self.stats.column(f"s{i}", width=self.em * 7, anchor="e")
        self.stats.delete(*self.stats.get_children())
        keys = [k for k, _ in next((s for s in stats.values() if s), [])]
        for key in keys:
            row = [key]
            for name in names:
                vals = dict(stats[name])
                row.append(fmt_num(vals[key]) if key in vals else "-")
            self.stats.insert("", tk.END, values=row)
        self._last_result = (general, stats)

    def zoom_selection(self):
        """선택 구간에 x축을 맞추고, 각 축 y범위를 그 구간 데이터에 맞춤"""
        if not self.d or not self.sel:
            return
        lo, hi = self.sel
        x0, x1 = float(self.d.t[lo]), float(self.d.t[hi - 1])
        if x1 <= x0:
            x1 = x0 + 1e-3
        tb = self.toolbar
        if tb._nav_stack() is None:         # 툴바 ← 버튼으로 돌아올 수 있게 현재 보기 저장
            tb.push_current()
        for ax, name in zip(self.axes, self.d.series):
            ylim = fit_ylim(self.d.series[name][lo:hi])
            if ylim:
                ax.set_ylim(*ylim)
        self.axes[0].set_xlim(x0, x1)       # xlim_changed -> 해당 구간 해상도로 다시 그림
        tb.push_current()
        whole = (lo == 0 and hi == self.d.n)
        self.status.config(text="전체 데이터에 맞춰 확대했습니다." if whole else
                           f"선택 구간 확대: {x0:,.3f} ~ {x1:,.3f} s  ({hi - lo:,} 샘플)")

    def reset_view(self):
        if not self.d:
            return
        for p in self.sel_patches:
            p.remove()
        self.sel_patches = []
        for ax, name in zip(self.axes, self.d.series):
            ax.set_ylim(*self.full_ylim[name])
        self.axes[0].set_xlim(0, self.d.t[-1] if self.d.t[-1] > 0 else 1.0)
        self.update_analysis(0, self.d.n)
        self.canvas.draw_idle()

    def copy_result(self):
        if not self.d:
            return
        general, stats = self._last_result
        lines = [f"파일\t{self.d.path.name}"]
        lines += [f"{k}\t{v}" for k, v in general]
        lines.append("")
        names = list(stats)
        lines.append("\t".join(["통계"] + names))
        keys = [k for k, _ in next((s for s in stats.values() if s), [])]
        for key in keys:
            row = [key]
            for name in names:
                vals = dict(stats[name])
                v = vals.get(key, math.nan)
                row.append(str(v) if isinstance(v, (int, np.integer)) else
                           ("" if math.isnan(v) else f"{v:.6g}"))
            lines.append("\t".join(row))
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.status.config(text="분석 결과를 클립보드에 복사했습니다 (Excel에 Ctrl+V).")

    def export_sel(self):
        if not self.d or not self.sel:
            return
        lo, hi = self.sel
        default = f"{self.d.path.stem}_{self.d.t[lo]:.0f}s-{self.d.t[hi - 1]:.0f}s.csv"
        path = filedialog.asksaveasfilename(
            initialdir=self.d.path.parent, initialfile=default, defaultextension=".csv",
            filetypes=[("CSV", "*.csv")], title="선택 구간 저장")
        if not path:
            return
        self.d.export(path, lo, hi)
        self.status.config(text=f"저장: {path}  ({hi - lo:,} 행)")
        if Path(path).parent == self.folder:
            self.refresh_files()


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "data"
    try:                                    # 고해상도 모니터에서 흐릿하지 않게
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    Viewer(root, folder)
    root.mainloop()


if __name__ == "__main__":
    main()
