# 두 구현체 대조 — 기획안 조항별

같은 기획안(`docs/spec-v1.ko.md`, 그 상위 골격은 `docs/spec-original.ko.md`)을
두 AI가 각자 처음부터 구현했다. 코드 공유는 없다. 패키지 구조도 DB 스키마도 다르다.

| | `junggyeol4444/aicut` | `AICUT2` main |
|---|---|---|
| 작성자 | Claude — 49커밋 중 48개 | Codex — PR 6건 전부 `codex/*` 브랜치 |
| 기간 | 2026-08-20 ~ 09-04 | 2026-08-27 ~ 09-07 |
| 본체 | Python 11,008줄 / 65모듈 | Python 5,348줄 / 35모듈 + JS 357줄 |
| 테스트 | 450개, 전부 통과 | 156(py) + 5(js), 1개 import 실패 |
| CI | Actions 6잡 (Linux 3.10/3.13, Windows, macOS, 빌드·설치, 데스크톱) | 없음 |

이 문서는 어느 쪽이 낫다는 주장이 아니라, **조항별로 코드에서 확인한 사실**이다.
확인 방법을 각 항에 적어 두었으므로 직접 다시 돌려볼 수 있다.

---

## 1. 양쪽 다 지킨 것

기획안의 강제 조항 — 틀리면 시스템이 기획 의도와 정반대가 되는 것들 — 은
두 구현체 모두 제대로 지켰다.

| 조항 | 요구 | 양쪽 근거 |
|---|---|---|
| **13.1** | 에피소드에 `start_sec`/`end_sec` 컬럼을 두지 않는다 | aicut `db/schema.sql: tb_episode` / AICUT2 `legacy/backend/schema.sql: episodes` — 양쪽 다 없음 |
| **2.4 / 13.2** | 컷 단위 비선형 타임라인 (`sequence_order` ≠ `source_start_sec`) | aicut `TB_EDIT_TIMELINE` / AICUT2 `edit_timeline` |
| **9.3** | 컷마다 KEEP / TRIM / CUT | 양쪽 `pacing_mode` 컬럼 |
| **11.3** | 검수 게이트를 통과하지 않은 영상은 공개되지 않는다 | aicut `publishing.publish_approved()`가 미승인 시 `PermissionError` / AICUT2 `queue_upload()`가 `review_status != APPROVED`면 거부하고 PUBLIC 자체를 막음 |
| **11.4** | 재시도는 PT 자정 기준 (24시간 후가 아니라) | aicut `intelligence/quota.py` / AICUT2 `legacy/backend/upload.py: next_quota_reset()` |
| **2.2 / 16장** | `NO_CONTENT`는 실패가 아니라 정상 종료 | aicut `runner._no_content()` / AICUT2 `database.py:544` |
| **10.4-3** | 라우드니스는 2-pass | 양쪽 `loudnorm` 측정 후 주입 |
| **10.4-2** | `acrossfade` 대신 컷 단위 `afade` + `concat` | 양쪽 동일 |

**여기까지는 "100% 기획안대로"라는 주장이 맞다.** 데이터 모델을 잘못 잡으면
2.4와 5.4가 표현 불가능해지는데, Codex는 그 함정을 피했다.

---

## 2. aicut에만 있는 것

| 조항 | 요구 | aicut | AICUT2 | 확인 방법 |
|---|---|---|---|---|
| **18장** | AI가 담당: 콘텐츠 경계·개수·종류·제작 여부·장면 선택·순서·구조·길이·편집 의도·**호흡 판정**·제목/썸네일 방향·제작 전략 | ✅ `llm/base.py: Producer` + `anthropic_provider.py` + `prompts.py`. `--producer anthropic`으로 실제 실행 | ❌ **0건** | `grep -rE "anthropic\|openai\|llm" legacy/backend/ legacy/src/` → 0 |
| **4장 전체** | YouTube Content Intelligence — 공개 지표 수집, 제작 패턴 추출, `TB_YT_REFERENCE` | ✅ `intelligence/reference.py`, `knowledge.py` | ❌ 없음 | `grep -rlE "public_metrics" legacy/backend/` → 0 |
| **5.3** | 상황 라벨 (단독토크 / 게임 / 다인원 / 자리비움) | ✅ `models.SituationLabel`, `analysis/signals.py` | ❌ 없음 | `grep -rlE "situation" legacy/backend/` → 0 |
| **17.2** | 캘리브레이션 데이터셋 구축 — 이 프로젝트의 병목 | ✅ `aicut dataset` + `calibration/dataset.py` (`derive-silences` 포함) | ❌ 없음 | `grep -rlE "dataset" legacy/backend/` → 0 |
| **17.4** | 파라미터 스윕 후 17.3 지표로 평가 | ✅ `calibration/sweep.py`, `harness.py`, `metrics.py` | ❌ `calibrate_pacing()`은 F1 계산만, 스윕 없음 | |
| **17.5** | 미측정 값은 확정값이 아님 — "임시" 표기 유지 | ✅ `provisional` / `measured` 마킹, 리포트 기록, `--strict`는 실행 거부 | ❌ **0건** | `grep -rlE "provisional" legacy/backend/` → 0 |
| **10.4-1** | 얼굴 추적 줌 — (a)segment_crop / (b)sendcmd / (c)프레임 합성 중 택1 | ✅ `render/ffmpeg.py: zoom_filter()`, `sendcmd_file()` 둘 다 구현, 선택은 프로파일 | ❌ 화면 중앙 고정 crop (`render.py:111`). 얼굴 추적이 아님 | |
| **22.1** | 단일 실행 가능한 데스크톱 프로그램 | ✅ `desktop.py` + `aicut.spec`, CI가 3 OS에서 빌드·실행 | ❌ 없음 | |
| **19장** | MVP 단계별 성공 기준 | ✅ README·docs가 MVP별 상태를 명시, `docs/measurements.md`에 실측 | ❌ 로드맵 언급 없음 | |
| **20.2** | 6시간 원본 STT·비전 처리 시간 실측 (R3) | ✅ 6시간 실측 — 신호 6.6분, 편집 계획 4.2분, RSS 74MB, 재개 254초→2.4초 | ❌ 없음 | `docs/measurements.md` |

### 17.1 위반 — AICUT2의 코드 기본값

기획안 17.1은 **"모든 판정 기준을 코드가 아닌 설정 파일에 둔다"**고 쓰고,
외부화 대상에 **"1차 통과 밀도"**와 **"2차 정밀 통과 대상 선정 기준"**을 명시했다.

AICUT2 `legacy/backend/pipeline.py`에 남은 코드 기본값:

```
line 200, 310   coarse_window_sec = 300   ← 17.1이 명시한 "1차 통과 밀도"
line 222        audio_window_sec  = 1.0
line 243        vision_interval_sec = 5.0
line 411        render_width/height = 1920 / 1080
line 421        loudness_range = 11
line 180        disk_reserve_bytes = 0
```

aicut은 `config.py`에서 프로파일에 없는 값을 읽으면 `ConfigError`를 던진다.
코드 기본값으로 넘어가지 않는다.

> AICUT2 README는 "정밀 분석 밀도 값은 하드코딩하지 않으며 채널 캘리브레이션에서
> 반드시 전달해야 합니다"라고 적었다. 정밀(2차) 쪽은 사실이다.
> 1차 통과 밀도(`coarse_window_sec`)는 그렇지 않다.

### 11.3 부분 미달 — 누가 승인했는지

기획안 11.3은 검수 게이트를 필수 단계로 승격시켰다. 게이트 자체는 양쪽 다 있다.
다만 AICUT2의 `review_episode(episode_id, approved: bool)`은 **검수자를 기록하지 않는다.**
공개를 허락한 사람이 누구인지 DB에 남지 않는다.
aicut의 검수 API는 검수자 이름 없이는 승인을 거부한다 (`review.add_argument("--reviewer", required=True)`).

### 테스트 1개가 깨져 있음

```
$ python3 -m unittest discover -s tests   # AICUT2 main
SyntaxError: f-string expression part cannot include a backslash
  legacy/backend/subtitles.py:71
Ran 156 tests — FAILED (errors=1)
```

`{"\n".join(dialogue)}` — f-string 안의 백슬래시는 Python 3.12(PEP 701)부터 허용된다.
README·package.json 어디에도 Python 최소 버전 표기가 없다.
그리고 이 모듈은 `legacy/backend/` 안 어디에서도 import되지 않는다 — 10.3 ASS 생성기가
파이프라인에 연결되어 있지 않다.

---

## 3. AICUT2에만 있던 것 → 이 브랜치로 이식함

기획안 18장이 **프로그램 담당**으로 지정한 "데이터베이스 / 작업 큐 / 서버"의
운영 측면이다. aicut에는 없었다.

| 기능 | 원본 | 이식 위치 | 테스트 |
|---|---|---|---|
| API 키 인증 | `legacy/backend/auth.py` | `aicut/ui/auth.py` | `tests/test_ops.py: ApiKeyGuardTests`, `UiAuthTests` |
| OAuth 토큰 암호화 저장 | `legacy/backend/token_store.py` | `aicut/intelligence/token_store.py` | `TokenStoreTests` |
| SQLite 온라인 백업 | `legacy/backend/backup.py` | `aicut/db/backup.py` | `BackupTests` |
| 런타임 스케줄러 | `legacy/backend/scheduler.py` | `aicut/scheduler.py` | `PeriodicTests`, `SchedulerTests` |

이식하면서 고친 것 3가지. 셋 다 원본에 실제로 있던 결함이다.

### (1) API 키가 정적 파일 경로에서 우회됐다

원본 `auth.py:22`는 `/api/`로 시작하지 않는 모든 경로에 True를 반환했고,
`server.py:461`의 정적 핸들러는 `dist/`가 없을 때 **리포지토리 루트 전체**를
문서 루트로 삼았다. 둘이 겹치면 API 키는 JSON API만 지킨다.

`AICUT_API_KEY=secret123`으로 서버를 띄우고 `Authorization` 헤더 **없이** 실측:

| 요청 | 결과 |
|---|---|
| `GET /api/projects` | `401` ✅ |
| `GET /package.json` | **`200`** |
| `GET /backend/token_store.py` | **`200`** — 소스 전문 |
| `GET /aicut.db` | **`200`, 249,856 bytes** — DB 전체 |
| `GET /../../etc/passwd` | `400` ✅ (루트 밖 traversal은 막혀 있었다) |

이식본에서 정적 경로는 `aicut/ui/static/`(패키지가 들고 온 페이지)만 도달 가능하고,
`tests/test_ops.py: test_the_workspace_is_not_reachable_through_the_static_route`가
워크스페이스가 그 경로로 새지 않는지 고정한다.

### (2) 토큰 파일의 권한 창

원본은 `write_text()` 다음 줄에서 `os.chmod(0o600)`을 했다.
그 두 호출 사이에 refresh token이 umask가 허용하는 권한으로 디스크에 있었다.
이식본은 `os.open(..., 0o600)`으로 연다.

### (3) 백업이 SQLite 커넥션을 닫지 않았다

원본 `database.backup_to()`는 `with sqlite3.connect(tmp) as conn:`을 썼다.
sqlite3 커넥션에서 `with`는 **트랜잭션** 컨텍스트지 닫기가 아니다.
리눅스에서는 핸들 누수로 끝나지만 Windows에서는 파일 잠금이 되어
스냅샷을 공개하는 `os.replace`가 실패한다.

이 프로젝트 CI는 정확히 같은 모양의 버그에 이미 한 번 당했다 —
UI가 요청 스레드마다 커넥션을 열고 닫지 않아 리눅스에서는 안 보이고
Windows에서만 워크스페이스가 삭제 불가였던 건. 그래서 가정이 아니다.
이식본은 모든 임시 커넥션을 `contextlib.closing`으로 감싼다.

---

## 3-b. 15장 UI — 웹 UI를 옮기는 대신 빠진 조항을 채웠다

Codex 구현의 vite 스튜디오 화면은 디자인이 낫다. 그래도 그 앱을 가져오지 않았다.

기획안 22.1은 **단일 실행 가능한 데스크톱 프로그램**을 산출물로 요구하고,
그것은 `aicut.spec`(PyInstaller)이 CI에서 3개 OS로 빌드·실행해 지키고 있다.
vite 빌드 단계를 끼우면 실행 파일에 빌드 산출물을 함께 실어야 하고, 그 산출물이
소스와 어긋났는지 검증할 방법이 또 필요해진다. 화면 하나 예쁘게 만들자고
22.1의 검증 경로를 흐리는 거래는 맞지 않는다.

대신 `aicut ui`의 단일 정적 페이지에서 **15장이 요구하는데 실제로 없던 것**을 채웠다.
빌드 단계는 여전히 없다.

| 15장 요구 | 이전 | 지금 |
|---|---|---|
| 15.2 드래그 앤 드롭 | 없음 | `file://` URI를 읽고, 없으면 무엇이 빠졌는지 표시 |
| 15.2 캘리브레이션 프로파일 선택 | 서버 전역 `--profile`만 | `GET /api/profiles` + 제출 단위 선택 |
| 15.5 썸네일 프리뷰 | 경로 문자열만 | 이미지로 서빙 (`/thumbnail/<n>`) |
| 15.5 재생 프리뷰 | 없음 | Range 지원 `/video` |
| 15.5 폴더 열기 | 없음 | `POST /reveal` (루프백 전용, 셸 없음) |
| 15.5 제목 후보 3종 | 첫 번째만 강조 | 후보 전체를 나열 |

Codex 구현의 웹 앱은 `legacy/src/`에 그대로 있다. 나중에 PyQt6/Electron 래퍼를
붙일 때(20.1) 참고 자산으로 쓸 수 있다.

**드래그 앤 드롭은 그쪽 버그를 그대로 옮기지 않았다.** Codex 구현의
`api.createProject({file_path: file?.path || file?.name || ...})`은 브라우저
`File` 객체에 없는 `.path`(Electron 전용)를 읽고, 실패하면 **파일명**을 경로 자리에
넣어 보낸다. 그 UI로는 실제 파일 등록이 불가능하다. 이식본은 파일 관리자가 함께
싣는 `file://` URI에서 진짜 경로를 읽고, 그것이 없으면 무엇이 부족한지 말한다.

---

## 4. 결론

> **"코덱스가 100% 기획안대로 만들었다"** — 강제 조항(13.1 데이터 모델, 11.3 게이트,
> 11.4 쿼터, 9.3 호흡 값, 10.4-2/3)에 한해서는 사실이다. 이 부분은 제대로 했다.
>
> 다만 4장(레퍼런스 학습) 전체, 5.3(상황 라벨), 17.2·17.4(캘리브레이션 절차),
> 17.5(임시값 표기), 18장의 **AI 담당 전부**, 22.1(데스크톱 프로그램)이 없고,
> 17.1은 코드 기본값 6종으로 위반돼 있다.
>
> 그리고 검증할 방법이 리포 안에 없었다 — 기획안 원문도, 장 번호 인용도 0건이었다.
> (aicut은 README 60회 + `docs/spec-map.md` 69회) 이번에 기획안 2종을
> `docs/`에 넣은 이유다.

가장 실질적인 차이는 **18장**이다. AICUT2는 AI 단계 6개를 전부
`*_executable` 옵션으로 외부 프로그램에 위임하고, 그 프로그램은 리포에 없다.
방송을 넣으면 FFmpeg 기반 신호 추출까지는 되지만 콘텐츠는 나오지 않는다.
기획안 18장이 "AI가 담당"으로 지정한 판단이 아무 데도 구현돼 있지 않기 때문이다.

계약을 정의한 것 자체는 나쁜 설계가 아니다. 하지만 기획안 23장(핵심 철학)이
말하는 "상황에 따라 판단하는 AI 콘텐츠 프로듀서"는 계약만으로 존재하지 않는다.
