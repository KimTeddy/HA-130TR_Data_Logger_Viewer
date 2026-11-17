# ha130tr_logger.py — HA-130TR 시리얼 CSV 로거 (기록 시작/종료 버튼 + 실시간 그래프)

> 이 문서는 이 프로젝트를 처음 보는 개발자를 위한 설명이다.
> 코드만 봐서는 알 수 없는 **배경, 실측 사실, 설계 이유, 함정**을 중심으로 적었다.
> 짝 문서: [check_rate.md](check_rate.md) (수신 주기 측정), [data_viewer.md](data_viewer.md) (기록 데이터 뷰어)

## 1. 무엇을 하는 프로그램인가

해암이엔지(HAIAM) **HA-130TR** 디지털 인디케이터(토크/RPM 표시기)의 RS232C 출력을
USB-시리얼 변환기(PC에서 **COM4**)로 받아서

1. **실시간 그래프**(Torque / RPM 2단)를 계속 보여 주고 (프로그램이 켜져 있는 동안 항상 수신)
2. **[기록 시작] ~ [기록 종료] 동안만** 한 사이클(S1 + S2)당 한 행씩 CSV로 저장한다.
   프로그램을 끄지 않고 **여러 번 기록**할 수 있고, 기록마다 새 파일이 생긴다.
3. 파일명 끝에 붙일 **접미사**를 입력 칸에서 받는다(비어 있으면 안 붙임, 있으면 `_접미사`).

## 2. 장비와 통신 프로토콜 (매뉴얼 근거)

출처: `HA-130TR,130DW,230AT-통합메뉴얼.pdf` 10장 "Option-02 (RS232C)".

| 항목 | 값 |
|---|---|
| 통신 | 8N1, 패리티 없음, 2400/4800/**9600** bps (장비 F-10, 기본 9600) |
| 모드 | 장비 F-09 ID = **00 → Stream mode**(상시 송신, 현재 사용). 01~32 → Command mode(PC가 `ID01P` 등으로 요청) |
| 한 줄 | 16 bytes 고정: `S1,NT,-00001.4\r\n` |
| 채널 | `S1` = CH1 = **Torque**, `S2` = CH2 = **RPM** (HA-130TR 기준) |
| `,NT,` | 매뉴얼상 **고정문자**. 상태값이 아니다 → CSV에 저장하지 않음 |
| 값 | 부호 포함 8 bytes. 소수점 자리는 장비 F-01 설정(기본 1자리) |
| 단위 | 장비는 단위를 **보내지 않음**. 장비 F-13(kgf/N/lb/…) 설정과 `--unit` 옵션을 사람이 맞춰야 함 |

매뉴얼에는 Stream mode의 **송신 주기가 나와 있지 않다** → [check_rate.py](../check_rate.py)로 실측했다:

- S1, S2 각각 **약 66 ms 주기 (15.15 Hz)**. 줄 단위로는 S1, S2가 번갈아 약 33 ms 간격.
- 개별 간격이 **64 ms 또는 80 ms로만** 나온다(16 ms 차이). 이건 장비 지터가 아니라
  **USB-시리얼 변환기의 latency timer(16 ms) 양자화**다. 그래서 CSV 타임스탬프에는 ±16 ms 오차가 있다.
  타임스탬프를 더 정밀하게 하려면 장치관리자 → COM4 → 고급 → Latency Timer를 1 ms로 낮춘다(FTDI 칩일 때만).
- 9600 bps에서 회선 사용률은 약 50%. **2400 bps로 낮추면 현재 주기를 못 따라간다**(한 줄 67 ms).
- 장비 F-03 Display filter(기본 1/4초 평균)가 전송값에도 적용되는지는 **확인되지 않았다**.

## 3. 실행과 화면

```
run_logger.bat                                     # 더블클릭
python ha130tr_logger.py --port COM4               # 위와 동일
python ha130tr_logger.py --no-plot --suffix A      # 창 없이 바로 기록, Ctrl+C로 종료
```

| 옵션 | 설명 |
|---|---|
| `--port COM4` / `--baud 9600` | 포트, 속도(2400/4800/9600만 허용) |
| `--unit kgf` | Torque 단위 이름(kgf, N, lb, kgcm2, bar, MPa). CSV 열 이름에 붙음 |
| `--suffix 텍스트` | 창 모드: 접미사 입력 칸 초기값. `--no-plot`: 그대로 파일명에 사용 |
| `--window 30` | 그래프 시간축 초기 폭(초). 5~600 s, 로그 눈금 |
| `--no-plot` | 창 없이 **시작하자마자 기록**(파일 1개), Ctrl+C로 저장 후 종료 |
| `--outdir` | 저장 폴더 (기본 `./data`) |
| `--raw` | 수신 원본 줄도 콘솔에 출력 |
| `--list` | COM 포트 목록 |

**Tera Term 등이 COM4를 잡고 있으면 열 수 없다.**

### 창 구성 (Tkinter + matplotlib 내장)
| 위치 | 내용 |
|---|---|
| 1행 | **[● 기록 시작] [■ 기록 종료]**, **파일명 접미사** 입력 칸, 저장될 파일명 미리보기(`→ HA130TR_<시작시각>_접미사.csv`) / 기록 중에는 실제 파일명(빨강) |
| 2행 | 상태: `대기 중 …` + 마지막 저장 파일(행 수, 길이) / `● 기록 중 hh:mm:ss \| N 행`(빨강) / 연결 끊김·수신 없음 경고(빨강). 오른쪽에 **시간축 폭** 슬라이더 |
| 가운데 | Torque / RPM 그래프. **기록된 구간은 연한 빨간 배경**으로 표시 |
| 아래 | 포트 설정, 현재 수신 주기, 저장 폴더 |

- 기록 중에는 시작 버튼과 **접미사 칸이 비활성**(파일명이 기록 시작 시점에 고정되므로).
- 접미사 칸에서 **Enter = 기록 시작**.
- **기록 중에 창을 닫으면** "기록을 종료(저장)하고 닫을까요?" 확인 → 예: 저장 후 종료, 아니오: 계속.
- 콘솔 창에는 기록 중일 때만 행이 출력된다(대기 중에는 조용함). 기록 시작/종료 시 경로와 요약 출력.


## 4. 출력 CSV

파일명: `data/HA130TR_YYYYMMDD_HHMMSS[_접미사].csv` — 시각은 **[기록 시작]을 누른 시각**.
- 접미사는 `clean_suffix()`로 정리: 앞뒤 공백 제거, Windows 금지 문자 `\ / : * ? " < > |`와 제어문자는 `_`로,
  끝의 마침표/공백 제거, 최대 80자. 정리 후 비어 있으면 접미사 없음. 한글·공백·괄호는 그대로 허용.
- 같은 이름이 이미 있으면 `... (2).csv`, `(3)`…을 붙인다(`unique_path`). 파일은 `open(..., "x")`로 열어 **절대 덮어쓰지 않음**.

UTF-8 BOM(Excel에서 한글 깨짐 없음):

```
Timestamp,Interval_ms,Torque_kgf,RPM
2026-11-16 17:40:57.696,,-1.3,0.0
2026-11-16 17:40:57.760,64,-1.3,0.0
```

- `Timestamp`: 사이클 첫 줄(S1) **PC 수신 시각** (장비 시각 아님).
- `Interval_ms`: 같은 파일 안에서 직전 행과의 간격. **각 기록의 첫 행은 빈칸**. **2000 ms 초과 = 수신 끊김 구간**(재연결 등).
- 값이 빠진 채널은 빈칸.
- [기록 시작] 이전에 시작된 사이클은 넣지 않는다(`t < started`면 무시) → 첫 행은 항상 시작 이후의 완전한 사이클.
- 행마다 `flush()` → 프로그램이 비정상 종료돼도 그때까지 데이터는 남는다(강제 종료로 확인).
- 크기: 한 행 약 40 bytes → 1시간 ≈ 2.2 MB, 24시간 ≈ 52 MB / 131만 행.
  **Excel은 약 104만 행(≈19시간)까지만 연다** (데이터 자체는 파일에 다 있음).
- **구 형식**: 초기 버전은 `Timestamp,Interval_ms,S1_Status,S1_Value,S2_Status,S2_Value`였다.
  `data/`에 이 형식 파일이 남아 있을 수 있고, data_viewer.py는 둘 다 읽는다.

## 5. 구조

```
main()
 ├─ open_port()      # 포트 설정 클로저. 재연결에도 재사용
 ├─ Recorder         # CSV 파일 1개의 수명: start(path) / write(t, values) / stop() -> 요약. 반복 가능
 ├─ Receiver         # 파싱 → 사이클 조립 → recorder.write → 그래프 버퍼 (기록 여부와 무관하게 항상 동작)
 ├─ read_loop()      # 수신 스레드: 줄 분리, 수신없음 감시, 끊기면 재연결
 └─ LoggerApp        # 메인 스레드 Tk 창: 버튼/접미사/상태/그래프, root.after(100)로 tick
```

- **스레드**: 창 모드에서 `read_loop`는 데몬 스레드, Tk는 메인 스레드.
  `--no-plot`이면 `read_loop`를 메인 스레드에서 직접 돈다(Ctrl+C → KeyboardInterrupt → `recorder.stop()`).
- **Recorder.lock**: 수신 스레드의 `write`와 GUI 스레드의 `start/stop`이 겹치지 않게 보호.
  기록 상태(`active`) 판단도 `f is not None` 하나로 한다.
- **그래프 데이터**: `Receiver.plot_t/plot_v`(deque, 최대 12000점 ≈ 13분)는 `Receiver.lock`으로 보호, `snapshot()`으로 복사본만 읽음.
- **종료 신호**: `threading.Event stop`. 창 닫기 → `LoggerApp.on_close`(기록 중이면 확인 후 저장) → stop + `root.destroy()`.
  Ctrl+C는 `signal.SIGINT` 핸들러가 stop만 세우고, 다음 `tick`에서 기록 저장 후 창을 닫는다.
  `main`의 `finally`에서도 `recorder.stop()`을 한 번 더 호출(남아 있으면 저장).
- **주기 통계가 두 종류**: `Receiver.recent`(화면용 최근 50개, 기록 여부 무관)와 `Recorder`의 누적 통계(파일별 요약).
- 창 모드는 **GUI를 먼저 만들고 첫 렌더링을 끝낸 뒤 수신 스레드를 시작**한다.
  matplotlib 초기화가 GIL을 잡아 초기 타임스탬프가 ~25 ms 밀리던 문제 때문.
- 이전 버전은 matplotlib 창(`plt.show`) + matplotlib Slider였다. 접미사 입력 칸을 넣으면서 Tk 창으로 바꿨다
  (matplotlib TextBox는 한글 IME 입력이 제대로 안 됨).

### 사이클 조립 규칙 (`Receiver.process_line`)
- 정규식 `^S(\d),NT,([+-]?\d+(\.\d+)?)$` (공백 허용, 대소문자 무시).
- S1이 오면 새 사이클 시작, S2(`last_ch`)가 오면 사이클 확정 → `recorder.write`(기록 중일 때만 실제 기록).
- 같은 채널이 사이클 안에서 다시 오면(줄 누락) 이전 사이클을 먼저 처리 → 빈칸 있는 행이 생김.
- **동기화(`synced`)**: 시작/재연결 직후 S2부터 받으면 첫 S1까지 버린다. 안 그러면 S2만 있는
  반쪽 행이 생겨 첫 주기가 ~33 ms로 왜곡된다(실제로 발생했던 버그).
- 시작/재연결 직후 **첫 줄 조각은 버린다**(줄 중간부터 받기 때문).
- [기록 종료] 시점에 조립 중이던 사이클은 그 파일에 들어가지 않는다.
- 줄 구분자는 CR / LF / CRLF 모두 허용(실제 장비는 CRLF).

### 안정성 기능
- **자동 재연결**: `SerialException/OSError` → 1초마다 `open_port()` 재시도. 기록 중이었다면 **같은 CSV에 이어서** 기록.
  끊긴 시간은 다음 행의 `Interval_ms`에 그대로 남는다. 실제 USB 분리로 동작 확인됨(7~16 s 끊김 후 정상 이어짐).
- **시작 시** 포트를 못 열면 재시도하지 않고 즉시 종료(설정 오류일 가능성이 높으므로).
- **수신 없음 경고**: 2초(`NO_DATA_SEC`) 이상 유효한 줄이 없으면 콘솔 + 창 상태줄(빨강)에 경고, 재개 시 알림.
- **주기 통계**는 누적 합/최소/최대만 유지(메모리 일정). 2초 초과 간격은 통계에서 제외하고 `gaps`로 따로 센다.
- 형식 불일치 경고는 처음 10개 + 이후 100개마다만 출력.

### 그래프
- Torque와 RPM은 단위·스케일이 달라 **한 축에 겹치지 않고** 공유 x축 2단으로 그린다(이중 y축 금지).
- 색: Torque `#2a78d6`(파랑), RPM `#eb6834`(주황), 기록 표시 `#d03b3b`(빨강). data_viewer.py와 동일 팔레트.
- x축은 프로그램 시작부터의 경과 초. 기록 구간은 `rec_segments`([시작, 끝]) 목록으로 기억해 매 tick `axvspan`으로 다시 그림.
- 시간축 폭은 `ttk.Scale`이 `log10` 값으로 동작(5~600 s). 선형이면 자주 쓰는 10~60 s가 왼쪽 끝에 몰려 조작이 어렵다.
- 처음에는 0 s부터 오른쪽으로 채워 나가고, 폭을 넘으면 스크롤. y축은 보이는 데이터 범위 + 10% 여백(최소 ±0.5).
- 한글 폰트: `Malgun Gothic`. `SetProcessDpiAwareness(1)`로 고해상도에서 선명하게.

## 6. 환경

- Windows 11, **Anaconda base (Python 3.11)**. VS Code는 `.vscode/settings.json`에서 Conda를 기본 환경 관리자로 지정.
  위 실행 예의 `python`은 base가 활성화된 터미널 기준이다.
- 의존성: `requirements.txt` (pyserial, matplotlib>=3.7; numpy·tkinter는 Anaconda에 포함).
  base에 pyserial이 없으면 `python -m pip install --user pyserial`로 설치한다
  (base가 `C:\ProgramData`에 있으면 관리자 권한 없이는 base에 직접 설치할 수 없다).
- `run_logger.bat` / `run_viewer.bat`은 Python을 **Anaconda base(`%ProgramData%\anaconda3` → `%USERPROFILE%\anaconda3`) → `.venv`** 순으로 찾는다.
  `.venv`는 예전 python.org + venv 환경용으로 남긴 대체 경로다. venv의 `python.exe`는 **런처**라서 실제 Python이 자식 프로세스로 뜬다
  → 테스트 시 PID로 창을 찾으면 안 된다.
- 콘솔 출력이 한글이므로 stdout을 리다이렉트할 때는 `PYTHONIOENCODING=utf-8`.
- `.bat` 파일은 **ASCII + CRLF**여야 한다. UTF-8 한글/LF로 저장하면 cmd가 줄을 깨뜨려 실행에 실패한다.
  `.gitattributes`에 `*.bat text eol=crlf`.

## 7. 테스트 방법

- **단위 테스트(장비 불필요)**: `csv_name`/`clean_suffix`(빈칸, 공백만, 한글, 금지문자, 끝 마침표, 괄호),
  `unique_path` 중복 시 `(2)`, `(3)`, `Recorder`: 대기 중 write 무시 / 시작 전 사이클 무시 / 끊김 통계 / 두 번째 기록의 첫 Interval 빈칸 /
  기존 파일 덮어쓰기 거부(`FileExistsError`).
- **창 테스트(실제 COM4)**: 테스트 스크립트 안에서 `Recorder`, `Receiver`, `LoggerApp`을 직접 만들고
  `suffix_entry.insert`, `start_btn.invoke()`, `stop_btn.invoke()`, `on_close()`(X 버튼과 동일)를 순서대로 호출.
  확인창은 `app.messagebox.askyesno`를 가짜로 바꿔 '예'. `root.update()` 루프로 진행하고 `PIL.ImageGrab`으로 창 캡처.
  - 캡처 전에 반드시 `root.attributes("-topmost", True)` — 안 하면 앞에 있는 다른 창(브라우저 등)이 찍힌다.
  - 예전 방식(`EnumWindows`로 제목 `HA-130TR*` 창을 찾아 `WM_CLOSE`)은 **이미 열려 있는 data_viewer 창까지 닫을 수 있으므로**
    쓰지 말 것(뷰어 제목도 `HA-130TR 데이터 뷰어`).
- **콘솔 모드**: Git Bash의 `timeout -s INT`는 Windows 프로그램에 Ctrl+C를 전달하지 못한다 → 프로세스가 COM4를 계속 잡는다.
  끝낼 때는 명령줄로 해당 프로세스만 찾아 종료(행마다 flush라 데이터는 남음).
- **재연결/수신없음**: 실제 Serial을 감싼 가짜 객체로 "3초 정상 → 3초 무응답 → 예외", `open_port`는 2회 실패 후 성공시켜
  `read_loop(open_port, ser, rx, stop)`를 직접 실행.
- 검증하지 못한 것: 창 모드 Ctrl+C, 실제 마우스 클릭/한글 IME 입력(코드로 문자열을 넣어 확인), 수 시간 이상 장시간 실행.

## 8. 앞으로 손댈 때 참고

- 채널/열 정의는 `Receiver.channels`와 `Recorder` 생성 시 열 이름(`main`) 두 곳. 순서가 같아야 한다.
  채널을 늘리면 `last_ch`(사이클 마지막 채널)도 맞출 것.
- CSV 형식을 바꾸면 **data_viewer.py의 `LogData`/`series_label`도 같이** 맞춰야 한다. 파일명 접미사는 뷰어와 무관(`*.csv` 전부 읽음).
- 간격 2000 ms 기준(`NO_DATA_SEC`)은 data_viewer.py의 `GAP_MS`와 같은 값이어야 한다.
- 후보 기능(미구현): Command mode 폴링, 일정 시간/행마다 파일 분할(Excel 행 제한 대응), 기록 중 메모 삽입.
