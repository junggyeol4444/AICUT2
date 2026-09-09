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
| `plugin/finalcut` (어댑터) | Final Cut Pro 전용 | `plugin/common` + `plugin/finalcut` 복사 | 문서 생성·엔진 호출은 테스트됨, **Final Cut 임포트는 미검증** |
| `plugin/vegas` (어댑터) | VEGAS Pro 전용 | `bundle.py` 로 파일 하나 만들어 복사 | 산술·번들은 테스트됨, **VEGAS API 호출은 미검증** |

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

---

## 4. Final Cut Pro — `plugin/finalcut`

Final Cut Pro에는 **스크립트 API가 없다.** 부를 함수도, 그걸 부를 인터프리터도
앱 안에 없다. 그래서 마지막 화살표가 문서다 — 시퀀스를 FCPXML로 쓰고 파일을
Final Cut에 넘기면, Final Cut이 자기 임포트 창을 연다. Resolve·Premiere와 다른
건 그것뿐이고, 읽는 모델도 순서도 거부하는 것도 같다.

### 설치

`plugin/common` 과 `plugin/finalcut` 을 나란히 두면 끝이다. 편집기가 로드하는 게
아니라 **터미널에서 직접 돌리는 스크립트**다. 파이썬 3.6 이상이면 되고 표준
라이브러리만 쓴다.

### 사용 — 두 갈래

```bash
# 이미 받아 둔 모델로 만들기
python plugin/finalcut/aicut_finalcut.py <edit-model>.json

# 4장의 버튼: 방송을 엔진에 넘기고, 기다렸다가, 돌아온 걸로 만들기
python plugin/finalcut/aicut_finalcut.py --engine /방송/live.mkv
```

* macOS면 다 쓰고 나서 Final Cut에 파일을 넘긴다 (`open -a "Final Cut Pro"`).
  다른 OS면 그렇게 말하고 파일 경로만 알려준다. `--no-open` 으로 끌 수 있다.
* `--fps` — 모델에 프레임 레이트가 없으면 **묻는다.** 모든 컷이 이 숫자를 기준으로
  놓이기 때문에 30으로 찍어버리면 전부 밀린다.
* `--out` — 파일 나갈 위치. 기본은 모델 옆.
* 엔진 주소는 `AICUT_ENGINE`, 키는 `AICUT_API_KEY`.

### FCPXML이 못 담는 것 — 조용히 빼먹지 않는다

한 번 돌린 실제 출력이다:

```
AI_ep-live -> .../AI_ep-live.fcpxml
  skipped 40.000-40.005s: shorter than one frame at 30.0 fps
  1 audio placement(s) the model asks for are not in the XML:
    BGM /music/bed.mp3 at 0.00s
  3 marker(s) in the model are not in the XML
  1 caption(s) in the model; import an .srt for them (`aicut export <plan> --format srt`)
```

BGM·효과음은 asset으로 선언해야 하는데 모델은 경로만 주고 길이·프레임 레이트를
주지 않는다. 안 잰 숫자를 지어내느니 몇 개가 빠졌는지 말한다. 마커도 같은 이유다 —
FCPXML의 마커는 타임라인이 아니라 **클립 안에** 붙어서, 엉뚱한 클립에 붙은 마커는
없느니만 못하다.

### 검증 상태

Final Cut Pro는 macOS 전용이고 이 환경에 없다. 임포트 자체는 해 본 적 없다.
대신 확인한 것:

* 이 어댑터가 쓴 FCPXML을 **`aicut export --format fcpxml` 이 같은 에피소드로
  만든 문서와 컷 단위로 대조한다** (`tests/test_plugin_finalcut.py`). offset,
  start, duration, 원본 파일 URI, `frameDuration` 이 전부 같아야 통과한다.
  23.976 같은 NTSC 레이트도 `1001/24000` 으로 같게 나온다.
* 실제 파일(120초짜리 mkv)로 한 번 돌려서 XML이 파싱되고, asset이 **존재하는
  파일**을 가리키고, 한 프레임보다 짧은 컷은 버려지면서 이름이 불리는 것까지 봤다.
* 엔진 실패를 "만들 게 없다"로 잘못 읽던 버그를 여기서 잡았다. 실제로 엔진에
  붙여 보니 STT가 없어서 실패한 작업이 `state: FAILED`, `error: ""` 로 오는데
  `error`만 보던 어댑터 셋 다 그걸 16장(제작 가치 있는 콘텐츠 없음)으로 보고했다.
  이제 셋 다 공통 판정(`failure_reason`)을 쓰고, 서버도 이유를 `error`에 채운다.

---

## 5. VEGAS Pro — `plugin/vegas`

VEGAS는 **파이썬이 아니라 .NET 스크립트**를 돌린다 (`.js`는 JScript.NET, `.cs`는
C#). 그래서 Premiere와 같은 JS 모듈을 쓰되, 한 가지가 다르다 — VEGAS는 **파일
하나만 컴파일한다.** `#include`도 `require`도 없다.

그렇다고 모델 읽는 코드를 이 편집기용으로 또 한 벌 쓰면, 그게 바로 37장이 가운데
층을 둬서 막으려던 것이다. 그래서 **공용 모듈 + VEGAS 본체를 하나로 합쳐서**
설치 파일을 만든다:

```bash
python plugin/vegas/bundle.py
# -> plugin/vegas/aicut_vegas.js (1000줄 남짓, 이거 하나만 복사하면 됨)
```

합쳐진 파일은 저장소에도 들어 있고, **테스트가 매번 다시 만들어서 커밋된 것과
같은지 대조한다** — 생성 파일이 조용히 낡는 걸 막는 유일한 방법이다.

### 설치

```
C:\Program Files\VEGAS\VEGAS Pro <버전>\Script Menu
```

`aicut_vegas.js` 하나만 넣으면 `Tools > Scripting` 메뉴에 뜬다.

### 사용 — 두 갈래

1. **모델 파일을 고른다** — 그 모델로 트랙을 만든다.
2. **취소한다** — 4장의 버튼. 이어서 방송 파일을 고르면 엔진에 넘기고, 기다렸다가,
   돌아온 걸로 만든다. 진행 상황은 스크립트 로그 창에 찍힌다 (26장).

엔진 주소는 `AICUT_ENGINE_HOST` / `AICUT_ENGINE_PORT`, 키는 `AICUT_API_KEY`,
모드는 `AICUT_TIMELINE_MODE` (`new_sequence` 기본 / `edit_current`).

### VEGAS라서 다른 것

* **끝 프레임 문제가 없다.** VEGAS 이벤트는 시작 + 길이 + 소스 오프셋이라 끝
  프레임을 안 쓴다. Resolve(마지막 프레임)와 Premiere(그 다음 프레임)가 서로
  반대라 상수로 못 박아둔 그 문제가 여기선 안 생긴다. 대신 **전부 프레임 단위로**
  계산해서 넣는다 — VEGAS는 초로 주면 프레임 사이에 이벤트를 놓고, 그게 쌓이면
  타임라인 끝에서 아무도 못 찾는 어긋남이 된다.
* **영상과 소리를 따로 놓는다.** Resolve·Premiere는 A/V가 링크된 클립 하나를
  놓지만 VEGAS는 그렇지 않아서, 24장의 원본 음성 트랙을 실제로 만들어서 같이
  깐다.
* **버전마다 생성자가 다르다.** `Sony.Vegas`가 14부터 `ScriptPortal.Vegas`가
  됐고 `new VideoTrack(...)` 인자도 달라졌다. 그래서 두 형태를 다 시도한다.

### 검증 상태

VEGAS Pro는 윈도우 전용이고 이 환경에 없다. API 호출은 실행된 적 없다.

* `aicut_vegas_time.js` — VEGAS를 안 건드린다. node로 돌고
  `tests/test_plugin_vegas.py`가 검사한다. 그중 하나는 같은 모델을 **파이썬
  리더(Resolve가 쓰는 것)와 대조**해서, 살아남는 구간과 길이가 같은지 본다.
* 번들 — 다시 만들어서 커밋된 파일과 대조, node 전용 줄(`require`, `module`)이
  안 남았는지 확인, `import` 가 맨 위에 몰려 있는지 확인. JScript.NET은 import가
  먼저 안 나오면 컴파일 자체가 안 되고, 그 실패는 사람이 알아볼 수 없는 줄 번호가
  적힌 대화상자로 뜬다.
* `aicut_vegas_body.js` — `new VideoEvent`, `AddTake`, `WebClient` 등 API 호출만.
  **실행된 적 없다.**
