r"""
HA-130TR RS232 -> USB Serial -> CSV 로거 (기록 시작/종료 버튼 + 실시간 그래프)

수신 형식 (매뉴얼 10장 RS232C Stream mode, 16 bytes/줄):
    S1,NT,-00001.4<CR><LF>     S1 = CH1 = TORQUE
    S2,NT,+00000.0<CR><LF>     S2 = CH2 = RPM
    (",NT," 는 고정문자)

동작:
    프로그램을 켜면 수신과 실시간 그래프는 계속 동작하고,
    [기록 시작] ~ [기록 종료] 동안만 CSV에 저장한다. 프로그램을 끄지 않고 여러 번 기록 가능.
    파일명: HA130TR_<기록 시작 시각>[_접미사].csv  (접미사 입력 칸이 비어 있으면 붙이지 않음)

CSV 출력 (한 사이클(S1~S2)당 한 행):
    Timestamp, Interval_ms, Torque_<단위>, RPM

최초 1회 설정:
    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt

사용법 (.venv 활성화 후):
    python ha130tr_logger.py --port COM4            # 창(그래프 + 기록 버튼)
    python ha130tr_logger.py --suffix 시험1          # 접미사 입력 칸 초기값
    python ha130tr_logger.py --no-plot --suffix A   # 창 없이 바로 기록 (Ctrl+C로 종료)
    python ha130tr_logger.py --unit N               # 장비 F-13 단위 설정과 맞출 것
    python ha130tr_logger.py --list                 # 포트 목록

통신이 끊기면(USB 분리 등) 1초마다 재연결을 시도하고, 기록 중이면 같은 CSV에 이어서 기록한다.
"""
import argparse
import bisect
import csv
import math
import re
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.exit("pyserial이 필요합니다:  pip install -r requirements.txt")

LINE_RE = re.compile(r"^\s*S(\d)\s*,\s*NT\s*,\s*([+-]?\s*\d+(?:\.\d+)?)\s*$", re.IGNORECASE)
PARITY = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}
STOPBITS = {1: serial.STOPBITS_ONE, 2: serial.STOPBITS_TWO}
# 장비 F-13 Force unit 설정값과 동일하게 지정 (장비는 단위를 전송하지 않음)
UNITS = ["kgf", "N", "lb", "kgcm2", "bar", "MPa"]

WINDOW_MIN, WINDOW_MAX = 5, 600          # 그래프 시간축 폭 범위(초)
PLOT_BUFFER = WINDOW_MAX * 20            # 최대 폭을 채울 만큼 보관 (15 Hz 기준 여유)
NO_DATA_SEC = 2.0                        # 이 시간 동안 수신이 없으면 경고 (정상 주기 ~66 ms)
RECONNECT_SEC = 1.0                      # 통신 끊김 시 재연결 시도 간격
BAD_LINE_PRINT = 10                      # 형식 불일치 줄 경고는 처음 N개 + 이후 100개마다
FILE_PREFIX = "HA130TR"
INVALID_FN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')   # Windows 파일명 금지 문자


# ----------------------------------------------------------------------------------------
# 파일명
# ----------------------------------------------------------------------------------------
def clean_suffix(text):
    """접미사 입력값 -> 파일명에 쓸 수 있는 문자열 (금지 문자는 '_', 앞뒤 공백/마침표 제거)"""
    s = INVALID_FN.sub("_", (text or "").strip()).strip(" .")
    return s[:80]


def csv_name(start, suffix):
    """HA130TR_YYYYMMDD_HHMMSS[_접미사].csv"""
    s = clean_suffix(suffix)
    return f"{FILE_PREFIX}_{start:%Y%m%d_%H%M%S}" + (f"_{s}" if s else "") + ".csv"


def unique_path(outdir, start, suffix):
    """같은 이름이 있으면 (2), (3)... 을 붙여 덮어쓰기 방지"""
    base = csv_name(start, suffix)[:-len(".csv")]
    p = Path(outdir) / f"{base}.csv"
    k = 2
    while p.exists():
        p = Path(outdir) / f"{base} ({k}).csv"
        k += 1
    return p


# ----------------------------------------------------------------------------------------
# CSV 기록 (시작/종료 반복 가능)
# ----------------------------------------------------------------------------------------
class Recorder:
    """기록 중일 때만 CSV에 쓴다. 수신 스레드(write)와 GUI 스레드(start/stop)가 함께 쓰므로 lock 사용."""

    def __init__(self, columns):
        self.columns = columns          # 값 열 이름 (채널 순서)
        self.lock = threading.Lock()
        self.f = None
        self.path = None

    @property
    def active(self):
        return self.f is not None

    def start(self, path):
        with self.lock:
            if self.f is not None:
                raise RuntimeError("이미 기록 중입니다.")
            f = open(path, "x", newline="", encoding="utf-8-sig")   # x: 기존 파일 덮어쓰기 금지
            self.w = csv.writer(f)
            self.w.writerow(["Timestamp", "Interval_ms"] + self.columns)
            f.flush()
            self.f, self.path = f, Path(path)
            self.started = datetime.now()
            self.count = 0
            self.first_time = self.last_time = None
            self.iv_n, self.iv_sum, self.iv_min, self.iv_max = 0, 0.0, math.inf, 0.0
            self.gaps = 0

    def write(self, t, values):
        """한 사이클 기록. 기록 중이 아니거나 기록 시작 전에 시작된 사이클이면 False"""
        with self.lock:
            if self.f is None or t < self.started:
                return False
            interval = ""
            if self.last_time is not None:
                iv = (t - self.last_time).total_seconds() * 1000
                interval = f"{iv:.0f}"          # CSV에는 끊김 공백도 그대로 남김
                if iv <= NO_DATA_SEC * 1000:    # 통계는 장비 주기만 (끊김 공백 제외)
                    self.iv_n += 1
                    self.iv_sum += iv
                    self.iv_min = min(self.iv_min, iv)
                    self.iv_max = max(self.iv_max, iv)
                else:
                    self.gaps += 1
            else:
                self.first_time = t
            self.last_time = t
            self.w.writerow([t.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], interval]
                            + ["" if v is None else f"{v:.1f}" for v in values])
            self.f.flush()                      # 비정상 종료돼도 여기까지는 남음
            self.count += 1
            return True

    def stop(self):
        """기록 종료 -> 요약 dict (기록 중이 아니면 None)"""
        with self.lock:
            if self.f is None:
                return None
            self.f.close()
            self.f = None
            dur = ((self.last_time - self.first_time).total_seconds()
                   if self.first_time and self.last_time else 0.0)
            return dict(path=self.path, rows=self.count, duration=dur, gaps=self.gaps,
                        iv_mean=self.iv_sum / self.iv_n if self.iv_n else None,
                        iv_min=self.iv_min if self.iv_n else None,
                        iv_max=self.iv_max if self.iv_n else None)


def summary_text(s):
    lines = [f"저장: {s['path']}", f"  {s['rows']:,} 행, 길이 {s['duration']:,.1f} s"]
    if s["iv_mean"] is not None:
        lines.append(f"  주기: 평균 {s['iv_mean']:.1f} ms, 최소 {s['iv_min']:.0f} ms, "
                     f"최대 {s['iv_max']:.0f} ms")
    if s["gaps"]:
        lines.append(f"  수신 끊김 구간: {s['gaps']}회 (CSV의 Interval_ms > {NO_DATA_SEC * 1000:.0f} 인 행)")
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------
# 수신 / 파싱 (기록 여부와 무관하게 항상 동작)
# ----------------------------------------------------------------------------------------
class Receiver:
    def __init__(self, unit, recorder, show_raw=False):
        # 채널 번호 -> 화면 표시 이름. 순서 = CSV 값 열 순서
        self.channels = {"1": f"Torque[{unit}]", "2": "RPM"}
        self.last_ch = "2"
        self.recorder = recorder
        self.show_raw = show_raw
        self.record = {}            # ch -> value (진행 중인 사이클)
        self.rec_time = None        # 사이클 첫 줄 수신 시각
        self.rec_perf = None
        self.last_time = None       # 직전 사이클 시작 시각 (화면 주기 표시용)
        self.synced = False         # 사이클 시작(S1)을 한 번이라도 받았는지
        self.cycles = 0             # 수신한 사이클 수 (기록 여부 무관)
        self.bad_lines = 0
        self.recent = deque(maxlen=50)      # 최근 수신 주기(ms)

        # 연결/수신 상태 (화면에서 읽음)
        self.t_start = time.perf_counter()
        self.last_rx = self.t_start
        self.connected = True

        # 그래프용 데이터 (수신 스레드가 쓰고 GUI가 읽음)
        self.lock = threading.Lock()
        self.plot_t = deque(maxlen=PLOT_BUFFER)
        self.plot_v = {ch: deque(maxlen=PLOT_BUFFER) for ch in self.channels}

    def resync(self):
        """통신 재연결 후: 받다 만 사이클은 버리고 다음 S1부터 다시 시작"""
        self.record = {}
        self.synced = False

    def process_line(self, line: str):
        line = line.strip()
        if not line:
            return
        if self.show_raw:
            print(f"  RAW: {line}")

        m = LINE_RE.match(line)
        if not m or m.group(1) not in self.channels:
            self.bad_lines += 1
            if self.bad_lines <= BAD_LINE_PRINT or self.bad_lines % 100 == 0:
                print(f"  [무시] 형식 불일치 #{self.bad_lines}: {line!r}")
            return
        self.last_rx = time.perf_counter()
        ch = m.group(1)
        value = float(m.group(2).replace(" ", ""))

        # 사이클 중간(S2)부터 받으면 첫 S1이 올 때까지 대기
        if not self.synced:
            if ch != "1":
                return
            self.synced = True

        # 같은 채널이 다시 오면(데이터 누락 등) 이전 사이클을 먼저 처리
        if ch in self.record:
            self.flush_record()
        if not self.record:
            self.rec_time = datetime.now()
            self.rec_perf = time.perf_counter()
        self.record[ch] = value

        if ch == self.last_ch:
            self.flush_record()

    def flush_record(self):
        if not self.record:
            return
        t = self.rec_time
        if self.last_time is not None:
            iv = (t - self.last_time).total_seconds() * 1000
            if iv <= NO_DATA_SEC * 1000:
                self.recent.append(iv)
        self.last_time = t
        self.cycles += 1

        values = [self.record.get(ch) for ch in self.channels]
        written = self.recorder.write(t, values)

        with self.lock:
            self.plot_t.append(self.rec_perf - self.t_start)
            for ch in self.channels:
                self.plot_v[ch].append(self.record.get(ch, float("nan")))

        if written:
            disp = "  ".join(f"{label}={'--' if v is None else f'{v:.1f}':>9}"
                             for label, v in zip(self.channels.values(), values))
            iv = f"주기 {self.recent[-1]:4.0f} ms" if self.recent else ""
            print(f"[{t.strftime('%H:%M:%S.%f')[:-3]}] #{self.recorder.count:<7} {disp}   {iv}")

        self.record = {}
        self.rec_time = None

    def snapshot(self):
        """그래프용 데이터 복사본"""
        with self.lock:
            return list(self.plot_t), {ch: list(v) for ch, v in self.plot_v.items()}

    def elapsed(self):
        return time.perf_counter() - self.t_start


def read_loop(open_port, ser, rx, stop):
    """시리얼 수신 -> 줄 분리 -> Receiver. 끊기면 재연결. stop 이벤트가 설정되면 종료."""
    warned_no_data = False
    try:
        while not stop.is_set():
            # ---- (재)연결 ----
            if ser is None:
                try:
                    ser = open_port()
                except (serial.SerialException, OSError):
                    stop.wait(RECONNECT_SEC)
                    continue
                rx.connected = True
                rx.last_rx = time.perf_counter()
                print("\n[연결] 시리얼 포트 재연결됨" +
                      (" - 같은 파일에 이어서 기록합니다." if rx.recorder.active else "") + "\n")

            buf = b""
            first_line = True       # (재)연결 직후 중간부터 받은 첫 조각은 버림
            rx.resync()
            try:
                while not stop.is_set():
                    chunk = ser.read(ser.in_waiting or 1)
                    now = time.perf_counter()
                    if chunk:
                        buf += chunk
                        # CR, LF, CRLF 모두 줄 구분자로 처리
                        parts = re.split(rb"\r\n|\r|\n", buf)
                        buf = parts.pop()
                        if parts and first_line:
                            parts.pop(0)
                            first_line = False
                        for p in parts:
                            rx.process_line(p.decode("ascii", errors="replace"))
                        if len(buf) > 1024:   # 비정상 데이터 보호
                            buf = b""

                    # 수신 없음 감시
                    silent = now - rx.last_rx
                    if silent > NO_DATA_SEC and not warned_no_data:
                        warned_no_data = True
                        print(f"\n[경고] {NO_DATA_SEC:.0f}초 이상 데이터 수신 없음 "
                              "(장비 전원 / 케이블 / 통신속도 / F-09 ID=00 확인)\n")
                    elif silent <= NO_DATA_SEC and warned_no_data:
                        warned_no_data = False
                        print("\n[정상] 데이터 수신 재개\n")
            except (serial.SerialException, OSError) as e:
                print(f"\n[오류] 시리얼 통신 끊김: {e}\n       {RECONNECT_SEC:.0f}초마다 재연결 시도 중...\n")
                rx.connected = False
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
    finally:
        if ser is not None:
            ser.close()
        stop.set()


# ----------------------------------------------------------------------------------------
# 창 (Tk + matplotlib)
# ----------------------------------------------------------------------------------------
class LoggerApp:
    # 팔레트 (data_viewer.py와 동일): 면 / 잉크 / 격자 / 시리즈 / 기록(critical red)
    SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
    COLORS = {"1": "#2a78d6", "2": "#eb6834"}
    REC = "#d03b3b"

    def __init__(self, root, rx, recorder, outdir, port_desc, unit, window, suffix, stop):
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import ttk, messagebox
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

        self.tk, self.ttk, self.messagebox = tk, ttk, messagebox
        self.root, self.rx, self.recorder = root, rx, recorder
        self.outdir, self.unit, self.stop = Path(outdir), unit, stop
        self.rec_segments = []      # [(시작 경과초, 끝 경과초 또는 None)] -> 그래프 빨간 배경
        self.rec_patches = []
        self.closed = False

        root.title("HA-130TR 로거")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{int(sw * 0.75)}x{int(sh * 0.8)}+{int(sw * 0.1)}+{int(sh * 0.05)}")
        em = tkfont.nametofont("TkDefaultFont").measure("가")
        style = ttk.Style()
        style.configure("Rec.TButton", font=("Malgun Gothic", 11, "bold"))

        # ---- 1행: 기록 버튼 / 접미사 ----
        bar = ttk.Frame(root, padding=(8, 8, 8, 2))
        bar.pack(fill=tk.X)
        self.start_btn = ttk.Button(bar, text="●  기록 시작", style="Rec.TButton",
                                    command=self.start_recording)
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(bar, text="■  기록 종료", style="Rec.TButton",
                                   command=self.stop_recording, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(6, 16))
        ttk.Label(bar, text="파일명 접미사").pack(side=tk.LEFT)
        self.suffix_var = tk.StringVar(value=suffix or "")
        self.suffix_entry = ttk.Entry(bar, textvariable=self.suffix_var, width=22)
        self.suffix_entry.pack(side=tk.LEFT, padx=(6, 8))
        self.suffix_entry.bind("<Return>", lambda e: self.start_recording())
        self.name_lbl = ttk.Label(bar, foreground=self.INK2)
        self.name_lbl.pack(side=tk.LEFT)
        self.suffix_var.trace_add("write", lambda *_: self._update_name_preview())
        self._update_name_preview()

        # ---- 2행: 상태 / 시간축 폭 ----
        bar2 = ttk.Frame(root, padding=(8, 2, 8, 2))
        bar2.pack(fill=tk.X)
        self.state_lbl = tk.Label(bar2, font=("Malgun Gothic", 11, "bold"), anchor="w")
        self.state_lbl.pack(side=tk.LEFT)
        self.win_lbl = ttk.Label(bar2, width=6, anchor="e")
        self.win_lbl.pack(side=tk.RIGHT)
        self.win_scale = ttk.Scale(bar2, from_=math.log10(WINDOW_MIN), to=math.log10(WINDOW_MAX),
                                   length=em * 14, command=lambda _: self._update_win_label())
        self.win_scale.set(math.log10(min(max(window, WINDOW_MIN), WINDOW_MAX)))
        self.win_scale.pack(side=tk.RIGHT, padx=4)
        ttk.Label(bar2, text="시간축 폭").pack(side=tk.RIGHT)
        self._update_win_label()

        # ---- 아래 상태줄 ----
        self.info_lbl = ttk.Label(root, foreground=self.INK2, padding=(8, 2), anchor="w")
        self.info_lbl.pack(side=tk.BOTTOM, fill=tk.X)
        self.port_desc = port_desc
        self.last_saved = ""

        # ---- 그래프 ----
        self.fig = Figure(figsize=(10, 6), facecolor=self.SURFACE, layout="constrained")
        self.axes = self.fig.subplots(2, 1, sharex=True)
        self.titles = {"1": f"Torque [{unit}]", "2": "RPM"}
        self.lines, self.title_art = {}, {}
        for ax, ch in zip(self.axes, ("1", "2")):
            ax.set_facecolor(self.SURFACE)
            ax.grid(True, color=self.GRID, linewidth=0.6)
            ax.set_axisbelow(True)
            for s in ("top", "right"):
                ax.spines[s].set_visible(False)
            for s in ("left", "bottom"):
                ax.spines[s].set_color(self.GRID)
            ax.tick_params(colors=self.INK2, labelsize=9)
            (self.lines[ch],) = ax.plot([], [], color=self.COLORS[ch], linewidth=1.6)
            self.title_art[ch] = ax.set_title(self.titles[ch], loc="left", color=self.INK,
                                              fontsize=11, fontfamily=["Malgun Gothic", "DejaVu Sans"])
        self.axes[1].set_xlabel("경과 시간 [s]   (빨간 배경 = 기록된 구간)", color=self.INK2,
                                fontfamily=["Malgun Gothic", "DejaVu Sans"])
        self.canvas = FigureCanvasTkAgg(self.fig, master=root)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._set_buttons()

    # ---------------- 표시 보조 ----------------
    def window_sec(self):
        return round(10 ** float(self.win_scale.get()))

    def _update_win_label(self):
        self.win_lbl.config(text=f"{self.window_sec()} s")

    def _update_name_preview(self):
        if self.recorder.active:
            return
        name = csv_name(datetime(2000, 1, 1), self.suffix_var.get()).replace(
            "20000101_000000", "<시작시각>")
        self.name_lbl.config(text=f"→ {name}")

    def _set_buttons(self):
        tk = self.tk
        rec = self.recorder.active
        self.start_btn.config(state=tk.DISABLED if rec else tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL if rec else tk.DISABLED)
        self.suffix_entry.config(state=tk.DISABLED if rec else tk.NORMAL)   # 기록 중에는 이름 고정

    # ---------------- 기록 시작 / 종료 ----------------
    def start_recording(self):
        if self.recorder.active:
            return
        start = datetime.now()
        path = unique_path(self.outdir, start, self.suffix_var.get())
        try:
            self.outdir.mkdir(parents=True, exist_ok=True)
            self.recorder.start(path)
        except OSError as e:
            self.messagebox.showerror("기록 시작 실패", f"파일을 만들 수 없습니다.\n{path}\n\n{e}")
            return
        self.rec_segments.append([self.rx.elapsed(), None])
        self.name_lbl.config(text=f"기록 파일: {path.name}", foreground=self.REC)
        print(f"\n[기록 시작] {path}\n")
        self._set_buttons()

    def stop_recording(self):
        s = self.recorder.stop()
        if s is None:
            return
        if self.rec_segments and self.rec_segments[-1][1] is None:
            self.rec_segments[-1][1] = self.rx.elapsed()
        print(f"\n[기록 종료]\n{summary_text(s)}\n")
        self.last_saved = f"마지막 저장: {s['path'].name}  ({s['rows']:,} 행, {s['duration']:,.1f} s)"
        self.name_lbl.config(foreground=self.INK2)
        self._update_name_preview()
        self._set_buttons()

    def on_close(self):
        if self.recorder.active:
            if not self.messagebox.askyesno(
                    "종료", "기록 중입니다.\n기록을 종료(저장)하고 프로그램을 닫을까요?"):
                return
            self.stop_recording()
        self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        self.root.destroy()

    # ---------------- 주기 갱신 (0.1 s) ----------------
    def tick(self):
        if self.closed:
            return
        if self.stop.is_set():          # Ctrl+C 또는 수신 스레드 종료
            self.stop_recording()
            self.close()
            return
        rx = self.rx
        ts, vals = rx.snapshot()
        w = self.window_sec()
        now = rx.elapsed()
        x0, x1 = (0, w) if now < w else (now - w, now)   # 초기에는 0초부터 채워 나감
        self.axes[1].set_xlim(x0, x1)

        lo = bisect.bisect_left(ts, x0)
        vis_t = ts[lo:]
        for ax, ch in zip(self.axes, ("1", "2")):
            vis_v = vals[ch][lo:]
            self.lines[ch].set_data(vis_t, vis_v)
            finite = [v for v in vis_v if v == v]
            if finite:
                vmin, vmax = min(finite), max(finite)
                pad = max((vmax - vmin) * 0.1, 0.5)
                ax.set_ylim(vmin - pad, vmax + pad)
                self.title_art[ch].set_text(f"{self.titles[ch]}    현재 {finite[-1]:.1f}")

        # 기록 구간 배경
        for p in self.rec_patches:
            p.remove()
        self.rec_patches = []
        for a, b in self.rec_segments:
            b = now if b is None else b
            if b >= x0 and a <= x1:
                for ax in self.axes:
                    self.rec_patches.append(ax.axvspan(a, b, color=self.REC, alpha=0.07, linewidth=0))
        self.canvas.draw_idle()

        # 상태 표시
        silent = time.perf_counter() - rx.last_rx
        rec = self.recorder
        if not rx.connected:
            state, color = "연결 끊김 - 재연결 시도 중...", self.REC
        elif silent > NO_DATA_SEC:
            state, color = f"[경고] {silent:.0f}초간 수신 없음 - 장비 전원 / 케이블 / 통신 설정 확인", self.REC
        elif rec.active:
            el = (datetime.now() - rec.started).total_seconds()
            state, color = (f"●  기록 중   {int(el // 3600):02d}:{int(el % 3600 // 60):02d}:{int(el % 60):02d}"
                            f"   |   {rec.count:,} 행", self.REC)
        else:
            state, color = "대기 중 (수신·그래프 동작, 저장 안 함)", self.INK2
            if self.last_saved:
                state += f"      {self.last_saved}"
        if rec.active and (not rx.connected or silent > NO_DATA_SEC):
            state = "●  기록 중   |   " + state
        self.state_lbl.config(text=state, fg=color)
        iv = f"{rx.recent[-1]:.0f} ms" if rx.recent else "-"
        self.info_lbl.config(text=f"{self.port_desc}   |   수신 주기 {iv}   |   저장 폴더 {self.outdir}")
        self.root.after(100, self.tick)


# ----------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="HA-130TR 시리얼 데이터 CSV 로거")
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--baud", type=int, choices=[2400, 4800, 9600], default=9600,
                    help="장비 F-10 설정과 동일 (기본 9600)")
    ap.add_argument("--parity", choices=PARITY.keys(), default="N")
    ap.add_argument("--databits", type=int, choices=[7, 8], default=8)
    ap.add_argument("--stopbits", type=int, choices=[1, 2], default=1)
    ap.add_argument("--unit", choices=UNITS, default="kgf",
                    help="토크 단위. 장비 F-13 설정과 맞출 것 (기본 kgf)")
    ap.add_argument("--outdir", default=str(Path(__file__).parent / "data"))
    ap.add_argument("--suffix", default="",
                    help="파일명 접미사 (창 모드: 입력 칸 초기값, --no-plot: 그대로 사용)")
    ap.add_argument("--no-plot", action="store_true", help="창 없이 시작하자마자 기록 (Ctrl+C로 종료)")
    ap.add_argument("--window", type=float, default=30,
                    help=f"그래프 시간축 초기 폭(초, {WINDOW_MIN}~{WINDOW_MAX}, 기본 30)")
    ap.add_argument("--raw", action="store_true", help="원본 수신 줄 출력")
    ap.add_argument("--list", action="store_true", help="사용 가능한 시리얼 포트 출력")
    args = ap.parse_args()

    if args.list:
        for p in serial.tools.list_ports.comports():
            print(f"{p.device}\t{p.description}")
        return

    def open_port():
        s = serial.Serial(args.port, args.baud, bytesize=args.databits,
                          parity=PARITY[args.parity], stopbits=STOPBITS[args.stopbits],
                          timeout=0.1)
        s.reset_input_buffer()
        return s

    # 시작 시 포트를 못 열면 바로 종료 (설정 오류일 가능성이 높음)
    try:
        ser = open_port()
    except serial.SerialException as e:
        print(f"[오류] {args.port} 를 열 수 없습니다: {e}")
        print("  - Tera Term 등 다른 프로그램이 포트를 사용 중이면 먼저 닫아 주세요.")
        ports = ", ".join(p.device for p in serial.tools.list_ports.comports())
        print(f"  - 사용 가능한 포트: {ports or '(없음)'}")
        sys.exit(1)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    recorder = Recorder([f"Torque_{args.unit}", "RPM"])
    rx = Receiver(args.unit, recorder, args.raw)
    port_desc = f"{args.port} ({args.baud}, {args.databits}{args.parity}{args.stopbits})"
    print(f"포트 : {port_desc}")
    print(f"단위 : Torque = {args.unit} (장비 F-13 설정과 일치해야 함), RPM")
    print(f"저장 : {outdir}")
    stop = threading.Event()

    if args.no_plot:
        path = unique_path(outdir, datetime.now(), args.suffix)
        recorder.start(path)
        print(f"기록 : {path}\n종료 : Ctrl+C\n")
        try:
            read_loop(open_port, ser, rx, stop)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            s = recorder.stop()
            print("\n" + summary_text(s))
        return

    # ---- 창 모드 ----
    import tkinter as tk
    try:                                    # 고해상도 모니터에서 흐릿하지 않게
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    app = LoggerApp(root, rx, recorder, outdir, port_desc, args.unit, args.window, args.suffix, stop)
    print("[기록 시작] 버튼을 누르면 저장을 시작합니다. 창을 닫으면 종료.\n")

    # 무거운 GUI 초기화(첫 렌더링)를 끝낸 뒤 수신 시작 -> 초기 타임스탬프 지연 방지
    root.update()
    app.canvas.draw()
    ser.reset_input_buffer()
    rx.t_start = rx.last_rx = time.perf_counter()
    reader = threading.Thread(target=read_loop, args=(open_port, ser, rx, stop), daemon=True)
    reader.start()

    signal.signal(signal.SIGINT, lambda *_: stop.set())     # Ctrl+C -> 다음 tick에서 저장 후 종료
    root.after(100, app.tick)
    try:
        root.mainloop()
    finally:
        stop.set()
        reader.join(timeout=2)
        s = recorder.stop()                 # 혹시 남아 있으면 저장
        if s:
            print("\n" + summary_text(s))


if __name__ == "__main__":
    main()
