# AICUT Studio

긴 생방송을 사건 단위로 이해하고 여러 YouTube 콘텐츠의 발견, 편집 기획, 렌더링 검수 및 퍼블리싱까지 연결하는 **AI 자율형 생방송 콘텐츠 제작 시스템의 인터랙티브 제품 프로토타입**입니다.

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

로컬 작업 큐는 `ffprobe → 전처리 → 1차/2차 스캔 계획 → 오디오/비전 특징 → WhisperX → 분석 결과 반영`을 하나의 체크포인트 기반 작업으로 연결합니다. 오디오 PCM 특징과 FFmpeg 비전 특징은 원본 시간 범위를 검증해 SQLite에 저장됩니다. STT·오디오·비전은 `timeline` 배열 하나로 원본 시작·종료 시각에 따라 안정적으로 정렬되어 Producer에 전달됩니다. 각 단계 결과, 입력 해시, 체크포인트 버전, 시도 횟수와 실패 원인은 SQLite에 보존됩니다. 원본 파일의 경로·크기·수정 시각·표본 해시 또는 실행 옵션이 변경되면 오래된 체크포인트를 무효화하고, 입력이 같을 때만 완료 단계를 재사용해 취소 후 재개합니다. 체크포인트 JSON이 손상되었거나 전처리 체크포인트가 가리키는 중간 파일이 사라진 경우에도 해당 단계를 자동으로 무효화해 다시 생성합니다. 전처리와 STT는 실행 옵션을 제공했을 때만 수행하므로 외부 모델이 아직 설치되지 않은 환경에서도 안전하게 사용할 수 있습니다. 멀티모달 분석기는 `tests/fixtures/analysis-manifest.json` 형식의 교환 매니페스트를 출력하면 되며, 분석기가 연결되지 않은 경우에는 앞 단계 완료 후 `UNDERSTANDING`에서 명시적으로 대기합니다.

`POST /api/projects/{project_id}/run`은 `resume`, `manifest_path`와 `options`를 받습니다. `options`에는 `preprocess`, `output_directory`, `disk_check`, `disk_required_bytes`, `disk_reserve_bytes`, `retry_policy`, `analysis_chunk_sec`, `frame_interval_sec`, `coarse_window_sec`, `precision_ranges`, `precision_policy`, `precision_analysis`, `precision_audio_window_sec`, `precision_vision_interval_sec`, `audio_analysis`, `audio_window_sec`, `vision_analysis`, `vision_interval_sec`, `stt_executable`, `audio_paths`, `language`, `producer_executable`을 지정할 수 있습니다. `disk_check`를 사용하면 장시간 작업을 시작하기 전에 출력 볼륨의 실제 여유 공간에서 예약 공간을 제외해 검사하고, 부족하면 `DISK_CHECK` 단계에서 즉시 중단합니다. `retry_policy`는 단계별 `max_attempts`와 `backoff_sec`를 지정하며, 각 시도 횟수와 마지막 오류를 체크포인트에 보존하고 대기 중 취소에도 즉시 반응합니다. `analysis_chunk_sec`을 지정하면 긴 원본의 오디오·비전 분석을 원본 시간 범위별 체크포인트로 분할하고, 실패나 앱 재실행 후 완료된 구간은 건너뛴 채 실패한 구간부터 이어서 처리합니다. 프로젝트에 캘리브레이션 프로파일이 연결되어 있으면 `params.pipeline_options`를 실행 기본값으로 적용하고 API 옵션으로 필요한 값만 덮어쓰며, 프로파일 측정 버전도 입력 해시에 포함합니다. `precision_policy`는 채널 캘리브레이션에서 측정한 문맥 범위와 STT 신뢰도·오디오 변화·화면 전환 기준을 받아 정밀 구간 선택, 문맥 확장, 중첩 병합 및 선택 이유 기록을 수행합니다. `precision_analysis`를 활성화하면 선택된 범위만 더 촘촘한 오디오 창과 비전 샘플 간격으로 실제 재분석하고, `PRECISION_` 관찰로 1차 결과와 함께 저장합니다. 정밀 분석 밀도 값은 하드코딩하지 않으며 채널 캘리브레이션에서 반드시 전달해야 합니다. Producer 실행기는 `--input <분석 패키지 JSON> --output <결과 JSON>` 계약을 따르며, 결과의 ID 참조·시간 범위·점수·판단값·호흡 모드를 검증한 뒤에만 DB에 원자적으로 반영합니다. `GET /api/projects/{project_id}/job`으로 단계별 체크포인트와 현재 외부 프로세스 PID를 확인할 수 있습니다. `POST /api/projects/{project_id}/cancel`은 취소 플래그만 설정하지 않고 실행 중인 FFmpeg·WhisperX·Producer 자식 프로세스를 즉시 terminate하고, 종료되지 않으면 kill한 뒤 단계를 `CANCELLED`로 보존합니다.

렌더러는 DB의 완성본 컷 순서를 읽어 원본 시각과 무관한 FFmpeg concat 그래프를 생성합니다. `CUT` 컷은 제외하고, 각 컷에 짧은 `afade`를 적용한 뒤 H.264/AAC MP4로 출력합니다. 오디오는 1차 EBU R128 측정 결과를 2차 렌더에 주입하는 `loudnorm` 2-pass 방식으로 전체 타임라인을 정규화합니다. 목표 LUFS, true peak와 loudness range는 외부에서 변경할 수 있습니다. 렌더 API는 기본적으로 측정 계획만 반환하는 dry-run이며 `execute: true`를 명시해야 실제 FFmpeg를 실행합니다.

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
- `backend/render.py` — 비선형 컷, 고정 crop 줌, 컷별 afade 및 concat 렌더 계획
- `tests/data.test.js` — 파이프라인과 핵심 데이터 불변 조건 검사
- `tests/test_database.py` — SQLite 트랜잭션, 제약 조건 및 비선형 타임라인 검사
