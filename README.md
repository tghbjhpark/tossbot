# Toss Securities Automatic Multi-Strategy Trading Bot

토스증권(Toss Securities) 공식 OpenAPI를 사용하여 국내주식 및 해외주식 매매를 자동화하는 트레이딩 봇입니다. 

기존 Firebase 의존성을 모두 걷어내고 로컬 **SQLite** 데이터베이스를 사용하여 매칭 이력 관리 및 미체결 추적을 무중단으로 안전하게 처리하며, Docker 컨테이너 환경으로 가볍고 신속하게 배포할 수 있습니다. 특히 **멀티 전략 아키텍처**를 지원하여 종목별로 서로 다른 전략을 구사할 수 있습니다.

---

## 📈 지원 매매 전략

이 봇은 종목별로 개별적인 매매 전략을 설정할 수 있는 플러그형 구조를 채택하고 있습니다.

### 1. 그리드 분할 매수 전략 (`GRID`)
- **하락 그리드 조건**: 현재 봇에 미체결 매수 주문이 없는 상태에서, 등록되어 있는 매도 대기(incomplete) 주문 중 **가장 낮은 평단가(또는 직전 매수가) 대비 설정된 그리드 간격(`grid_interval`)만큼 하락**한 시점에 신규 그리드 매수를 진입합니다. (예: `grid_interval: 0.005` 설정 시 기준가 대비 0.5% 하락 시 추가 매수)
- **상승기 그리드 복원 (`fill_grid_on_rise`)**: 하락 쿨다운 등으로 매수를 건너뛰었거나 급상승 시, 현재가 기준 가상 익절 목표가(`current_price * (1 + yield_target)`)의 `+- grid_interval` 오차 범위 내에 등록된 기존 매도 주문이 하나도 없다면(격자 공백 감지), 즉시 추가 매수를 실행해 그리드 격자를 촘촘히 복원 채우기합니다.
- **수익 설정 (익절 목표가)**: 각각 분할 매수 체결된 진입 가격에 **목표 수익률(`yield_target`)을 1대1로 대응**시켜 개별 익절 가격(`buy_price * (1 + yield_target)`)을 계산하고 독자적인 매도 목표를 수립합니다. (예: `yield_target: 0.015` 설정 시 1.5% 수익 도달 시 익절)
- **매수 주문 취소**: 다음 턴(Tick)까지 매수가 체결되지 않으면 주문을 즉시 자동 취소하여 현재가 기준으로 그리드 포지션을 원활히 리로드합니다.

### 2. 가상 매도 대기 (Standby Sell) & 가격 트리거 (공통 기능)
- 매수가 체결되면 즉시 거래소에 매도 주문을 올리지 않고, DB 및 메모리 상에 가상 대기 상태(`isSynthetic`)로만 보관합니다.
- 현재 시장가가 목표 가격(`buy_price * (1 + yield_target)`) 이상으로 도달할 때에만 **실시간으로 실제 매도 주문을 전송**합니다.
- 이를 통해 **한국 주식의 양방향 주문 제한(반대 포지션 미체결 에러)**을 우회하고, **미국 주식 마켓 교체기(데이마켓 ↔ 프리마켓)의 매도 주문 강제 취소/유실 문제**를 해결합니다.
- 매도 주문 역시 1턴 내에 즉시 체결되지 않을 경우 취소 후 대기 상태로 환원되며, 부분 체결 발생 시 체결분만 우선 정산하고 잔량은 신규 매도 대기로 스플릿 관리합니다.

### 3. 연속 매수 쿨다운 (Consecutive Buy Cooldown - 공통 기능)
- 매도 거래 없이 하락장에서 연속적으로 매수만 발생하는 경우, 예수금 고갈을 방지하기 위해 지정 횟수(`max_consecutive_buys`) 도달 시 지정된 시간(`cooldown_minutes` 분) 동안 추가 매수 진입을 일시적으로 중단(쿨다운)합니다.
- 쿨다운 작동 중이라도 **매도가 1주라도 체결되면** 즉시 연속 매수 카운터가 `0`으로 초기화되고 제한이 해제됩니다.
- 설정된 쿨다운 시간이 경과하면 자동으로 카운터가 리셋되고 정상 상태로 복원됩니다.

---

## 🏗️ 시스템 아키텍처 및 폴더 구조

전략 패턴(Strategy Pattern)을 기반으로 구현되어 새로운 투자 전략을 추가하기 쉽습니다.

- `main.py`: 봇의 진입점. 스케줄러와 데이터베이스 및 API 클라이언트를 기동합니다.
- `trader.py`: 핵심 오케스트레이터 (`TradeBot`). 설정 파일을 지속 모니터링하며 각 종목별 전략 인스턴스를 동적으로 바인딩하고 폴링 루프를 구동합니다.
- `strategies/`:
  - `base.py`: 공통 클래스 `BaseStrategy`. Toss OpenAPI 통신 및 주문 매칭/체결 정산 공통 로직 관리.
  - `grid.py`: `GridStrategy`. 그리드 분할 매매 전략 로직 탑재.
  - `__init__.py`: 전략 클래스 로더 및 팩토리.

---

## 🛠️ Docker 사용 방법

### 1. 컨테이너 빌드
프로젝트 루트 디렉토리에서 아래 명령어로 Docker 이미지를 빌드합니다.
```bash
docker build -t toss-bot:latest .
```

### 2. 컨테이너 실행
호스트의 `config/` 디렉토리와 `data/` 디렉토리를 볼륨 마운트하여 컨테이너가 종료되어도 설정 및 거래 이력(DB)이 보존되도록 아래 명령어로 실행합니다.
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
봇이 동작하는 실시간 상황 및 주문 내역은 컨테이너 로그를 통해 확인할 수 있습니다.
```bash
docker logs -f toss-trading-bot
```

---

## ⚙️ 설정 가이드

### 1. `env` 파일 설정
토스증권 공식 개발자 센터에서 발급받은 API 정보를 입력합니다. (예제값 형태이며 실제 정보 기입 후 GitHub 업로드 시 유출에 주의해 주세요.)

```ini
TOSS_CLIENT_ID=your_id
TOSS_CLIENT_SECRET=your_secret
TOSS_ACCOUNT_SEQ=your_account_sequence_number
```

### 2. `config/ticker.json` 설정 및 필드 설명
매매 대상 종목 정보 및 그리드 설정을 제어합니다. 이 파일은 봇 구동 중 외부에서 수정하더라도 **봇 중단 없이 동적으로 자동 리로드**됩니다.

```json
[
  {
    "ticker": "TSLL",
    "strategy": "GRID",
    "market": "US",
    "buy_mode": "AMOUNT",
    "buy_qty": 1,
    "buy_amount": 20.0,
    "yield_target": 0.015,
    "grid_interval": 0.005,
    "enabled": true,
    "max_consecutive_buys": 5,
    "cooldown_minutes": 10,
    "fill_grid_on_rise": true
  }
]
```

#### 필드 명세
* **`ticker`** (String): 매매할 종목의 티커 혹은 단축 코드 (예: `TSLL`, `TQQQ`, `0195S0` 등).
* **`strategy`** (String, Optional): 해당 종목에 적용할 매매 전략. 기본값은 `"GRID"` 입니다. 향후 추가되는 전략명으로 지정할 수 있습니다.
* **`market`** (String): `US` (미국 주식) 또는 `KR` (한국 주식).
* **`buy_mode`** (String): 
  - `QTY` (수량 지정 매수): 정수 수량 기준으로 매수 주문 제출.
  - `AMOUNT` (금액 지정 매수): 소수점 금액 주문 형태로 매수 주문 제출. (미국 소수점 거래 및 국내 소수점 거래용)
* **`buy_qty`** (Integer): `buy_mode`가 `QTY`일 때 1회당 매수할 주식 수.
* **`buy_amount`** (Float): `buy_mode`가 `AMOUNT`일 때 1회당 매수할 한화/외화 금액.
* **`yield_target`** (Float): 목표 익절 수익률. (예: `0.015` = 1.5% 익절 목표)
* **`grid_interval`** (Float): 그리드 매수 간격 비율. (예: `0.005` = 직전 체결가 대비 0.5% 하락 시 추가 매수)
* **`enabled`** (Boolean): `true`일 때 거래가 정상 진행되며, `false`로 변경 시 즉시 매매 및 그리드 감지가 일시정지됩니다.
* **`max_consecutive_buys`** (Integer, Optional): 매도 없이 연속으로 매수 가능한 최대 횟수. (해당 필드가 없거나 설정하지 않으면 쿨다운 장치가 비활성화되어 기존처럼 계속 매수합니다.)
* **`cooldown_minutes`** (Integer, Optional): 연속 매수 제한 횟수 도달 시, 매수 감지를 중단할 대기 시간 (분 단위).
* **`fill_grid_on_rise`** (Boolean, Optional): 현재가 기준 목표 익절가 부근에 매도 물량이 없을 때, 상승 중에도 그리드 격자를 촘촘히 채우기 위한 신규 추격 매수 활성화 여부. (해당 필드가 없거나 설정하지 않으면 기본적으로 `true`로 동작합니다.)

---

## 📊 SQLite 데이터베이스 및 실현 손익 조회

데이터베이스는 호스트의 `data/toss_trade_bot.db` 경로에 저장됩니다.

### DB 테이블 구조
1. `pending_buy_orders`: 미체결 매수 주문 트래킹 테이블
2. `incomplete_orders`: 체결 완료된 매수에 대응하는 **가상 매도 대기(Standby)** 정보 테이블
3. `trades_history`: 실체 체결 매칭 역사 및 정산된 손익 관리 테이블

### 실현 손익 조회 명령어
터미널에서 SQLite CLI를 이용해 누적 실현 손익을 쉽게 조회할 수 있습니다.

#### 1. 전체 누적 실현 손익(Profit) 합계 조회
```bash
sqlite3 -column -header data/toss_trade_bot.db "SELECT SUM(profit) AS total_profit FROM trades_history WHERE status = 'COMPLETED';"
```

#### 2. 종목(Ticker)별 누적 실현 손익 및 누적 거래 횟수 통계 조회
```bash
sqlite3 -column -header data/toss_trade_bot.db "SELECT symbol, SUM(profit) AS total_profit, COUNT(*) AS trade_count FROM trades_history WHERE status = 'COMPLETED' GROUP BY symbol;"
```
