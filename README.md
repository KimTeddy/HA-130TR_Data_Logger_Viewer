# HA-130TR Data Logger & Viewer

**[한국어](#한국어) · [English](#english)**

해암이엔지 **HA-130TR** 토크/RPM 인디케이터의 RS232C 출력을 실시간으로 그래프에 띄우고 CSV로 기록한 뒤, 구간을 골라 분석하는 Windows용 도구입니다.

---

## 한국어

### 구성

| 파일 | 역할 |
|---|---|
| `ha130tr_logger.py` | 실시간 Torque/RPM 그래프 · **[기록 시작]~[기록 종료]** 구간을 CSV로 저장 |
| `data_viewer.py` | CSV 목록에서 파일 선택 · **드래그로 구간 선택 → 즉시 통계** |
| `check_rate.py` | 장비 송신 주기와 통신 설정 점검 |

### 주요 기능

- **로거**: 켜 둔 채 여러 번 기록할 수 있고 기록마다 새 파일이 생깁니다. 파일명 접미사를 지정할 수 있고, 행마다 바로 저장해 비정상 종료돼도 데이터가 남습니다. 연결이 끊기면 자동으로 다시 연결합니다.
- **뷰어**: 구간의 평균, 표준편차, 최소·최대, 피크-피크, RMS, 변화량, 샘플링 주기를 계산합니다. 선택 구간 확대, 결과 복사(Excel에 바로 붙여넣기), 선택 구간 CSV 저장을 지원합니다.

### 빠른 시작

```bash
pip install -r requirements.txt     # pyserial, matplotlib (Anaconda base에서 바로 동작)
```

| 실행 | 명령 |
|---|---|
| 로거 | `run_logger.bat` 더블클릭 또는 `python ha130tr_logger.py --port COM4` |
| 뷰어 | `run_viewer.bat` 더블클릭 또는 `python data_viewer.py` |
| 포트 목록 | `python ha130tr_logger.py --list` |

> 예제 데이터 [`data/HA130TR_20261116_181509.csv`](data/HA130TR_20261116_181509.csv)가 들어 있어서, 장비 없이도 뷰어를 바로 써 볼 수 있습니다.

### 장비 설정

RS232C **9600 bps, 8N1**, F-09 ID = `00` (Stream mode). 단위는 장비가 보내지 않으므로 F-13 설정에 맞춰 `--unit`을 지정합니다 (기본 `kgf`).

### CSV 형식

```csv
Timestamp,Interval_ms,Torque_kgf,RPM
2026-11-16 18:15:10.459,,-2.5,5.0
2026-11-16 18:15:10.523,64,-2.5,5.0
```

UTF-8 BOM이라 Excel에서 한글이 깨지지 않습니다. 수신 주기는 약 66 ms(≈15 Hz)입니다.

### 문서

[로거](docs/ha130tr_logger.md) · [뷰어](docs/data_viewer.md) · [주기 측정](docs/check_rate.md)

---

## English

A Windows toolkit for the HAIAM **HA-130TR** torque/RPM indicator: plot its RS232C output live, record it to CSV, and analyze any selected range.

### Contents

| File | Purpose |
|---|---|
| `ha130tr_logger.py` | Live Torque/RPM plot · saves the **[Start]–[Stop] recording** range to CSV |
| `data_viewer.py` | Pick a file from the CSV list · **drag to select a range → instant statistics** |
| `check_rate.py` | Check the device's transmit rate and serial settings |

### Features

- **Logger**: record as many times as you like without restarting, each recording to a new file. Supports a filename suffix and flushes every row, so data survives a crash. Reconnects automatically if the link drops.
- **Viewer**: computes mean, std dev, min/max, peak-to-peak, RMS, change and sampling rate for the selected range. Supports zoom to selection, copy results (pastes straight into Excel) and saving the selection as CSV.

### Quick start

```bash
pip install -r requirements.txt     # pyserial, matplotlib (works out of the box on Anaconda base)
```

| Run | Command |
|---|---|
| Logger | double-click `run_logger.bat`, or `python ha130tr_logger.py --port COM4` |
| Viewer | double-click `run_viewer.bat`, or `python data_viewer.py` |
| List ports | `python ha130tr_logger.py --list` |

> Sample data [`data/HA130TR_20261116_181509.csv`](data/HA130TR_20261116_181509.csv) is included, so you can try the viewer without the device.

### Device settings

RS232C **9600 bps, 8N1**, F-09 ID = `00` (Stream mode). The device does not send units, so pass `--unit` to match its F-13 setting (default `kgf`).

### CSV format

```csv
Timestamp,Interval_ms,Torque_kgf,RPM
2026-11-16 18:15:10.459,,-2.5,5.0
2026-11-16 18:15:10.523,64,-2.5,5.0
```

Saved as UTF-8 with BOM so Excel opens it cleanly. Samples arrive about every 66 ms (≈15 Hz).

### Docs (Korean)

[Logger](docs/ha130tr_logger.md) · [Viewer](docs/data_viewer.md) · [Rate check](docs/check_rate.md)
