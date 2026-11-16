"""
HA-130TR 데이터 수신 주기(주파수) 측정 도구

지정한 시간 동안 시리얼 데이터를 받아 채널별 수신 간격과 주파수를 통계로 보여준다.
CSV 파일은 만들지 않는다.

사용법 (.venv 활성화 후):
    python check_rate.py --port COM4 --baud 9600 --duration 10
"""
import argparse
import re
import statistics
import sys
import time
from collections import Counter, defaultdict

try:
    import serial
except ImportError:
    sys.exit("pyserial이 필요합니다:  pip install -r requirements.txt")

LINE_RE = re.compile(r"^\s*(S\d+)\s*,\s*([A-Za-z]+)\s*,\s*([+-]?\s*\d+(?:\.\d+)?)\s*$")
PARITY = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}


def summarize(name, times):
    """수신 시각 목록 -> 간격/주파수 통계 출력"""
    if len(times) < 2:
        print(f"  {name:<6} 수신 {len(times)}개 (통계 불가)")
        return
    iv = [(b - a) * 1000 for a, b in zip(times, times[1:])]
    mean = statistics.mean(iv)
    span = times[-1] - times[0]
    hz = (len(times) - 1) / span if span > 0 else 0
    sd = statistics.pstdev(iv)
    print(f"  {name:<6} 수신 {len(times):>5}개 | 주기 평균 {mean:8.2f} ms  "
          f"중앙값 {statistics.median(iv):8.2f}  최소 {min(iv):8.2f}  최대 {max(iv):8.2f}  "
          f"표준편차 {sd:6.2f} | {hz:7.3f} Hz")
    return iv


def main():
    ap = argparse.ArgumentParser(description="HA-130TR 수신 주기 측정")
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--parity", choices=PARITY.keys(), default="N")
    ap.add_argument("--databits", type=int, choices=[7, 8], default=8)
    ap.add_argument("--stopbits", type=int, choices=[1, 2], default=1)
    ap.add_argument("--duration", type=float, default=10.0, help="측정 시간(초)")
    ap.add_argument("--raw", action="store_true", help="수신 줄과 상대 시각 출력")
    args = ap.parse_args()

    try:
        ser = serial.Serial(args.port, args.baud, bytesize=args.databits,
                            parity=PARITY[args.parity],
                            stopbits=serial.STOPBITS_TWO if args.stopbits == 2 else serial.STOPBITS_ONE,
                            timeout=0.01)
    except serial.SerialException as e:
        sys.exit(f"[오류] {args.port} 를 열 수 없습니다: {e}\n  (Tera Term/로거가 포트를 쓰고 있으면 닫아 주세요)")

    print(f"{args.port} ({args.baud}, {args.databits}{args.parity}{args.stopbits}) "
          f"에서 {args.duration:.0f}초 동안 측정합니다...")
    ser.reset_input_buffer()

    line_times = []                     # 모든 유효 줄 수신 시각
    ch_times = defaultdict(list)        # 채널별 수신 시각
    total_bytes = 0
    bad = 0
    raw_all = bytearray()               # 줄 끝 문자 판별용 원본
    first_line = True                   # 측정 시작 시 중간부터 받은 첫 조각은 버림
    buf = b""
    t0 = time.perf_counter()
    end = t0 + args.duration
    try:
        while time.perf_counter() < end:
            chunk = ser.read(ser.in_waiting or 1)
            if not chunk:
                continue
            now = time.perf_counter()
            total_bytes += len(chunk)
            raw_all += chunk
            buf += chunk
            parts = re.split(rb"\r\n|\r|\n", buf)
            buf = parts.pop()
            if parts and first_line:
                parts.pop(0)
                first_line = False
            for p in parts:
                line = p.decode("ascii", errors="replace").strip()
                if not line:
                    continue
                m = LINE_RE.match(line)
                if args.raw:
                    print(f"  {now - t0:9.4f}s  {line}")
                if not m:
                    bad += 1
                    continue
                line_times.append(now)
                ch_times[m.group(1).upper()].append(now)
    except KeyboardInterrupt:
        print("(중단됨)")
    finally:
        ser.close()

    elapsed = time.perf_counter() - t0
    print(f"\n===== 결과 (측정 {elapsed:.1f}초, {total_bytes} bytes, "
          f"{total_bytes / elapsed:.0f} B/s) =====")
    if not line_times:
        print("유효한 데이터가 수신되지 않았습니다. 포트/통신속도를 확인하세요.")
        return

    crlf = raw_all.count(b"\r\n")
    cr = raw_all.count(b"\r") - crlf
    lf = raw_all.count(b"\n") - crlf
    terms = Counter({"CRLF": crlf, "CR": cr, "LF": lf})
    print("줄 끝 문자: " + ", ".join(f"{k} {v}회" for k, v in terms.items() if v))
    sample = max(raw_all.split(b"\n"), key=len, default=b"")
    if sample:
        line_len = len(sample) + 1
        print(f"한 줄 길이: {line_len} bytes -> {args.baud}bps에서 전송 시간 "
              f"{line_len * (1 + args.databits + args.stopbits + (args.parity != 'N')) / args.baud * 1000:.1f} ms")
    if bad:
        print(f"형식 불일치 줄: {bad}개")

    print("\n[채널별]")
    for ch in sorted(ch_times):
        summarize(ch, ch_times[ch])
    print("\n[전체 줄]")
    summarize("ALL", line_times)

    # 채널 간 시간차 (예: S1 -> S2)
    chs = sorted(ch_times)
    if len(chs) >= 2:
        a, b = ch_times[chs[0]], ch_times[chs[1]]
        gaps, j = [], 0
        for ta in a:
            while j < len(b) and b[j] < ta:
                j += 1
            if j < len(b):
                gaps.append((b[j] - ta) * 1000)
        if gaps:
            print(f"\n[{chs[0]} -> {chs[1]} 시간차] 평균 {statistics.mean(gaps):.2f} ms, "
                  f"최소 {min(gaps):.2f}, 최대 {max(gaps):.2f}")

    print("\n※ USB-시리얼 변환기의 버퍼링(보통 1~16 ms) 때문에 개별 간격에는 지터가 있습니다.\n"
          "  실제 장비 주기는 '평균 주기' / 'Hz' 값을 보세요.")


if __name__ == "__main__":
    main()
