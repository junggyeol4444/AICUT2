# AICUT Studio

긴 생방송을 사건 단위로 이해하고 여러 YouTube 콘텐츠의 발견, 편집 기획, 렌더링 검수 및 퍼블리싱까지 연결하는 **AI 자율형 생방송 콘텐츠 제작 시스템의 인터랙티브 제품 프로토타입**입니다.

> 이 후속 작업은 최신 GitHub `origin/main` 위에 다시 구성되어 이전 PR과 동일 파일에서 발생하던 add/add 충돌을 제거했습니다. PR 전에는 `git fetch origin` 후 `npm run check:pr`로 미해결 인덱스, 충돌 마커 및 바이너리 diff를 검사하세요.

## 구현된 흐름

- 워크스페이스와 프로젝트 라이브러리
- 새 방송 파일 등록 및 채널·캘리브레이션 프로파일 선택
- `PARSING → UNDERSTANDING → DISCOVERING → EVALUATING → PLANNING → RENDERING → REVIEW_PENDING` 상태 표시
- 사건 기반 콘텐츠 후보 목록, 필터, 상세 판단 근거 및 사람 승인
- 원본 시점과 완성본 순서를 분리한 비선형 컷 타임라인
- 컷별 `KEEP / TRIM / CUT` 호흡, 화자, 역할, 자막 및 화면 효과 표시
- 제목 후보 3종, 설명·챕터·태그, 렌더링 사양과 필수 사람 검수 게이트
- YouTube 레퍼런스 제작 지식, 채널 캘리브레이션 지표 및 실시간 처리 로그
- `NO_CONTENT`를 포함한 정상 프로젝트 종료 상태

프로토타입 UI와 함께 Python 표준 라이브러리 기반 로컬 API 및 SQLite 영속화 계층을 제공합니다. 프로젝트, 사건, 후보, 에피소드, 비선형 컷 타임라인, 캘리브레이션과 작업 로그 스키마가 실제로 생성되고 API를 통해 상태가 저장됩니다. WhisperX/비전 모델/FFmpeg/YouTube API 실행기는 대용량 모델, 미디어 원본 및 사용자 자격 증명을 연결하는 다음 런타임 단계입니다.

로컬 작업 큐는 `ffprobe`로 실제 미디어의 길이, 해상도, 프레임레이트, 코덱과 오디오 트랙 수를 검사합니다. 멀티모달 분석기는 `tests/fixtures/analysis-manifest.json` 형식의 교환 매니페스트를 출력하면 되며, 런타임은 사건·언급·후보·에피소드·컷 전체를 단일 트랜잭션으로 반영합니다. 분석기가 연결되지 않은 경우에는 미디어 파싱을 완료한 뒤 `UNDERSTANDING` 상태에서 명시적으로 대기합니다.

Broadcast Understanding 전처리는 멀티 오디오 트랙을 각각 48kHz WAV로 분리하고, 캘리브레이션 프로파일이 제공한 간격으로 전체 영상의 프레임을 샘플링합니다. 1차 통과 창은 방송 시작부터 끝까지 빈틈없이 생성하고, 신호가 표시한 2차 정밀 구간은 겹침을 병합합니다. 외부 STT가 반환한 구간·단어 타임스탬프는 원본 범위를 검증한 후 트랙·화자와 함께 저장합니다.

STT 실행기는 WhisperX 호환 CLI를 인자 배열로 호출해 셸 주입을 방지하고, 멀티트랙 결과를 방송 시간순으로 병합합니다. 단어별 점수는 발화 신뢰도로 집계하며 화자 태그가 있으면 유지하고, 모델 실행 명령은 API dry-run으로 먼저 검토할 수 있습니다.

렌더러는 DB의 완성본 컷 순서를 읽어 원본 시각과 무관한 FFmpeg concat 그래프를 생성합니다. `CUT` 컷은 제외하고, 각 컷에 짧은 `afade`를 적용한 뒤 H.264/AAC MP4로 출력합니다. 오디오는 1차 EBU R128 측정 결과를 2차 렌더에 주입하는 `loudnorm` 2-pass 방식으로 전체 타임라인을 정규화합니다. 목표 LUFS, true peak와 loudness range는 외부에서 변경할 수 있습니다. 렌더 API는 기본적으로 측정 계획만 반환하는 dry-run이며 `execute: true`를 명시해야 실제 FFmpeg를 실행합니다.

패키징 단계는 제목 후보 3개, 설명, 시간순 챕터와 태그를 검증해 JSON/TXT 패키지로 저장하고 지정한 완성본 시점에서 고화질 썸네일 후보를 추출합니다. 업로드 큐는 렌더 완료와 사람 검수 승인을 모두 DB에서 확인하며, 자동 단계에서는 공개 업로드를 허용하지 않고 `PRIVATE` 또는 `UNLISTED`만 접수합니다.

업로드 실행기는 중복 실행을 차단하고 성공 시 YouTube 영상 ID를 기록합니다. 쿼터 초과는 임의의 24시간 후가 아니라 `America/Los_Angeles` 시간대의 다음 자정으로 계산해 `RETRY_QUEUED`에 저장합니다. 실제 업로드는 OAuth 구현체를 `UploadClient` 계약에 연결해야 하며, 자격 증명이 없는 기본 런타임은 명시적으로 실패합니다.

호흡 캘리브레이션은 사람이 라벨링한 `KEEP/TRIM/CUT` 샘플에서 무음 레벨, 무음 길이와 직전 텐션 후보를 전수 평가해 F1과 정확도가 가장 높은 조합을 채널 프로파일로 저장합니다. 화자 전환과 의미 있는 반응은 기계적인 무음 제거보다 항상 우선하며, 측정하지 않은 임계값을 운영 프로파일에 넣지 않습니다. 입력 계약 예시는 `tests/fixtures/pacing-samples.json`에 있습니다.

Source-to-Output 학습기는 원본 길이와 완성본 컷 타임라인을 대조해 선택·제거된 원본 구간, 시간 역순 재배치, 반복 사용과 시각적 강조를 추출합니다. 선택 구간은 합집합으로 계산해 반복 컷이 선택률을 부풀리지 않으며, 결과를 원본↔완성본 학습 쌍으로 영속화합니다.

성과 학습기는 자기 채널에서 수집한 조회·반응·CTR·평균 시청 비율·시점별 유지율을 에피소드 스냅샷으로 보존합니다. 초반 이탈과 유지율 상승 판단값은 코드에 두지 않고 채널 프로파일에서 받아, 근거 시점·변화량·개선 제안으로 기록합니다.

## 실행

```bash
npm install
npm start
```

브라우저에서 `http://localhost:4173`을 여세요.

다른 터미널에서 로컬 API를 실행하면 UI가 자동으로 데모 데이터 모드에서 SQLite 런타임 모드로 전환됩니다.

```bash
npm run api
```

프로덕션 번들을 API 서버에서 함께 제공하려면 다음 순서로 실행합니다.

```bash
npm run build
npm run serve
```

브라우저에서 `http://127.0.0.1:8787`을 여세요. 데이터베이스 경로는 `AICUT_DB=/path/to/aicut.db`로 변경할 수 있습니다.

## 테스트 및 빌드

```bash
npm test
npm run build
```

## 주요 파일

- `src/main.js` — 전체 화면, 모달, 상태 전이 및 상호작용
- `src/data.js` — 프로젝트·사건·후보·타임라인·학습 지식 데이터 계약
- `src/styles.css` — 데스크톱 스튜디오 UI 디자인 시스템
- `src/api.js` — 로컬 런타임 API 클라이언트와 오프라인 폴백
- `backend/schema.sql` — 제약 조건과 인덱스를 포함한 SQLite 스키마
- `backend/database.py` — 프로젝트·후보·타임라인·검수 저장소
- `backend/server.py` — JSON API 및 프로덕션 정적 파일 서버
- `backend/media.py` — ffprobe 미디어 검사와 정규화
- `backend/pipeline.py` — 중복 실행 방지 로컬 작업 큐와 실패 상태 처리
- `backend/understanding.py` — 멀티트랙·프레임 전처리, 2단계 스캔 계획 및 STT 계약 검증
- `backend/stt.py` — WhisperX 호환 멀티트랙 STT 실행, 단어 타임스탬프·화자 정규화
- `backend/render.py` — 비선형 컷, 고정 crop 줌, 컷별 afade 및 concat 렌더 계획
- `backend/package.py` — 메타데이터 패키지, 챕터 검증 및 썸네일 후보 추출
- `backend/upload.py` — 검수된 비공개 업로드 실행, 중복 방지 및 PT 자정 쿼터 재시도
- `backend/calibration.py` — 사람 라벨 기반 호흡 파라미터 스윕과 정밀도·재현율 평가
- `backend/learning.py` — 원본↔완성본 선택·제거·재배치·반복·강조 분석
- `backend/performance.py` — 자기 채널 성과 검증, 유지율 변화 및 전략 개선 근거 추출
- `tests/data.test.js` — 파이프라인과 핵심 데이터 불변 조건 검사
- `tests/test_database.py` — SQLite 트랜잭션, 제약 조건 및 비선형 타임라인 검사
