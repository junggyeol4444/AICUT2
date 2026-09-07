# AICUT 편집기 연동 및 영상 학습·분석 대화 가이드

> 이 문서는 실행 코드가 아니라, 실제 편집 작업자가 AICUT을 어떻게 사용하고 무엇을 기대해야 하는지 설명하는 별도 운영 문서입니다.

## 먼저 정확히 말하면

**사용자:** 이제 정말 100%고, 영상을 편집기에 넣기만 하면 알아서 편집되나요?

**AICUT:** 코드로 계획한 로컬 분석·기획·렌더·업로드 흐름은 구현됐지만, 모든 상용 편집기에 설치되는 범용 플러그인이 완성됐다는 뜻은 아닙니다. 편집기마다 비공개 프로젝트 형식과 확장 API가 다르기 때문에 AICUT은 우선 공개 교환 형식으로 결과를 내보냅니다. 실제 모델 실행 파일, GPU 환경, 코덱과 편집기 버전을 연결한 실영상 검증은 반드시 별도로 해야 합니다.

## 지원하는 교환 파일

분석과 기획이 끝난 에피소드에 다음 요청을 보냅니다.

```bash
curl -X POST http://127.0.0.1:8787/api/episodes/EPISODE_ID/editor-export \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer YOUR_AICUT_API_KEY' \
  -d '{"fps":30,"output_directory":"/exports/my-episode"}'
```

생성되는 번들은 다음과 같습니다.

| 파일 | 용도 |
|---|---|
| `aicut-timeline.fcpxml` | XML timeline을 받아들이는 NLE용 비선형 컷 순서, source in/out |
| `aicut-timeline.edl` | CMX3600 EDL을 받아들이는 편집기용 컷 목록 |
| `aicut-timeline.csv` | CapCut·Vrew를 포함해 자동 timeline 교환이 제한된 도구에서 검토·수동 반영할 수 있는 UTF-8 BOM 컷 시트 |
| 렌더된 MP4와 ASS/SRT | 프로젝트 교환 형식이 맞지 않는 편집기에서 완성본과 자막을 가져오는 호환 경로 |

Adobe Premiere Pro나 DaVinci Resolve에서는 먼저 FCPXML을 시험하고, 해당 버전에서 XML 해석이 맞지 않으면 EDL을 사용합니다. CapCut과 Vrew는 버전·플랫폼마다 프로젝트 timeline import 지원이 달라 독점 프로젝트 파일을 직접 생성하지 않습니다. 이 경우 AICUT 렌더 MP4, 자막 파일, CSV 컷 시트를 사용합니다. **원본을 이동하면 XML/EDL의 미디어를 다시 연결해야 합니다.**

## “영상을 넣으면 분석하고 편집”하는 실제 순서

**사용자:** 원본 영상 하나가 있습니다. 무엇부터 하나요?

**AICUT:** 프로젝트를 만들고 원본의 절대 경로를 전달합니다.

```bash
curl -X POST http://127.0.0.1:8787/api/projects \
  -H 'Content-Type: application/json' \
  -d '{"file_path":"/media/live.mp4","name":"방송 1화"}'
```

**사용자:** 그러면 즉시 AI 편집이 끝나나요?

**AICUT:** 아닙니다. 사용할 STT·오디오·비전·이해·기획 모델 실행기를 설정한 뒤 `/api/projects/{project_id}/run`을 호출해야 합니다. 모델이 연결되지 않은 단계는 가짜 결과를 만들지 않고 대기하거나 명시적으로 실패합니다. 분석 결과가 만들어지면 사람이 후보와 타임라인을 검수하고, 에피소드별 `editor-export`를 실행합니다.

**사용자:** 왜 바로 상용 편집기 프로젝트 파일을 수정하지 않나요?

**AICUT:** 원본 프로젝트를 자동으로 수정하면 미디어 오프라인, 프레임레이트 변환, 플러그인 손실 또는 기존 편집 덮어쓰기가 발생할 수 있습니다. 그래서 AICUT은 새 interchange timeline을 만들고 원본 프로젝트는 건드리지 않습니다.

## 영상 분석은 어떻게 하나요?

**사용자:** AICUT이 영상에서 무엇을 봅니까?

**AICUT:** 다음 신호를 모두 원본 시간축으로 정렬합니다.

1. STT 단어·문장·화자·confidence
2. 트랙별 음량 변화, 무음, 웃음·환호·효과음 같은 오디오 사건
3. 장면 전환, 얼굴, OCR, 게임 UI 같은 비전 관찰
4. 긴 방송을 window로 나눈 요약과 이전 window의 누적 memory
5. 여러 시간대에 흩어진 동일 사건 mention과 후보 장면

1차 coarse scan 뒤 confidence가 낮거나 변화 신호가 강한 구간만 precision scan합니다. 발견 단계는 “무슨 사건과 콘텐츠 후보가 있는가”를 결정하고, retrieval은 후보에 맞는 원본 장면을 찾습니다. planner는 그 장면을 시간순이 아닌 이야기 순서로 배열하고, pacing은 각 컷을 KEEP/TRIM/CUT으로 조정합니다.

## 영상 학습은 어떻게 하나요?

**사용자:** 완성본을 보고 AI가 자동으로 제 취향을 학습하나요?

**AICUT:** 무조건 자동 학습하지 않습니다. 학습 데이터는 다음 두 경로로 만듭니다.

1. **원본–완성본 쌍:** 원본에서 어떤 구간을 선택·삭제·재배열·반복·강조했는지 계산합니다.
2. **게시 성과:** YouTube 유지율 변화를 완성본 컷과 원본 장면 역할에 상관관계로 연결합니다.

**사용자:** 조회수가 높으면 바로 다음 영상에 적용되나요?

**AICUT:** 아닙니다. 표본 수가 작으면 `INSUFFICIENT_SAMPLE` 또는 `HOLD`로 남깁니다. 충분한 여러 영상에서 효과와 confidence interval을 계산해 PROMOTE/HOLD/ROLLBACK 전략 초안을 만들고, 사람이 명시적으로 활성화한 전략만 다음 discovery/planning 입력에 포함합니다. 상관관계를 인과관계처럼 자동 적용하지 않습니다.

**사용자:** 학습 품질을 높이려면 무엇을 기록해야 하나요?

**AICUT:** 원본 파일, 최종 출력, 실제 컷 순서, 장면 역할, 자막, 제목·썸네일 버전, 게시 시각, 조회수와 유지율 snapshot을 같은 에피소드 ID로 보존하세요. 서로 다른 채널이나 포맷의 데이터를 한 calibration profile에 무작정 섞지 말고, 최소 표본 기준을 충족한 뒤 전략을 검수하세요.

## 편집기별 체크리스트

**Premiere Pro / DaVinci Resolve 계열**

1. 새 프로젝트 또는 복제 프로젝트를 만든다.
2. 원본 영상의 프레임레이트와 export 요청의 `fps`가 같은지 확인한다.
3. FCPXML을 먼저 가져오고 미디어를 relink한다.
4. 오디오 트랙 매핑, 속도 변경, transition을 검수한다.
5. XML 호환 문제가 있으면 EDL과 CSV를 비교한다.

**CapCut / Vrew 및 기타 편집기**

1. AICUT 렌더 MP4를 가져오는 경로가 가장 안정적이다.
2. 자막은 지원되는 ASS/SRT 방식으로 가져온다.
3. 재편집이 필요하면 CSV의 source in/out과 순서를 기준으로 컷을 재구성한다.
4. 독점 프로젝트 파일을 임의 변조하지 않는다.

## 최종 검수

내보낸 timeline은 반드시 다음을 확인해야 합니다.

- 원본 미디어 relink 여부
- sequence fps 및 오디오 sample rate
- 컷 경계의 프레임 오차
- 다중 오디오 트랙과 ducking 결과
- 자막 싱크와 안전 영역
- 저작권·개인정보·플랫폼 정책
- 게시 전 사람 승인

AICUT은 편집 결정을 자동 생성하고 교환 파일로 전달하지만, 상용 편집기별 플러그인 호환성과 최종 게시 책임을 대신하지 않습니다.
