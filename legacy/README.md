# legacy/ — Codex 구현체 (동결)

`AICUT2` main 브랜치의 Codex 구현 전체다. **손대지 않는다.**

같은 기획안(`docs/spec-v1.ko.md`)을 두 AI가 각자 처음부터 구현했고, 이 브랜치는
그중 하나(`aicut/`)를 본체로 삼았다. 이쪽은 지운 게 아니라 남겨둔 것이다.

## 왜 남겼나

1. **기록.** `docs/merge.ko.md`가 이 코드를 줄 번호까지 인용해 조항별 대조를 한다.
   여기를 고치면 그 문서가 가리키는 대상이 사라진다.
2. **운영 계층의 출처.** `aicut/ui/auth.py`, `aicut/intelligence/token_store.py`,
   `aicut/db/backup.py`, `aicut/scheduler.py`는 각각 `backend/auth.py`,
   `backend/token_store.py`, `backend/backup.py`, `backend/scheduler.py`에서 왔다.
   이식하며 고친 결함 3건도 원본이 있어야 대조가 된다.
3. **웹 UI 자산.** `src/`의 vite 스튜디오 화면은 디자인이 낫다. 20.1의 PyQt6/Electron
   래퍼를 붙일 때 참고 자산이다. (지금 옮기지 않은 이유는 `docs/merge.ko.md` 3-b절)

## 이 트리는 실행되지 않는다

`legacy/tests/`에는 `__init__.py`가 없다. 그래서
`python -m unittest discover -s . -p "test_*.py"`가 여기를 수집하지 않고,
루트 스위트는 `aicut/`만 검사한다. 의도한 상태다.

`tests/test_legacy_frozen.py`가 그 상태를 고정한다. 누군가 `__init__.py`를 넣어
이 트리를 수집 대상으로 만들면 그 테스트가 먼저 실패하면서 이유를 말한다 —
안 그러면 아래 결함이 전체 스위트를 무너뜨리는데 원인이 안 보인다.

## 알려진 결함 (고치지 않음)

| 곳 | 문제 |
|---|---|
| `backend/subtitles.py:71` | f-string 안 백슬래시 (`{"\n".join(dialogue)}`). Python 3.12(PEP 701) 미만에서 `SyntaxError`. 리포 어디에도 Python 최소 버전 표기가 없다 |
| `backend/subtitles.py` 전체 | `backend/` 안 어디서도 import하지 않는 고아 모듈. 10.3 ASS 생성기가 파이프라인에 연결돼 있지 않다 |
| `backend/server.py:461` + `backend/auth.py:22` | API 키가 정적 경로에서 우회된다. `dist/` 없으면 리포 루트 전체가 문서 루트. `GET /aicut.db`가 무인증 200 (실측 249,856 bytes) |
| `backend/database.py: backup_to()` | `with sqlite3.connect(...) as conn:`은 트랜잭션 컨텍스트지 닫기가 아니다. Windows에서 파일 잠금 |
| `backend/token_store.py:35-37` | `write_text()` 뒤에 `chmod(0o600)`. 그 사이 refresh token이 umask 권한으로 디스크에 있다 |
| `backend/pipeline.py` | 코드 기본값 6종 (`coarse_window_sec=300` 등). 17.1이 외부화하라고 명시한 "1차 통과 밀도" 포함 |
| `src/main.js` | `file?.path`는 Electron 전용. 브라우저에선 `file.name`이 경로 자리에 들어간다 — 웹 UI로 실제 파일 등록 불가 |

앞의 넷은 이식본에서 고쳤다. 여기 원본은 대조용으로 그대로 둔다.
