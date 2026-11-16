r"""
HA-130TR RS232 -> USB Serial -> CSV 로거 (+ 실시간 그래프)

수신 형식 (매뉴얼 10장 RS232C Stream mode, 16 bytes/줄):
    S1,NT,-00001.4<CR><LF>     S1 = CH1 = TORQUE
    S2,NT,+00000.0<CR><LF>     S2 = CH2 = RPM
    (",NT," 는 고정문자)

CSV 출력 (한 사이클(S1~S2)당 한 행):
    Timestamp, Interval_ms, Torque_<단위>, RPM

최초 1회 설정:
    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt

사용법 (.venv 활성화 후):
    python ha130tr_logger.py --port COM4            # 로깅 + 실시간 그래프
    python ha130tr_logger.py --window 60            # 그래프 시간축 초기 폭(초)
    python ha130tr_logger.py --no-plot              # 그래프 없이 로깅만
    python ha130tr_logger.py --unit N               # 장비 F-13 단위 설정과 맞출 것
    python ha130tr_logger.py --list                 # 포트 목록
    python ha130tr_logger.py --raw                  # 원본 수신 줄도 화면에 출력
종료: 그래프 창 닫기 또는 Ctrl+C

통신이 끊기면(USB 분리 등) 1초마다 재연결을 시도하고, 연결되면 같은 CSV에 이어서 기록한다.
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

WINDOW_MIN, WINDOW_MAX = 5, 600          # 그래프 시간축 폭 슬라이더 범위(초)
PLOT_BUFFER = WINDOW_MAX * 20            # 최대 폭을 채울 만큼 보관 (15 Hz 기준 여유)
NO_DATA_SEC = 2.0                        # 이 시간 동안 수신이 없으면 경고 (정상 주기 ~66 ms)
RECONNECT_SEC = 1.0                      # 통신 끊김 시 재연결 시도 간격
BAD_LINE_PRINT = 10                      # 형식 불일치 줄 경고는 처음 N개 + 이후 100개마다


class Logger:
    def __init__(self, unit, csv_path, show_raw=False):
        # 채널 번호 -> (CSV 열 이름, 화면 표시 이름)
        self.channels = {"1": (f"Torque_{unit}", f"Torque[{unit}]"),
                         "2": ("RPM", "RPM")}
        self.last_ch = "2"
        self.show_raw = show_raw
        self.record = {}            # ch -> value (진행 중인 사이클)
        self.rec_time = None        # 사이클 첫 줄 수신 시각
        self.rec_perf = None
        self.last_time = None       # 직전 사이클 시작 시각
        self.synced = False         # 사이클 시작(S1)을 한 번이라도 받았는지
        self.count = 0
        self.bad_lines = 0
        self.gaps = 0               # 수신 끊김 구간 수

        # 주기 통계 (전체는 누적값만 보관해 메모리 일정)
        self.recent = deque(maxlen=50)
        self.iv_n, self.iv_sum, self.iv_min, self.iv_max = 0, 0.0, math.inf, 0.0

        # 연결/수신 상태 (그래프 상태줄에서 읽음)
        self.t_start = time.perf_counter()
        self.last_rx = self.t_start
        self.connected = True

        # 그래프용 데이터 (리더 스레드가 쓰고 그래프가 읽음)
        self.lock = threading.Lock()
        self.plot_t = deque(maxlen=PLOT_BUFFER)
        self.plot_v = {ch: deque(maxlen=PLOT_BUFFER) for ch in self.channels}

        self.csv_path = csv_path
        # utf-8-sig: Excel에서 바로 열림
        self.f = open(csv_path, "w", newline="", encoding="utf-8-sig")
        self.w = csv.writer(self.f)
        self.w.writerow(["Timestamp", "Interval_ms"] + [c[0] for c in self.channels.values()])
        self.f.flush()

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

        # 같은 채널이 다시 오면(데이터 누락 등) 이전 사이클을 먼저 기록
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
        interval = ""
        if self.last_time is not None:
            iv = (t - self.last_time).total_seconds() * 1000
            interval = f"{iv:.0f}"          # CSV에는 끊김 공백도 그대로 남김
            if iv <= NO_DATA_SEC * 1000:    # 통계는 장비 주기만 (끊김 공백 제외)
                self.recent.append(iv)
                self.iv_n += 1
                self.iv_sum += iv
                self.iv_min = min(self.iv_min, iv)
                self.iv_max = max(self.iv_max, iv)
            else:
                self.gaps += 1
        self.last_time = t

        row = [t.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], interval]
        disp = []
        for ch, (_, label) in self.channels.items():
            if ch in self.record:
                row.append(f"{self.record[ch]:.1f}")
                disp.append(f"{label}={self.record[ch]:9.1f}")
            else:
                row.append("")
                disp.append(f"{label}={'--':>9}")
        self.w.writerow(row)
        self.f.flush()
        self.count += 1

        with self.lock:
            self.plot_t.append(self.rec_perf - self.t_start)
            for ch in self.channels:
                self.plot_v[ch].append(self.record.get(ch, float("nan")))

        stat = ""
        if self.recent:
            r = self.recent
            stat = (f"주기 {r[-1]:4.0f} ms (최근{len(r)}개 평균 {sum(r) / len(r):.0f}"
                    f" / 최소 {min(r):.0f} / 최대 {max(r):.0f})")
        print(f"[{t.strftime('%H:%M:%S.%f')[:-3]}] #{self.count:<6} {'  '.join(disp)}   {stat}")

        self.record = {}
        self.rec_time = None

    def snapshot(self):
        """그래프용 데이터 복사본"""
        with self.lock:
            return list(self.plot_t), {ch: list(v) for ch, v in self.plot_v.items()}

    def close(self):
        self.record = {}        # 종료 시점에 받다 만 사이클(S1만 수신 등)은 버림
        self.f.close()
        print(f"\n종료: {self.count} 행 저장 -> {self.csv_path}")
        if self.iv_n:
            print(f"측정 주기: 평균 {self.iv_sum / self.iv_n:.1f} ms, "
                  f"최소 {self.iv_min:.0f} ms, 최대 {self.iv_max:.0f} ms ({self.iv_n}개 샘플)")
        if self.gaps:
            print(f"수신 끊김 구간: {self.gaps}회 (CSV의 Interval_ms > {NO_DATA_SEC * 1000:.0f} 인 행)")
        if self.bad_lines:
            print(f"형식 불일치로 무시한 줄: {self.bad_lines}")


def read_loop(open_port, ser, log, stop):
    """시리얼 수신 -> 줄 분리 -> Logger. 끊기면 재연결. stop 이벤트가 설정되면 종료."""
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
                log.connected = True
                log.last_rx = time.perf_counter()
                print("\n[연결] 시리얼 포트 재연결됨 - 같은 파일에 이어서 기록합니다.\n")

            buf = b""
            first_line = True       # (재)연결 직후 중간부터 받은 첫 조각은 버림
            log.resync()
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
                            log.process_line(p.decode("ascii", errors="replace"))
                        if len(buf) > 1024:   # 비정상 데이터 보호
                            buf = b""

                    # 수신 없음 감시
                    silent = now - log.last_rx
                    if silent > NO_DATA_SEC and not warned_no_data:
                        warned_no_data = True
                        print(f"\n[경고] {NO_DATA_SEC:.0f}초 이상 데이터 수신 없음 "
                              "(장비 전원 / 케이블 / 통신속도 / F-09 ID=00 확인)\n")
                    elif silent <= NO_DATA_SEC and warned_no_data:
                        warned_no_data = False
                        print("\n[정상] 데이터 수신 재개\n")
            except (serial.SerialException, OSError) as e:
                print(f"\n[오류] 시리얼 통신 끊김: {e}\n       {RECONNECT_SEC:.0f}초마다 재연결 시도 중... "
                      "(종료하려면 창 닫기 / Ctrl+C)\n")
                log.connected = False
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
    finally:
        if ser is not None:
            ser.close()
        stop.set()


def run_plot(log, unit, window, stop, on_ready):
    """실시간 그래프. 창을 닫거나 stop 이벤트가 설정되면 반환.
    on_ready: 그래프 준비(무거운 초기화) 후 호출 -> 이때 수신 시작 (초기 타임스탬프 지연 방지)"""
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    from matplotlib.widgets import Slider

    # 팔레트: 차트 면 / 잉크 / 시리즈 색 (Torque=blue, RPM=orange) / 경고(critical)
    SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
    CRITICAL = "#d03b3b"
    COLORS = {"1": "#2a78d6", "2": "#eb6834"}
    TITLES = {"1": f"Torque [{unit}]", "2": "RPM"}

    plt.rcParams.update({
        "font.family": ["Malgun Gothic", "DejaVu Sans"],
        "axes.unicode_minus": False,
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
        "axes.edgecolor": GRID, "axes.labelcolor": INK2,
        "xtick.color": INK2, "ytick.color": INK2,
        "axes.spines.top": False, "axes.spines.right": False,
    })

    fig, axes = plt.subplots(2, 1, sharex=True, figsize=(11, 6.5))
    fig.canvas.manager.set_window_title(f"HA-130TR 실시간 - {log.csv_path.name}")
    fig.subplots_adjust(left=0.08, right=0.97, top=0.90, bottom=0.17, hspace=0.35)

    lines, titles = {}, {}
    for ax, ch in zip(axes, ("1", "2")):
        ax.grid(True, color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        (lines[ch],) = ax.plot([], [], color=COLORS[ch], linewidth=1.6)
        titles[ch] = ax.set_title(TITLES[ch], loc="left", color=INK, fontsize=11)
    axes[1].set_xlabel("경과 시간 [s]")

    status = fig.text(0.08, 0.955, "", color=INK2, fontsize=9, va="center")

    # 시간축 폭 슬라이더 (로그 눈금: 짧은 구간도 세밀하게 조절)
    s_ax = fig.add_axes([0.20, 0.035, 0.62, 0.03], facecolor="#f0efec")
    window = min(max(window, WINDOW_MIN), WINDOW_MAX)
    slider = Slider(s_ax, "시간축 폭", math.log10(WINDOW_MIN), math.log10(WINDOW_MAX),
                    valinit=math.log10(window), color="#86b6ef", track_color="#f0efec")
    slider.label.set_color(INK2)
    slider.valtext.set_color(INK)

    def window_sec():
        return round(10 ** slider.val)

    def on_slide(_):
        slider.valtext.set_text(f"{window_sec()} s")

    slider.on_changed(on_slide)
    on_slide(None)

    def update(_):
        if stop.is_set():
            plt.close(fig)
            return
        ts, vals = log.snapshot()
        w = window_sec()
        now = time.perf_counter() - log.t_start
        x0, x1 = (0, w) if now < w else (now - w, now)   # 초기에는 0초부터 채워 나감
        axes[1].set_xlim(x0, x1)

        # 보이는 구간만 그림
        lo = bisect.bisect_left(ts, x0)
        vis_t = ts[lo:]
        for ax, ch in zip(axes, ("1", "2")):
            vis_v = vals[ch][lo:]
            lines[ch].set_data(vis_t, vis_v)
            finite = [v for v in vis_v if v == v]
            if finite:
                vmin, vmax = min(finite), max(finite)
                pad = max((vmax - vmin) * 0.1, 0.5)
                ax.set_ylim(vmin - pad, vmax + pad)
                titles[ch].set_text(f"{TITLES[ch]}    현재 {finite[-1]:.1f}")

        silent = time.perf_counter() - log.last_rx
        if not log.connected:
            status.set_text(f"[연결 끊김] 재연결 시도 중...   |   {log.csv_path.name}   |   {log.count} 행")
            status.set_color(CRITICAL)
        elif silent > NO_DATA_SEC:
            status.set_text(f"[경고] {silent:.0f}초간 수신 없음 - 장비 전원 / 케이블 / 통신 설정 확인"
                            f"   |   {log.count} 행")
            status.set_color(CRITICAL)
        else:
            iv = f"{log.recent[-1]:.0f} ms" if log.recent else "-"
            status.set_text(f"기록 중: {log.csv_path.name}   |   {log.count} 행   |   주기 {iv}"
                            f"   |   창을 닫으면 로깅 종료")
            status.set_color(INK2)

    # 그래프 표시 중 Ctrl+C -> 종료 이벤트 (다음 갱신 때 창이 닫힘)
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    anim = FuncAnimation(fig, update, interval=100, cache_frame_data=False)  # noqa: F841
    fig.canvas.draw()       # 첫 렌더링(폰트 로딩 등)을 수신 시작 전에 끝냄
    on_ready()
    plt.show()
    stop.set()


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
    ap.add_argument("--no-plot", action="store_true", help="실시간 그래프 끄기")
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
    csv_path = outdir / f"HA130TR_{datetime.now():%Y%m%d_%H%M%S}.csv"
    log = Logger(args.unit, csv_path, args.raw)
    print(f"포트 : {args.port} ({args.baud}, {args.databits}{args.parity}{args.stopbits})")
    print(f"단위 : Torque = {args.unit} (장비 F-13 설정과 일치해야 함), RPM")
    print(f"저장 : {csv_path}")
    print("종료 : " + ("Ctrl+C" if args.no_plot else "그래프 창 닫기 또는 Ctrl+C") + "\n")

    stop = threading.Event()
    try:
        if args.no_plot:
            read_loop(open_port, ser, log, stop)
        else:
            reader = threading.Thread(target=read_loop, args=(open_port, ser, log, stop),
                                      daemon=True)

            def start_reader():
                ser.reset_input_buffer()            # 그래프 준비 중 쌓인 데이터 버림
                log.t_start = log.last_rx = time.perf_counter()
                reader.start()

            run_plot(log, args.unit, args.window, stop, start_reader)
            stop.set()
            reader.join(timeout=2)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        log.close()


if __name__ == "__main__":
    main()
