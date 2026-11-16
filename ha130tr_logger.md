# ha130tr_logger.py — HA-130TR 시리얼 CSV 로거 + 실시간 그래프

> 이 문서는 이 프로젝트를 처음 보는 AI 에이전트/개발자를 위한 설명이다.
> 코드만 봐서는 알 수 없는 **배경, 실측 사실, 설계 이유, 함정**을 중심으로 적었다.
> 짝 문서: [check_rate.md](check_rate.md) (수신 주기 측정), [data_viewer.md](data_viewer.md) (기록 데이터 뷰어)

## 1. 무엇을 하는 프로그램인가

해암이엔지(HAIAM) **HA-130TR** 디지털 인디케이터(토크/RPM 표시기)의 RS232C 출력을
USB-시리얼 변환기(PC에서 **COM4**)로 받아서

1. 한 사이클(S1 + S2)당 한 행씩 **CSV로 저장**하고 (시작부터 종료까지 전부, 크기 제한 없음)
2. **실시간 그래프**(Torque / RPM 2단)를 띄운다. 그래프 아래 슬라이더로 시간축 폭을 실시간 변경.

## 2. 장비와 통신 프로토콜 (매뉴얼 근거)

출처: `HA-130TR,130DW,230AT-통합메뉴얼.pdf` 10장 "Option-02 (RS232C)". 사용자가 대화 중 제공했고 저장소에는 없다.

| 항목 | 값 |
|---|---|
| 통신 | 8N1, 패리티 없음, 2400/4800/**9600** bps (장비 F-10, 기본 9600) |
| 모드 | 장비 F-09 ID = **00 → Stream mode**(상시 송신, 현재 사용). 01~32 → Command mode(PC가 `ID01P` 등으로 요청) |
| 한 줄 | 16 bytes 고정: `S1,NT,-00001.4\r\n` |
| 채널 | `S1` = CH1 = **Torque**, `S2` = CH2 = **RPM** (HA-130TR 기준) |
| `,NT,` | 매뉴얼상 **고정문자**. 상태값이 아니다 → CSV에 저장하지 않음 |
| 값 | 부호 포함 8 bytes. 소수점 자리는 장비 F-01 설정(기본 1자리) |
| 단위 | 장비는 단위를 **보내지 않음**. 장비 F-13(kgf/N/lb/…) 설정과 `--unit` 옵션을 사람이 맞춰야 함 |

매뉴얼에는 Stream mode의 **송신 주기가 나와 있지 않다** → [check_rate.py](check_rate.py)로 실측했다:

- S1, S2 각각 **약 66 ms 주기 (15.15 Hz)**. 줄 단위로는 S1, S2가 번갈아 약 33 ms 간격.
- 개별 간격이 **64 ms 또는 80 ms로만** 나온다(16 ms 차이). 이건 장비 지터가 아니라
  **USB-시리얼 변환기의 latency timer(16 ms) 양자화**다. 그래서 CSV 타임스탬프에는 ±16 ms 오차가 있다.
  타임스탬프를 더 정밀하게 하려면 장치관리자 → COM4 → 고급 → Latency Timer를 1 ms로 낮춘다(FTDI 칩일 때만).
- 9600 bps에서 회선 사용률은 약 50%. **2400 bps로 낮추면 현재 주기를 못 따라간다**(한 줄 67 ms).
- 장비 F-03 Display filter(기본 1/4초 평균)가 전송값에도 적용되는지는 **확인되지 않았다**.

## 3. 실행

```
.venv\Scripts\python.exe ha130tr_logger.py --port COM4           # 로깅 + 그래프
run_logger.bat                                                   # 위와 동일 (더블클릭용)
```

| 옵션 | 설명 |
|---|---|
| `--port COM4` / `--baud 9600` | 포트, 속도(2400/4800/9600만 허용) |
| `--unit kgf` | Torque 단위 이름(kgf, N, lb, kgcm2, bar, MPa). CSV 열 이름에 붙음 |
| `--window 30` | 그래프 시간축 초기 폭(초). 슬라이더 범위 5~600 s, 로그 눈금 |
| `--no-plot` | 그래프 없이 콘솔 로깅만 |
| `--outdir` | 저장 폴더 (기본 `./data`) |
| `--raw` | 수신 원본 줄도 콘솔에 출력 |
| `--list` | COM 포트 목록 |

종료: 그래프 창 닫기 또는 Ctrl+C. **Tera Term 등이 COM4를 잡고 있으면 열 수 없다.**

## 4. 출력 CSV

파일: `data/HA130TR_YYYYMMDD_HHMMSS.csv`, UTF-8 BOM(Excel에서 한글 깨짐 없음)

```
Timestamp,Interval_ms,Torque_kgf,RPM
2026-11-16 17:40:57.696,,-1.3,0.0
2026-11-16 17:40:57.760,64,-1.3,0.0
```

- `Timestamp`: 사이클 첫 줄(S1) **PC 수신 시각** (장비 시각 아님).
- `Interval_ms`: 직전 행과의 간격. 첫 행은 빈칸. **2000 ms 초과 = 수신 끊김 구간**(재연결 등)으로 해석.
- 값이 빠진 채널은 빈칸.
- 행마다 `flush()` → 프로그램이 비정상 종료돼도 그때까지 데이터는 남는다.
- 크기: 한 행 약 40 bytes → 1시간 ≈ 2.2 MB, 24시간 ≈ 52 MB / 131만 행.
  **Excel은 약 104만 행(≈19시간)까지만 연다** (데이터 자체는 파일에 다 있음).
- **구 형식**: 초기 버전은 `Timestamp,Interval_ms,S1_Status,S1_Value,S2_Status,S2_Value`였다.
  `data/`에 이 형식 파일이 남아 있을 수 있고, data_viewer.py는 둘 다 읽는다.

## 5. 구조

```
main()
 ├─ open_port()            # 포트 설정 클로저. 재연결에도 재사용
 ├─ Logger                 # 파싱 → 사이클 조립 → CSV 기록 → 통계 → 그래프 버퍼
 ├─ read_loop()            # 수신 스레드: 줄 분리, 수신없음 감시, 끊기면 재연결
 └─ run_plot()             # 메인 스레드: matplotlib(TkAgg) 창, 0.1 s마다 갱신
```

- **스레드**: 그래프 모드에서는 `read_loop`가 데몬 스레드, matplotlib은 메인 스레드(Tk 요구사항).
  `--no-plot`이면 `read_loop`를 메인 스레드에서 직접 돈다(Ctrl+C → KeyboardInterrupt).
- **공유 데이터**: `Logger.plot_t/plot_v`(deque, 최대 12000점 ≈ 13분)는 `Logger.lock`으로 보호.
  그래프는 `snapshot()`으로 복사본만 읽는다. CSV는 수신 스레드만 쓴다.
- **종료 신호**: `threading.Event stop`. 창 닫힘 → `plt.show()` 반환 → stop. 그래프 모드의 Ctrl+C는
  `signal.SIGINT` 핸들러가 stop만 세우고, 다음 갱신(update)에서 창을 닫는다.

### 사이클 조립 규칙 (`Logger.process_line`)
- 정규식 `^S(\d),NT,([+-]?\d+(\.\d+)?)$` (공백 허용, 대소문자 무시).
- S1이 오면 새 사이클 시작, S2(`last_ch`)가 오면 행 확정.
- 같은 채널이 사이클 안에서 다시 오면(줄 누락) 이전 사이클을 먼저 기록 → 빈칸 있는 행이 생김.
- **동기화(`synced`)**: 시작/재연결 직후 S2부터 받으면 첫 S1까지 버린다. 안 그러면 S2만 있는
  반쪽 행이 생겨 첫 주기가 ~33 ms로 왜곡된다(실제로 발생했던 버그).
- 시작/재연결 직후 **첫 줄 조각은 버린다**(줄 중간부터 받기 때문).
- 종료 시 미완성 사이클은 **버린다**(마찬가지로 통계 왜곡 방지).
- 줄 구분자는 CR / LF / CRLF 모두 허용(실제 장비는 CRLF).

### 안정성 기능
- **자동 재연결**: `SerialException/OSError` → 1초마다 `open_port()` 재시도 → 성공하면 **같은 CSV에 이어서** 기록.
  끊긴 시간은 다음 행의 `Interval_ms`에 그대로 남는다. 실제 USB 분리로 동작 확인됨(사용자 로그에 7~16 s 끊김 후 정상 이어짐).
- **시작 시** 포트를 못 열면 재시도하지 않고 즉시 종료(설정 오류일 가능성이 높으므로).
- **수신 없음 경고**: 2초(`NO_DATA_SEC`) 이상 유효한 줄이 없으면 콘솔 + 그래프 상태줄(빨강)에 경고, 재개 시 알림.
- **주기 통계**는 누적 합/최소/최대만 유지(메모리 일정). 2초 초과 간격은 통계에서 제외하고 `gaps`로 따로 센다.
- 형식 불일치 경고는 처음 10개 + 이후 100개마다만 출력.
- 그래프 모드에서는 **figure를 먼저 만들고 첫 렌더링을 끝낸 뒤(`on_ready`) 수신 스레드를 시작**한다.
  matplotlib import/첫 draw가 GIL을 잡아 초기 타임스탬프가 ~25 ms 밀리던 문제 때문.

### 그래프
- Torque와 RPM은 단위·스케일이 달라 **한 축에 겹치지 않고** 공유 x축 2단으로 그린다(이중 y축 금지).
- 색: Torque `#2a78d6`(파랑), RPM `#eb6834`(주황). data_viewer.py와 동일 팔레트.
- 시간축 슬라이더는 `log10` 값으로 동작(5~600 s). 선형이면 자주 쓰는 10~60 s가 왼쪽 끝에 몰려 조작이 어렵다.
- 처음에는 0 s부터 오른쪽으로 채워 나가고, 폭을 넘으면 스크롤.
- y축은 보이는 데이터 범위 + 10% 여백(최소 ±0.5)으로 자동.
- 한글 폰트: `Malgun Gothic`, `axes.unicode_minus=False`.

## 6. 환경

- Windows 11, **python.org Python 3.14** (사용자 PATH), 프로젝트 `.venv`.
  초기엔 uv로 설치했다가 사용자 요청("일반적인 방법")으로 제거하고 python.org + venv로 바꿨다.
- 의존성: `requirements.txt` (pyserial, matplotlib).
- `.venv\Scripts\python.exe`는 **런처**라서 실제 Python은 자식 프로세스로 뜬다 → 테스트 시 PID로 창을 찾으면 안 된다.
- 콘솔 출력이 한글이므로 stdout을 리다이렉트할 때는 `PYTHONIOENCODING=utf-8`.
- `run_logger.bat`은 **ASCII + CRLF**여야 한다. UTF-8 한글/LF로 저장하면 cmd가 줄을 깨뜨려 실행 실패(실제로 겪음).
  `.gitattributes`에 `*.bat text eol=crlf`.

## 7. 테스트 방법 (이전에 사용한 것)

- **실장비**: COM4에 장비가 연결된 상태에서 `--outdir`를 임시 폴더로 주고 몇 초 실행 후 CSV 확인.
- **창 닫기 종료**: 프로세스를 띄운 뒤 Win32 `EnumWindows`로 제목이 `HA-130TR*`인 창을 찾아 `WM_CLOSE`(0x0010) 전송 →
  정상 종료 요약 출력/CSV 확인. (`Process.CloseMainWindow()`는 런처 PID라서 실패한다.)
- **그래프 렌더링 확인**: `matplotlib.use("Agg")`, `FuncAnimation`을 가짜로 바꿔 update 함수를 직접 호출하고 `savefig`.
- **재연결/수신없음**: 실제 Serial을 감싼 가짜 객체로 "3초 정상 → 3초 무응답 → 예외", `open_port`는 2회 실패 후 성공시켜
  `read_loop(open_port, ser, log, stop)`를 직접 실행.
- 검증하지 못한 것: 그래프 모드 Ctrl+C, 슬라이더 실제 마우스 드래그, 수 시간 이상 장시간 실행.

## 8. 앞으로 손댈 때 참고

- 채널/열 정의는 `Logger.channels` 한 곳. 채널을 늘리면 `last_ch`(사이클 마지막 채널)도 맞출 것.
- CSV 형식을 바꾸면 **data_viewer.py의 `LogData`/`series_label`도 같이** 맞춰야 한다.
- 간격 2000 ms 기준(`NO_DATA_SEC`)은 data_viewer.py의 `GAP_MS`와 같은 값이어야 한다.
- 후보 기능(요청은 없었음): Command mode 폴링, 일정 시간/행마다 파일 분할(Excel 행 제한 대응).
