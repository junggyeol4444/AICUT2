# aicut 편집기 플러그인

`aicut`은 편집기 없이 혼자 완성 영상까지 만든다. 이 폴더는 그 결과를
**렌더링하지 않고 편집기 타임라인으로 받는 쪽**이다 — 컷은 AI가 정하고,
마무리는 사람이 자기 편집기에서 한다.

두 갈래가 있다. 편집기를 고르기 전에 이걸 먼저 봐라.

| 방법 | 대상 | 설치 | 검증 상태 |
|---|---|---|---|
| `aicut export` (교환 파일) | Premiere Pro, Final Cut Pro, Resolve, Avid 등 **전부** | 불필요 | 실제 영상의 계획으로 생성·검증 완료 |
| `plugin/resolve` (어댑터) | DaVinci Resolve 전용 | `plugin/common` + `plugin/resolve` 복사 | 판단·산술·엔진 호출은 테스트됨, **Resolve API 호출은 미검증** |
| `plugin/premiere` (어댑터) | Premiere Pro 전용 | `plugin/common` + `plugin/premiere` 복사 | 판단·산술·엔진 호출은 테스트됨, **Premiere API 호출은 미검증** |

기획안 37장의 구조다:

```
AI Engine  →  Common Edit Model  →  Editor Adapter  →  편집기
```

어댑터는 **Common Edit Model**을 읽는다. 편집 계획을 직접 읽지 않는다 —
계획의 의미가 두 번 구현되면 둘이 어긋난다. 계획을 주면 어댑터가 이름을
붙여서 거부하고 어디서 모델을 받는지 알려준다.

---

## 1. 어느 편집기든 — `aicut export`

플러그인이 필요 없는 쪽이다. 편집 계획을 EDL / FCPXML / SRT로 쓴다.

```bash
aicut run stream.mkv --no-render          # 계획까지만
aicut export workspace/<project>/plans/<episode>.json --format fcpxml --format srt
```

* `--fps` 를 주지 않으면 계획의 렌더 설정을 따른다. 편집기 시퀀스의 프레임
  레이트와 **반드시** 같아야 한다. 다르면 모든 컷이 조금씩 밀린다.
* FCPXML은 원본의 실제 해상도를 따로 선언한다. 원본을 읽을 수 없는 곳에서
  내보내면 그 사실을 출력에 적고, 편집기에서 클립이 늘어나 보이면 relink 하면 된다.
* EDL은 컷만 옮긴다. 자막·크롭·라우드니스는 EDL 포맷이 담지 못한다 —
  자막은 `--format srt` 로 따로 받고, 나머지는 `aicut render` 쪽에만 있다.
* 정직하게 옮길 수 있는 타임코드가 없는 프레임 레이트는 EDL 생성을 **거부**한다.
  틀린 타임코드를 내보내는 것보다 낫다.

---

## 2. DaVinci Resolve — `plugin/resolve`

교환 파일이 아니라 Resolve 안에서 직접 타임라인을 만든다. Common Edit Model을
읽고, 원본을 미디어 풀에 넣고, 모델이 정한 순서대로 클립을 얹는다.

### 설치

`plugin/common` 과 `plugin/resolve` 를 Resolve의 스크립트 폴더에 복사한다.

```
Windows  %APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility
macOS    ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility
Linux    ~/.local/share/DaVinciResolve/Fusion/Scripts/Utility
```

`plugin/common/` 폴더도 같이 복사해야 한다 (`aicut_model.py`, `aicut_engine.py`).
결정은 전부 그쪽에 있다.

### 사용

1. Resolve에서 프로젝트를 열고, **프로젝트 설정의 프레임 레이트를 먼저 정한다.**
   스크립트는 모델이 아니라 프로젝트의 프레임 레이트로 타임라인을 만든다.
2. `Workspace > Scripts > aicut_resolve`
3. 모델 `.json` 을 고르거나, **취소한다.** 취소가 4장의 버튼이다 — 지금
   타임라인에 올려둔 방송을 엔진(`aicut ui`)에 넘기고, 분석이 끝나면 돌아온
   모델로 타임라인을 만든다.

터미널에서 바로 돌려도 된다:

```bash
python aicut_resolve.py <edit-model>.json
```

자막은 모델에 들어 있지만 Resolve 스크립팅 API로는 얹지 못한다. 몇 줄이
빠졌는지 출력하고, 필요하면 따로 받아서 올린다:

```bash
aicut export workspace/<project>/plans/<episode>.json --format srt
```

### 이 스크립트가 지키는 것

* **타임라인 순서** (2.4). 원본 시간순이 아니라 모델이 정한 순서다. 시간순으로
  다시 정렬해 버리면 AI가 고른 구조가 조용히 사라진다.
* **컷 안에서 버린 구간** (9.3). `remove_spans`가 있는 컷은 클립 하나가 아니라
  여러 개로 쪼개져 들어간다. 무시하면 aicut이 잘라낸 공백이 그대로 재생된다.
* **`endFrame`은 마지막 프레임이지 그 다음 프레임이 아니다.** 여기서 1이 어긋나면
  타임라인의 모든 컷이 한 프레임씩 길거나 짧아지고, 내보내기 전까지 아무도 모른다.
* **한 프레임보다 짧은 구간**은 버리되 조용히 버리지 않고 몇 초짜리였는지 출력한다.

### 검증 상태 — 읽고 넘어가라

이 저장소가 만들어진 환경에 **Resolve가 설치되어 있지 않다.** 그래서 코드를
둘로 쪼갰다:

* `common/aicut_model.py` — Resolve를 import하지 않는다. 초→프레임, 컷 순서,
  `remove_spans` 분할, 짧은 구간 처리, 모델 로딩·오류 메시지. `tests/test_plugin_model_reader.py`
  와 `tests/test_plugin_resolve.py`가 이걸 전부 테스트하고, 그중 하나는 실제
  직렬화된 모델을 통과시켜 플러그인의 `kept_spans`가 aicut 본체의
  `Cut.kept_spans`와 같은 답을 내는지 대조한다. 두 벌로 갈라진 구현은 반드시
  어긋나기 때문이다.
* `aicut_resolve.py` — `CreateTimelineFromClips`, `AddItemListToMediaPool`,
  `ImportIntoTimeline` 등 Resolve API 호출만 있다. **이 호출들은 실행된 적이 없다.**
  Blackmagic 스크립팅 문서를 보고 쓴 것이다. 첫 실행이 곧 테스트다.

엔진이 실제로 만든 모델(컷 10개, 그중 둘은 한 프레임보다 짧고 셋은 안에서
구간이 잘린 것)로 클립 리스트 생성까지 돌려 봤다. 파이썬 리더와 JS 리더가 같은
답을 낸다:

```
30 fps     : 11 clips, 90.0s, dropped 2
23.976 fps : 12 clips, 90.1s, dropped 1
```

프레임 레이트가 바뀌면 살아남는 구간 수가 달라진다는 것도 여기서 보인다 —
버리되 몇 개를 버렸는지 말한다. 여기까지가 확인된 부분이고, 그 리스트를
Resolve/Premiere에 넘기는 마지막 한 줄은 확인되지 않았다.

### 잘 안 될 때

| 증상 | 원인 |
|---|---|
| `this script must be run from inside DaVinci Resolve` | Resolve 메뉴가 아니라 밖에서 돌렸고 `DaVinciResolveScript`가 경로에 없다 |
| `Resolve is not running, or scripting is disabled` | 환경설정 > System > General 의 외부 스크립팅을 켠다 |
| `Resolve refused to build the timeline` | 클립과 프로젝트의 프레임 레이트가 다르다 |
| `the model's source is not at ...` | 원본을 옮겼다. 되돌리거나 새 위치로 다시 분석한다 |
| `no aicut engine at ...` | 엔진이 꺼져 있다. `aicut ui` 로 켠다 |
| 자막이 안 들어감 | 스크립팅 API에 없다. `aicut export --format srt` 로 받아서 올린다 |

---

## 3. Premiere Pro — `plugin/premiere`

Resolve 쪽과 **같은 구조**다. 같은 Common Edit Model을 읽고, 같은 순서로 얹고,
같은 것을 거부한다. 다른 건 언어(ExtendScript)와 편집기 API뿐이다. 어댑터마다
모델의 의미를 다르게 구현하면 편집기별로 결과가 갈리는데, 37장이 가운데 층을
둔 이유가 그거다.

ExtendScript 파일이다. **CEP 확장이 아니다** — ZXP도, 서명 인증서도, 확장 관리자도
필요 없다. 폴더 복사하면 설치 끝이다.

### 설치

```
Windows  %APPDATA%\Adobe\Premiere Pro\<버전>\Scripts
macOS    ~/Documents/Adobe/Premiere Pro/<버전>/Scripts
```

`plugin/common` 과 `plugin/premiere` 를 **나란히** 복사한다. 스크립트가
`../common/` 을 `#include` 한다:

```
Scripts/
  common/aicut_model.js      모델을 읽는 쪽 (판단)
  common/aicut_engine.js     엔진과 통신하는 쪽
  premiere/aicut_build.js    프레임·틱 산술
  premiere/aicut_premiere.jsx  Premiere API 호출만
```

### 사용 — 두 갈래

`File > Scripts > aicut_premiere` 를 누르면 파일 선택창이 뜬다.

1. **모델 파일을 고른다** — 이미 받아 둔 Common Edit Model로 시퀀스를 만든다.
2. **취소한다** — 4장의 버튼이다. 지금 시퀀스에 올려둔 방송 파일을 엔진에
   넘기고, 분석이 끝나기를 기다렸다가, 돌아온 모델로 시퀀스를 만든다.
   진행 상황은 엔진이 말하는 단계 이름 그대로 콘솔에 찍힌다 (26장).

엔진은 로컬 `aicut ui` 서버다. 먼저 켜 둬야 한다:

```bash
aicut ui                     # 기본 127.0.0.1:8765
```

주소가 다르면 `AICUT_ENGINE_HOST` / `AICUT_ENGINE_PORT`, 서버에 키를 걸었으면
`AICUT_API_KEY` 로 알려준다. 파일 선택창 없이 돌리려면 `AICUT_MODEL` 에 경로를
넣는다. 25장의 두 모드는 `AICUT_TIMELINE_MODE` 로 고른다 — 기본값
`new_sequence`(내 편집은 건드리지 않고 새 시퀀스), `edit_current`(지금 시퀀스에
이어 붙임).

첫 비디오 트랙에 클립이 이미 있으면 **거부한다.** 남의 편집 위에 조용히
얹는 것보다 시퀀스 하나 새로 만드는 편이 싸다.

자막과 BGM·효과음은 모델에 있지만 Premiere 스크립팅 API에 "임의의 파일을 임의의
시각에 놓는" 호출이 없다. 그래서 **조용히 빠뜨리지 않고 몇 개가 안 들어갔는지
출력한다.** 자막은 `aicut export <plan> --format srt` 로 따로 받아서 캡션 트랙에
끌어다 놓으면 된다.

### 이 스크립트가 지키는 것

Resolve 쪽과 같다 — 타임라인 순서(2.4), `remove_spans` 분할(9.3), 한 프레임 미만
구간 보고. 더해서 Premiere 특유의 두 가지:

* **틱**. Premiere는 초도 프레임도 아닌 틱(초당 254,016,000,000)으로 센다.
  6시간이면 5.5e15라 double이 1 단위로 못 세는 영역이고, 그래서 Premiere의
  `Time.ticks`도 문자열이다. 이쪽도 문자열로 낸다.
* **out point가 다음 프레임이다.** Resolve의 `endFrame`은 마지막 프레임이고
  Premiere의 out point는 그 다음 프레임이다. 정반대라서 양쪽 다 상수로 적어 놨다.
  1이 어긋나면 모든 컷이 한 프레임씩 길거나 짧아지고 내보내기 전까지 아무도 모른다.

### 검증 상태

Resolve와 같은 이유로 같은 방식이다. Premiere Pro가 이 환경에 없다.

* `common/aicut_model.js`, `common/aicut_engine.js`, `premiere/aicut_build.js` —
  Premiere를 건드리지 않는다. node로 돌고, `tests/test_plugin_premiere.py`가 그
  실제 파일들을 node로 실행해서 검사한다. 파이썬으로 다시 구현해서 비교하면 두
  복사본이 서로 같다는 것만 증명되므로 그렇게 하지 않았다. 대신 **같은 모델을
  파이썬 리더와 JS 리더에 각각 넣고 답을 대조한다** — Resolve가 읽는 쪽과
  Premiere가 읽는 쪽이 갈라지는 순간 실패한다.
* 엔진 통신은 **실제로 돌려 봤다.** 켜 놓은 `aicut ui`에 이 파일이 만든 HTTP를
  raw 소켓으로 그대로 보내서 확인했다: `GET /api/profiles` 응답 정상, 없는
  에피소드는 `HTTP 404`로 올라오고, 한글 경로(`/한글/방송.mkv`)도 서버가 그대로
  받았다 (Content-Length를 글자 수가 아니라 바이트 수로 세야 하는 자리다).
* `aicut_premiere.jsx` — `insertClip`, `setInPoint`, `importFiles`,
  `createNewSequence`, `Socket` 등 API 호출만. **실행된 적 없다.** Adobe
  ExtendScript 문서를 보고 썼다. 첫 실행이 곧 테스트다.

FCPXML 경로(`aicut export --format fcpxml`)는 Premiere에서도 되고 그쪽은 검증됐다.
스크립트가 안 맞으면 그걸 쓰면 된다.
