# 기획안 ↔ 코드 대응표

통합 기획안 v1의 각 장이 어디에 구현되어 있는지, 그리고 문서가 명시적으로
금지하거나 수정한 사항이 코드의 어느 지점에서 강제되는지 적는다.

기획안 원문은 이제 리포 안에 있다 — 대응표만 있고 대응 대상이 없으면
조항별 검증이 불가능하기 때문이다.

- `docs/spec-v1.ko.md` — 통합 기획안 v1. 아래 장 번호는 전부 이 문서를 가리킨다.
- `docs/spec-original.ko.md` — v1이 "상위 기획 골격"으로 참조하는 원본(ai편집.txt).
  v1과 충돌하는 지점에서는 v1이 우선한다.
- `docs/merge.ko.md` — 같은 기획안을 구현한 다른 구현체(Codex, `legacy/`)와의
  조항별 대조 결과.

## 1~2장 — 설계 철학

| 원칙 | 강제 지점 |
|---|---|
| 2.1 영상 구조 하드코딩 금지 | `llm/prompts.py` `plan_structure`, `TB_EPISODE.planned_structure` |
| 2.2 콘텐츠 개수 하드코딩 금지 | `pipeline/runner.py` `_no_content()`, `State.NO_CONTENT` |
| 2.3 콘텐츠 종류 하드코딩 금지 | `SituationLabel`은 내부 신호. `Episode.target_type`은 자유 문자열 |
| 2.4 시간순 편집 강제 금지 | `TB_EDIT_TIMELINE.sequence_order` ≠ `source_start_sec`, `render/timeline.py` |
| 2.5 화면이 아니라 사건으로 분할 | `pipeline/discovery.py` — 사건 앵커 없는 후보는 폐기 |
| 2.6 길이 하드코딩 금지 | `planning._note_length_deviation()` → 리포트의 `length_deviations` |

## 4장 — YouTube Content Intelligence

- 4.1 좁게 시작 → `intelligence/reference.py: DEFAULT_QUERIES`
- 4.2 **사실관계**: 유지율/CTR은 자기 채널 한정 → `YouTubeClient.public_metrics()`(타 채널)
  와 `.analytics()` / `.audience_retention()`(`ids="channel==MINE"`)이 분리되어 있다
- 4.5 지식이지 규칙이 아님 → `ProductionKnowledge.summary_for_planner()`가 support/share와
  caveat을 함께 넘긴다
- 4.6 원본 미디어 미보관 → `tb_yt_reference`에 미디어 컬럼이 없다 (테스트로 고정)

## 5장 — Long-Term Broadcast Understanding

- 5.1 1차 전 구간 통과 → `understanding._first_pass()`가 0초부터 끝까지 창을 이어 붙인다
  (테스트: 창 사이에 틈이 없음을 검사)
- 5.1 2차 정밀 통과 → `_second_pass()`, 대상 선정 기준은 `scan.pass2_trigger`
- 5.2 멀티트랙 전제 → `media/probe.py: AudioTrack.role`, `needs_diarization`
- 5.3 상황 라벨 → `analysis/signals.py: label_situations()` (얼굴 신호 없으면 UNKNOWN)
- 5.4 장기 메모리 → `TB_EVENT` + `TB_EVENT_MENTION`, `_memory()`가 누적 문맥을 실어 나른다

## 6장 — Autonomous Content Discovery

- 6.2 사건 기준 분할 → `discovery.run()`이 사건 앵커를 요구
- 6.3 가치 판단 4종 → `Decision`, `evaluating.group_for_production()`이 결합 후보를 병합
- 6.4 경계 감지는 힌트일 뿐 → `analysis/signals.py: boundary_hints()`,
  `boundary_hints.min_hint_count` 이상 겹칠 때만 후보 지점으로 올린다

## 7~8장 — Planning / Retrieval

- 7장 구조 결정 → `producer.plan_structure()`
- 8.1 장면 검색 후 AI 검증 → `pipeline/retrieval.py` (BM25 + 사건/역할 가중) →
  `producer.select_scene()`이 전부 거절할 수도 있다
- 8.2 편집 계획과 렌더링의 완전 분리 → `render/editplan.py`

## 9장 — 스마트 페이싱

- 9.1 두 종류의 정적 → `analysis/pacing.py`
- 9.1 웃음/비명 → `analysis/vocalburst.py` (이 방송의 발화 레벨 대비 크고 단어 밀도 낮음).
  검출기가 없으면 텐션의 laughter 가중치를 0점 주는 대신 재분배한다
- 9.2 판정 신호 → `SilenceContext` (직전 텐션 / 화자 전환 / 화면 정지 / 컷 역할)
- 9.3 KEEP·TRIM·CUT → `PacingMode`, `TB_EDIT_TIMELINE.pacing_mode`
- 9.4 사람 완성본과 대조 → `calibration/metrics.py: score_pacing()`

## 10장 — 렌더링

- 10.1 렌더러는 판단하지 않음 → `Renderer.render()`는 `EditPlan`만 받는다
- 10.3 자막 스타일 외부화 → `config/subtitle_styles/*.json`
- 10.4-1 crop 표현식 오류 수정 → `render/ffmpeg.py: zoom_filter()`, `sendcmd_file()`.
  실측 결과: sendcmd로 crop `w`/`h`를 바꾸면 그래프 교착(ffmpeg 7.1). sendcmd는 팬 전용,
  배율 변화는 `segment_crop`. MVP 6 비교의 입력 하나가 이걸로 확정됐다
- 10.4-2 acrossfade 오용 수정 → `audio_edge_filter()` + `build_concat_command()`
- 10.4-3 2-pass 라우드니스 → `media/audio.py: measure_loudness()` → `build_final_command()`

## 11장 — 패키징 / 업로드

- 11.1 썸네일 후보 → `render/thumbnails.py` (템플릿 없음, 사람이 고른다)
- 11.3 사람 검수 게이트 → `State`에 REVIEW_PENDING을 거치지 않는 PUBLISHED 경로가 없다.
  `publishing.publish_approved()`는 승인되지 않은 에피소드에 `PermissionError`를 던진다
- 11.4 쿼터 사실관계 수정 → `intelligence/quota.py` (10,000 units / 1,600 units /
  PT 자정 리셋). "24시간 후 재시도"가 아니라 다음 PT 자정을 겨냥한다

## 12장 — 학습

- 12.1 자기 채널 한정 지표 → `performance.collect()`
- 12.3 A/B/C → `intelligence/reference.py`, `intelligence/source_output.py`,
  `pipeline/performance.py`

## 13~14장 — 데이터 모델 / 상태 기계

- 13.1 에피소드는 컷의 순서 있는 집합 → `db/schema.sql`의 `tb_episode`에
  start/end 컬럼이 없다
- 14장 → `pipeline/states.py`

## 16장 — 예외 처리

| 상황 | 처리 |
|---|---|
| 음성 미감지 | 발화 없는 사건도 `SceneIndex`에서 검색 가능 |
| 단일 주제 방송 | 억지 분할 없음 — 사건이 하나면 후보도 하나 |
| 제작 가치 없음 | `NO_CONTENT` 정상 종료 + 사유 리포트 |
| 쿼터 초과 | 로컬 보관 + `tb_upload_queue`에 PT 자정 기준 재시도 등록 |
| 렌더링 실패 | 편집 계획 보존, `aicut render <plan>`으로 렌더만 재실행 |
| 중간 단계 실패 | `aicut resume <project>` — 저장된 1차/2차 통과와 사건 그래프를 재사용하고 그 뒤만 다시 판단 |
| 화자 분리 실패 | `UNKNOWN` 태그로 진행, 화자 기반 연출만 비활성 (`speaker_reliability`) |
| 10시간 초과 원본 | `understanding._build_events()`가 `scan.long_source_chunk_sec` 단위로 접고 `_merge_events()`로 병합. 병합 패스가 빠뜨린 사건은 그대로 살아남는다 (자료 유실 금지) |

## 17장 — 캘리브레이션

- 17.2 데이터셋 → `aicut dataset` + `calibration/dataset.py`. 호흡 판정은 사람 완성본에서
  유도한다(`derive-silences`): 정적 양옆 발화가 완성본에도 있으면 그 간격이 얼마나
  살아남았는지로 KEEP/CUT을 가른다 — 12.3 B의 신호를 17.3의 정답지로 직접 쓰는 것
- 17.4 2단계 재생 → `calibration/harness.py`. 내장이라 운영자가 하네스를 짤 필요 없다.
  캐시된 신호를 재생하므로 조합마다 원본을 다시 디코딩하지 않는다
- 17.4 1단계 실측 → `aicut calibrate --init` (캐시된 RMS 분포에서 무음 레벨 시작값)
- 17.1 전면 외부화 → `config.py`. 프로파일에 없는 값을 읽으면 `ConfigError`
  (코드 기본값으로 대충 넘어가지 않는다)
- 17.5 미측정 값은 확정값이 아님 → `provisional` / `measured` 마킹,
  `touched_provisional()`가 리포트에 실린다, `--strict`는 아예 거부

## 18장 — 담당 경계

`llm/base.py: Producer`의 메서드 목록이 "AI가 담당"의 전부다.
그 밖의 모든 것(디코딩·DB·검색·렌더링·API·큐)은 프로그램이 한다.

## 15장 — UI

`aicut ui`가 15.1의 4단계 플로우를 그대로 제공한다.

| 화면 | 엔드포인트 | 문서 |
|---|---|---|
| 입력 패널 | `POST /api/projects`, `GET /api/profiles` | 15.2 |
| 진행 모니터 | `GET /api/jobs/<id>` (상태 + 실시간 로그) | 15.3 |
| 콘텐츠 후보 검토 | `GET/POST /api/projects/<id>/candidates` | 15.4 |
| 결과 패널 | `GET /api/projects/<id>/episodes`, `/api/episodes/<id>/plan`, `/thumbnail/<n>`, `/video`, `POST …/review`, `POST …/reveal` | 15.5 |

15.2가 요구하는 네 가지가 모두 화면에 있다.

- **드래그 앤 드롭** — 브라우저는 JavaScript에 파일의 **이름**만 준다. 경로는 주지
  않는다. 그래서 떨어뜨린 것을 그대로 보내면 파일명을 경로라고 우기는 셈이 된다
  (Codex 구현의 `file?.path`가 정확히 그것이었고, 그 UI로는 실제 파일 등록이
  불가능하다). 파일 관리자에서 끈 드롭은 보통 `file://` URI도 같이 실어 보내므로
  그게 있으면 그걸 쓰고, 없으면 무엇이 빠졌는지 화면에 적는다.
- **목표 길이 힌트** — 2.6대로 강제가 아니며 어긋나면 리포트의 `length_deviations`에 남는다.
- **채널 프로필**
- **캘리브레이션 프로파일 선택** — `GET /api/profiles`가 기본 프로파일(과 그 중
  무엇이 아직 추측값인지)과 측정된 프로파일 목록을 함께 준다. 17장은 프로파일을
  채널 단위로 못박으므로 선택은 제출 단위여야 한다. 프로젝트는 자기가 분석된
  프로파일 이름을 들고 있고, `context()`는 나중에 그것으로 다시 읽는다 — 끝난
  프로젝트를 다른 프로파일로 읽으면 그 결과를 만든 적 없는 임계값으로 보고하게 된다.

15.5의 결과 패널:

| 요구 | 어디 |
|---|---|
| 썸네일 프리뷰 | `GET /api/episodes/<id>/thumbnail/<n>` — 11.1의 후보 프레임, 템플릿 없이 사람이 고른다 |
| 제목 후보 3종 | `episodes` 응답의 `titles` |
| 재생 프리뷰 | `GET /api/episodes/<id>/video` — Range 지원. 없으면 스크럽할 때마다 처음부터 다시 받는다 |
| 편집 계획 열람 | `GET /api/episodes/<id>/plan` |
| 폴더 열기 | `POST /api/episodes/<id>/reveal` |
| 작업 리포트 | `GET /api/projects/<id>/report` |

미디어 라우트는 DB에 적힌 경로를 워크스페이스 안으로 가둔다. 경로는 이 파이프라인이
`workspace/<project>/…`에 직접 쓴 것이지만, 그래도 가두는 이유는 손으로 고친 행 하나가
프리뷰 라우트를 임의 파일 읽기로 바꾸지 못하게 하기 위해서다. 확장자도 허용 목록이다.
`reveal`은 프로그램을 실행하므로 루프백에서 온 요청만 받고, 셸 없이 고정 argv로 연다.

- 후보 화면은 AI의 결정과 **판단 근거**를 함께 띄우고, 동의/반대를 받아
  `TB_CONTENT_CANDIDATE.human_verdict`에 적재한다 (12.3 B 학습 데이터).
  같은 화면에서 원본 32장의 네 항목도 후보마다 받는다
  (`human_assessment`) — 동의/반대 하나로는 네 항목 중 어느 것이 틀렸는지
  알 수 없기 때문이다. 19장 MVP 3이 채점하는 것이 그 네 항목이다.
- 검수 API는 검수자 이름 없이는 승인을 거부한다 (11.3 — 누가 공개를 허락했는지 기록).
- 미측정 파라미터 경고를 모든 화면 상단에 띄운다 (17.5).
- 전달 방식은 로컬 HTTP 서버 + 정적 페이지다. 20.1이 적은 데스크톱 래퍼는
  `aicut/desktop.py`가 이 서버를 감싸는 방식으로 붙어 있다 — 실행 파일 하나를
  더블클릭하면 서버가 뜨고 브라우저가 열린다. PyInstaller 스펙은 `aicut.spec`,
  세 플랫폼에서 실제로 빌드·실행하는 것은 CI의 `desktop` 잡이다.

## 19장 — MVP 관문

19장은 각 MVP의 성공 기준을 통과해야 다음으로 넘어간다고 못박고, MVP 2에는
실측 항목까지 이름을 붙였다. 관문은 재는 것이 있어야 관문이다.

| 관문 | 기준 | 어디 | 상태 |
|---|---|---|---|
| MVP 1 | 분석 결과가 실제 제작 의도와 일치하는 비율 | `intelligence/reference.py: record_reference_verdict / reference_agreement`, `aicut gate mvp1` | 입력·집계 있음. 어느 비율이면 확보인지는 19장이 안 정함 |
| MVP 2 | 사람이 기억하는 주요 사건을 누락 없이 잡는가 / **실측**: 1차 통과 밀도별 사건 검출률과 처리 시간 | `calibration/mvp2.py`, `aicut gate mvp2 <project> --density --remembered` | 실측 명령 있음. 실제 방송에서 측정하는 것은 운영자 몫 |
| MVP 3 | 항목별 평가 4개 (원본 32장) | `pipeline/review.py: ASSESSMENT_ITEMS`, `aicut candidates --assess`, `aicut gate mvp3`, UI 15.4 화면 | 입력·집계 있음. 통과 판정은 사람 |
| MVP 4 | 원본↔완성본 매핑 (= 17.2 데이터셋) | `intelligence/source_output.py`, `aicut learn pairs` | 데이터셋 필요 |
| MVP 5 | 편집 계획만 읽고 결과물을 예상할 수 있는가 | `render/editplan.py: describe()`, `aicut plan` | 사람이 읽는 것으로 검증 |
| MVP 6~9 | 10장 / 11장 / 11.4 / 12.2 | `render/`, `pipeline/packaging.py`, `publishing.py`, `performance.py` | 코드 있음, 관문 측정 없음 |

통과 여부는 어느 쪽도 코드가 정하지 않는다. 19장이 사람에게 맡긴 판단이고,
숫자를 만들어 통과라고 적는 것은 그 판단을 지어내는 것이다.

## 20.1 / 22 — 프로그램과 편집기

| 것 | 어디 | 검증 |
|---|---|---|
| 더블클릭 실행 파일 (exe 하나 + 웹 UI 자동 실행) | `aicut/desktop.py`, `aicut.spec` | CI `desktop` 잡이 체크아웃 밖에서 실행 |
| ffmpeg 자동 다운로드 | `media/ffmpeg_fetch.py`, `aicut fetch-ffmpeg` | 체크섬 미기록 → **거부**. 다운로드 호스트가 이 환경에서 차단되어 실호스트 확인은 안 됨 |
| 편집기 교환 파일 (EDL/FCPXML/SRT) | `render/exchange.py`, `aicut export` | 실제 영상의 계획으로 생성·검증 |
| DaVinci Resolve 스크립트 | `plugin/resolve/` | 산술은 `tests/test_plugin_resolve.py`. **API 호출은 미실행** |
| Premiere Pro 스크립트 | `plugin/premiere/` | 산술은 `tests/test_plugin_premiere.py`가 node로 실행. **API 호출은 미실행** |

## 18장 "프로그램이 담당" 중 운영 측면

18장은 데이터베이스·작업 큐·서버를 프로그램의 몫으로 못박는다. 엔진에는 그
자료구조와 흐름이 있었지만, 사람 없이 돌아가는 동안 그것들을 지키는 층은
없었다. `legacy/`의 Codex 구현에서 이식했다 (경위와 이식 중 고친 결함 3건은
`docs/merge.ko.md`).

| 것 | 어디 | 검증 |
|---|---|---|
| API 키 (`AICUT_UI_API_KEY` / `aicut ui --api-key`) | `aicut/ui/auth.py` | `tests/test_ops.py: ApiKeyGuardTests`, `UiAuthTests` — 키 없이 `/api/*`는 401, 페이지는 200, **워크스페이스는 정적 경로로 도달 불가** |
| OAuth 토큰 암호화 저장 (`AICUT_TOKEN_KEY`) | `aicut/intelligence/token_store.py`, `youtube.load_credentials()` | `TokenStoreTests` — refresh token이 디스크에 평문으로 남지 않고, 변조는 MAC에서 걸리고, 파일은 생성 시점부터 0600 |
| DB 스냅샷 (`aicut backup`) | `aicut/db/backup.py` | `BackupTests` — SQLite 온라인 백업 + `quick_check` + SHA-256, 보존 정책은 이 모듈이 이름 붙인 파일만 지움 |
| 런타임 스케줄러 (`aicut ui --backup-every`) | `aicut/scheduler.py` | `PeriodicTests`, `SchedulerTests` — 한 태스크의 실패가 다른 태스크를 막지 않고, 일 단위 작업이 분 단위 루프에 끌려가지 않음 |

11.4의 업로드 재시도 큐는 여기서 등록하지 않는다. `process_retry_queue()`는
OAuth 자격 증명을 요구하는데 UI 프로세스가 그것을 들고 있다는 보장이 없고,
없는 자격 증명으로 매 분 실패하는 태스크는 신호가 아니라 잡음이다.
자격 증명을 가진 호출자(`aicut upload --retry`)가 실행한다.

`GET /api/runtime`이 스케줄러 상태·보관 중인 스냅샷·키 요구 여부를 함께 돌려준다.
