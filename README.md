# 업앤다운 ▲▼

코스피 보통주 중 **다음 거래일에 오를 확률이 높게 계산된 10개 종목**을 매일 보여주는 프로그램입니다.
평일 장 마감 후(16시 20분경) 자동으로 계산되고, 결과는 휴대폰에서 웹페이지로 확인합니다.
PC를 켜 둘 필요가 없고, 비용도 들지 않습니다 (GitHub 무료 기능 사용).

---

## 처음 한 번만 하는 설정 (PC에서 약 10분)

### 1. GitHub 가입
[github.com](https://github.com)에서 무료로 가입합니다.

### 2. 저장소 만들기
1. 오른쪽 위 **+** → **New repository**
2. Repository name: `upanddown`
3. **Public** 선택 (무료로 웹페이지를 쓰려면 공개 저장소여야 합니다)
4. **Create repository**

### 3. 파일 올리기
1. 새 저장소 화면에서 **uploading an existing file** 링크를 누릅니다.
2. 압축을 푼 `upanddown` 폴더 **안에 있는 것 전부**를 끌어다 놓습니다.
   - `.github` 폴더, `docs` 폴더, `upanddown.py`, `requirements.txt`, `README.md`
3. 아래 **Commit changes**를 누릅니다.

> **맥 사용자:** `.github`는 숨김 폴더라 Finder에서 안 보입니다. Finder에서 `Cmd + Shift + .` 을 누르면 보여요.
>
> `.github` 폴더가 그래도 안 올라갔다면: **Add file → Create new file** → 파일 이름 칸에
> `.github/workflows/daily.yml` 입력 → `daily.yml` 내용을 그대로 붙여넣기 → **Commit changes**

### 4. 자동 실행 권한 주기
**Settings → Actions → General** → 맨 아래 **Workflow permissions**에서
**Read and write permissions** 선택 → **Save**

### 5. 웹페이지 켜기
**Settings → Pages** → **Source**를 **GitHub Actions**로 선택

### 6. 첫 실행
1. 저장소 위쪽 **Actions** 탭 → 왼쪽 **업앤다운 매일 업데이트**
2. 오른쪽 **Run workflow** → 초록 **Run workflow** 버튼
3. 5~10분 뒤 초록 체크(✓)가 뜨면 완료

### 7. 휴대폰에서 보기
`https://내아이디.github.io/upanddown/` 주소를 휴대폰 브라우저로 엽니다.
- 아이폰(Safari): 공유 버튼 → **홈 화면에 추가**
- 안드로이드(Chrome): ⋮ 메뉴 → **홈 화면에 추가**

이제 앱처럼 아이콘을 눌러 확인하면 됩니다.

---

## 화면에 나오는 것

| 항목 | 의미 |
|---|---|
| **TOP 10 확률** | 다음 거래일 종가가 오늘 종가보다 높을 확률 (모델 추정치를 최근 실적으로 보정한 값) |
| **이 숫자, 얼마나 믿을 수 있나요?** | 최근 120거래일 동안 이 방식으로 뽑았다면 실제로 몇 %가 올랐는지 |
| **지난 추천 성적** | 실제로 추천했던 종목이 다음날 몇 개 올랐는지 매일 자동 채점 |

**꼭 알아두세요:** 하루 주가 등락은 예측이 매우 어렵습니다. 정직하게 계산하면 TOP 10의 확률도
대부분 50%대 초중반에 머뭅니다. "지난 추천 성적"이 전체 평균보다 꾸준히 높은지 몇 주 지켜본 뒤
참고 여부를 판단하세요. 투자 권유가 아니며, 매매 판단과 책임은 본인에게 있습니다.

---

## 어떻게 계산하나요?

1. 네이버 금융에서 코스피 종목과 최근 약 600거래일 시세를 받습니다.
2. 우선주·ETF·스팩, 거래가 적은 종목(20일 평균 거래대금 10억 원 미만)은 뺍니다.
3. 최근 수익률, 이동평균과의 거리, 거래량 변화, 변동성, RSI, 시장 전체 흐름 등 34개 지표를 계산합니다.
4. "다음날 올랐는가"를 정답으로 Gradient Boosting 모델을 학습합니다.
5. 최근 120거래일로 검증한 결과를 이용해 확률이 과장되지 않도록 보정합니다.

## 설정 바꾸기
`upanddown.py` 맨 위의 값을 고치면 됩니다 (GitHub에서 파일을 열고 ✏️ 연필 아이콘).
- `TOP_N = 10` — 보여줄 종목 수
- `MIN_TRADING_VALUE = 1e9` — 거래대금 하한 (1e9 = 10억 원)
- `TEST_DAYS = 120` — 검증 기간

## 문제가 생기면
- **Actions에 빨간 X가 떴을 때:** 해당 실행을 눌러 빨간 단계의 로그를 확인하세요.
  네이버 금융 사이트 구조가 바뀌면 데이터 수신이 실패할 수 있습니다. 로그 내용을 Claude에게 보여주면 고칠 수 있어요.
- **자동 실행이 멈췄을 때:** GitHub는 오래 활동이 없는 저장소의 예약 실행을 멈추기도 합니다.
  Actions 탭에서 **Enable workflow**를 누르면 다시 동작합니다.
- **공휴일:** 실행은 되지만 새 시세가 없으면 바로 종료합니다.

## PC에서 직접 실행 (선택)
```bash
pip install -r requirements.txt
python upanddown.py --out docs          # 실제 데이터
python upanddown.py --out docs --demo   # 인터넷 없이 가상 데이터로 시험
```
