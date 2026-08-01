# Toss Securities Automatic Multi-Strategy Trading Bot

토스증권(Toss Securities) 공식 OpenAPI를 사용하여 국내주식 및 해외주식 매매를 자동화하는 트레이딩 봇입니다. 

로컬 **SQLite** 데이터베이스를 사용하여 매칭 이력 관리, 가상 매도 대기(Standby Sell), 세션 상태 및 체결 이력을 무중단으로 안전하게 처리하며, Docker 컨테이너 환경으로 가볍고 신속하게 배포할 수 있습니다. 특히 **멀티 전략 아키텍처**를 지원하여 종목별로 서로 다른 전략(`GRID`, `DCA`, `VR`)을 자유롭게 구사할 수 있습니다.

---

## 📈 지원 매매 전략

이 봇은 종목별로 개별적인 매매 전략을 설정할 수 있는 플러그형 아키텍처를 채택하고 있습니다.

### 1. 그리드 분할 매매 전략 (`GRID`)
* **하락 그리드 진입**: 미체결 매수 주문이 없는 상태에서, 기존 매도 대기 포지션 중 **가장 낮은 평단가(또는 직전 체결가) 대비 설정된 그리드 간격(`grid_interval`)만큼 하락**한 시점에 신규 그리드 매수를 진행합니다. (예: `grid_interval: 0.005` 설정 시 0.5% 하락 마다 추가 매수)
* **상승기 그리드 복원 (`fill_grid_on_rise`)**: 쿨다운 등으로 매수를 건너뛰었거나 주가 상승 시, 목표 익절가 부근에 매도 물량이 없다면 즉시 추격 매수를 실행해 그리드 격자를 촘촘히 채웁니다.
* **1대1 개별 익절 대응**: 분할 매수 체결건마다 **목표 수익률(`yield_target`)을 1대1로 대응**시켜 개별 익절가(`buy_price * (1 + yield_target)`)를 독립적으로 관리합니다.
* **연속 매수 제한 및 쿨다운**: 지정된 횟수(`max_consecutive_buys`) 연속 매수 시 일정 시간(`cooldown_minutes`) 매수를 일시 중단하며, 매도가 1주라도 체결되면 카운터가 리셋됩니다.
* **상단 포지션 손절 (`stop_loss_count`)**: 설정 시 정규장 시간 내 매도 예정가가 가장 높은 최상단 포지션부터 지정 개수만큼 시장가 손절을 실행합니다.

---

### 2. 라오어 무한매수법 응용 전략 (`DCA`)
정해진 시각(타임슬롯)마다 코스트 에버리징 매수를 진행하고, 동적 목표 수익률 달성 시 익절 청산하는 전략입니다.

* **정해진 타임슬롯 분할 매수**: 한국장(KST 10:00, 12:30, 15:00) 또는 미국장(KST 23:00, 01:00, 03:00) 지정 타임슬롯에 최대 회차(`max_session_buys`, 기본 40회)까지 분할 매수를 진행합니다.
* **동적 목표 수익률 (Dynamic Target Yield)**: 진행 회차가 절반($N/2$) 이하일 때는 기본 목표 수익률(`yield_target`, 예: 10%)을 유지하다가, 절반 초과 진행 시 회차가 늘어날수록 목표 수익률을 점진적으로 낮추어(Decay) 빠른 익절 청산을 유도합니다.
* **3/4 지점 30% 부분 손절 (Partial Cut) & 회차 연장**: 진행률 75%(3/4) 이상이고 수익률이 음수일 때, 현재 보유 수량의 30%를 시장가 부분 손절하고 최대 매수 회차를 20%(+8회) 연장하여 손실 리스크 감소 및 평단가 인하 기회를 확보합니다.
* **원본 체결 이력 보존 & 원가 동기화**: 부분 손절 발생 시 원본 매수 레코드(`dca_incomplete_orders`)를 덮어쓰거나 삭제하지 않고 100% 보존하며, DB 세션 상태에 손절 수량(`cut_quantity`) 및 차감 원가(`cut_total_cost`)를 누적 관리하여 추가 매수가 발생해도 오차 0%의 평단가를 유지합니다.
* **세션 완료 및 매수 이력 아카이빙 (`dca_session_buys_history`)**: 익절 청산 또는 최종 회차 도달 청산 시, 고유 세션 ID(`session_id`)를 부여하여 세션 동안의 모든 회차별 매수 상세 이력을 완료 이력 DB에 보존하고 세션을 초기화합니다.

---

### 3. 라오어 밸류 리밸런싱 전략 (`VR`)
포트폴리오 목표 평가액($V$)과 현금 풀($Pocket$)을 동적으로 관리하며 밴드 리밸런싱을 수행하는 전략입니다.

* **장중 실시간 밴드 감시 및 리밸런싱**: 장 운영 시간 동안 실시간으로 주식 평가액($E$)을 감시하며, 밴드 범위($V_{min} \sim V_{max}$, 기본 $\pm 15\%$)를 이탈할 때만 저가 매수 / 고가 이익실현 리밸런싱 주문을 실행합니다.
  * **과평가 ($E > V_{max}$)**: 초과분만큼 주식을 매도하여 이익을 실현하고 대금을 현금 풀($Pocket$)로 회수.
  * **저평가 ($E < V_{min}$)**: 부족분만큼 현금 풀($Pocket$)을 사용하여 주식을 분할 매수.
* **3가지 운용 모드**:
  * `ACCUMULATE` (적립식): 주기마다 적립금(`cycle_deposit`)을 현금 풀 및 $V$ 목표액에 투입.
  * `LUMP_SUM` (거치식): 추가 입출금 없이 초기 자금으로 지속 운용.
  * `WITHDRAWAL` (인출식): 주기마다 인출금(`cycle_withdrawal`)을 빼내어 생활비 등으로 활용.
* **10일 사이클 $V$ 목표가 성동적 성장**: 10일 주기마다 G-Factor ($G=10.0$) 또는 고정 성장률 기반으로 $V$ 목표가를 상향 조정.
* **목표 현금 비율 옵션 (`target_cash_ratio`)**: 주가 폭등 시 현금 비중 쏠림을 방지하기 위해, 사이클 갱신 시 기존 V 계산값과 [목표 현금 비율(예: 30%) 초과분을 흡수한 V 목표값] 중 더 큰 값(Max)을 선택하여 $V$를 대폭 상향.
* **1회성 추가금 옵션 (`one_time_deposit`)**: 일시적 여유 자금을 $V$ 조정 없이 포켓 현금 풀(`pocket_cash`)에 즉시 투입하고, 설정파일/메모리의 값을 `0.0`으로 자동 초기화하여 안전하게 현금을 융통.

---

### 4. 공통 핵심 기능 (Standby Sell & 쿨다운)
* **가상 매도 대기 (Standby Sell)**: 매수 체결 시 증권사에 매도 주문을 즉시 전송하지 않고 DB/메모리에 가상 대기 상태(`isSynthetic`)로 보관하다가, 목표가 도달 시에만 실시간 매도를 실행합니다. (국내 주식 양방향 주문 제한 우회 및 미국 주식 마켓 전환기 매도 주문 유실 문제 완벽 해결)
* **동적 설정 리로드 (Dynamic Reload)**: 봇 실행 중 `config/ticker.json` 파일을 수정하더라도 봇 재시작 없이 실시간으로 설정이 자동 적용됩니다.

---

## 🏗️ 프로젝트 구조

```text
TossTradeBot/
├── main.py                # 프로그램 진입점 (스케줄러 및 루프 실행)
├── trader.py              # 멀티 전략 오케스트레이터 (TradeBot)
├── config.py              # 설정 파일 동적 로더 및 환경 변수 관리
├── sqlite_manager.py      # SQLite 로컬 DB 관리자 (매칭, 세션, 이력 저장을 담당)
├── strategies/            # 매매 전략 모듈
│   ├── base.py            # BaseStrategy 공통 전략 상속 클래스
│   ├── grid.py            # GridStrategy (그리드 전략)
│   ├── dca.py             # DcaStrategy (무한매수 응용 전략)
│   ├── vr.py              # VrStrategy (밸류 리밸런싱 전략)
│   └── __init__.py        # 전략 팩토리 (Strategy Factory)
├── config/
│   └── ticker.json        # 종목별 상세 매매 전략 설정 파일
└── data/
    └── toss_trade_bot.db  # 로컬 SQLite 데이터베이스 파일
```

---

## 🛠️ Docker 사용 방법

### 1. 컨테이너 빌드
```bash
docker build -t toss-bot:latest .
```

### 2. 컨테이너 실행
```bash
docker run -d \
  --name toss-trading-bot \
  --restart always \
  --log-driver json-file \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  --env-file env \
  -v $(pwd)/config:/app/config \
  -v $(pwd)/data:/app/data \
  toss-bot:latest
```

### 3. 실시간 로그 모니터링
```bash
docker logs -f toss-trading-bot
```

---

## ⚙️ 설정 가이드 (`config/ticker.json`)

`config/ticker.json` 파일에서 각 종목별로 적용할 전략과 파라미터를 설정합니다.

### 설정 예시
```json
[
  {
    "ticker": "TSLL",
    "strategy": "GRID",
    "market": "US",
    "buy_mode": "AMOUNT",
    "buy_amount": 20.0,
    "yield_target": 0.015,
    "grid_interval": 0.005,
    "enabled": true,
    "max_consecutive_buys": 5,
    "cooldown_minutes": 10,
    "fill_grid_on_rise": true
  },
  {
    "ticker": "TQQQ",
    "strategy": "DCA",
    "market": "US",
    "buy_mode": "AMOUNT",
    "buy_amount": 20.0,
    "yield_target": 0.10,
    "max_session_buys": 40,
    "min_session_buys": 6,
    "min_sell_qty": 1.0,
    "enabled": true
  },
  {
    "ticker": "SOXL",
    "strategy": "VR",
    "market": "US",
    "buy_mode": "AMOUNT",
    "mode": "ACCUMULATE",
    "v_target": 500.0,
    "pocket_cash": 500.0,
    "band_rate": 0.15,
    "cycle_deposit": 100.0,
    "g_factor": 10.0,
    "cycle_days": 10,
    "target_cash_ratio": 0.30,
    "one_time_deposit": 0.0,
    "enabled": true
  }
]
```

### 상세 필드 명세

#### 공통 필드
* **`ticker`** (String): 매매할 종목 티커/단축코드 (예: `TSLL`, `TQQQ`, `SOXL`, `0195S0`).
* **`strategy`** (String): 전략 종류 (`GRID`, `DCA`, `VR`).
* **`market`** (String): 시장 구분 (`US` - 미국주식, `KR` - 한국주식).
* **`buy_mode`** (String): 매수 주문 방식 (`AMOUNT` - 금액 지정 소수점 매수, `QTY` - 온주 수량 지정 매수).
* **`buy_amount`** (Float): `buy_mode`가 `AMOUNT`일 때 1회당 매수 금액.
* **`buy_qty`** (Integer): `buy_mode`가 `QTY`일 때 1회당 매수 수량.
* **`yield_target`** (Float): 목표 익절 수익률 (예: `0.015` = 1.5%).
* **`enabled`** (Boolean): `true`일 때 정상 매매, `false` 설정 시 신규 매수는 중단하고 기존 보유분 매도/감시만 진행.

#### GRID 전략 전용 필드
* **`grid_interval`** (Float): 그리드 매수 간격 비율 (예: `0.005` = 0.5% 하락 시 추가 매수).
* **`max_consecutive_buys`** (Integer, Optional): 연속 매수 허용 최대 횟수.
* **`cooldown_minutes`** (Integer, Optional): 연속 매수 도달 시 대기(쿨다운) 시간(분).
* **`fill_grid_on_rise`** (Boolean, Optional): 상승 중 익절가 부근 공백 발생 시 그리드 추격 매수 여부 (기본값 `true`).
* **`stop_loss_count`** (Integer, Optional): 최상단 매도 포지션 손절 개수.

#### DCA 전략 전용 필드
* **`max_session_buys`** (Integer): 세션 내 최대 매수 회차 $N$ (기본값 `40`).
* **`min_session_buys`** (Integer, Optional): 청산 감지를 허용할 최소 매수 회차 (기본값 `6`).
* **`min_sell_qty`** (Float, Optional): 매도 실행을 허용할 최소 수량 (기본값 `1.0`).

#### VR 전략 전용 필드
* **`mode`** (String): 운용 모드 (`ACCUMULATE` - 적립식, `LUMP_SUM` - 거치식, `WITHDRAWAL` - 인출식).
* **`v_target`** (Float): 초기 포트폴리오 목표가 $V$.
* **`pocket_cash`** (Float): 초기 매수 대기 현금 잔고 $Pocket$.
* **`band_rate`** (Float): 밸류 밴드 비율 (기본값 `0.15` = 15%).
* **`cycle_deposit`** (Float): 주기마다 현금/V에 추가되는 적립금 (`ACCUMULATE` 모드용).
* **`cycle_withdrawal`** (Float): 주기마다 인출되는 금액 (`WITHDRAWAL` 모드용).
* **`g_factor`** (Float): V 목표가 상승 기울기 계수 (기본값 `10.0`).
* **`cycle_days`** (Integer): 리밸런싱 사이클 주기 거래일 수 (기본값 `10`).
* **`min_trade_amount`** (Float, VR 전용): 과도한 잦은 매매 및 수수료 낭비를 방지하기 위한 최소 거래 허들 금액 (기본값 `$10.0`).
* **`target_cash_ratio`** (Float, Optional): 목표 현금 비율 (예: `0.30` = 30%). 현금 과도 축적 시 V 목표가를 상향시켜 현금 비율을 자동 조절.
* **`one_time_deposit`** (Float, Optional): 1회성 여유 자금 추가금. 설정 시 현금 풀에 즉시 합산되고 `0.0`으로 자동 리셋.

---

## 📊 SQLite 데이터베이스 및 이력 관리

데이터베이스는 호스트의 `data/toss_trade_bot.db`에 안전하게 보존됩니다.

### 주요 테이블 구조
1. **`pending_buy_orders` / `dca_pending_buy_orders` / `vr_pending_buy_orders`**: 미체결 매수 주문 트래킹 테이블.
2. **`incomplete_orders` / `dca_incomplete_orders` / `vr_incomplete_orders`**: 매수 체결 완료된 거래별 가상 매도 대기(Standby) 및 보유 내역.
3. **`dca_session_state` / `vr_session_state`**: 전략별 세션 진행 상태 및 밸류/현금 변수 저장.
4. **`dca_session_buys_history`**: 완료된 DCA 세션의 회차별 매수 상세 이력 아카이빙 테이블 (`session_id` 매핑).
5. **`trades_history` / `dca_trades_history` / `vr_trades_history`**: 최종 완료된 매도 정산 역사 및 실현 손익 테이블.

### 실현 손익 조회 (SQLite CLI)

```bash
# DCA 세션별 실현 손익 및 총 매수 회차 조회
sqlite3 -column -header data/toss_trade_bot.db "SELECT session_id, symbol, profit, buy_count, sell_time FROM dca_trades_history WHERE status = 'COMPLETED';"

# 종목별 전체 누적 실현 손익 통계 조회
sqlite3 -column -header data/toss_trade_bot.db "SELECT symbol, SUM(profit) AS total_profit, COUNT(*) AS trade_count FROM dca_trades_history WHERE status = 'COMPLETED' GROUP BY symbol;"
```
