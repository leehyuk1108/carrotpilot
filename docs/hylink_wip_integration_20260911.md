# Hylink ↔ carrotpilot/wip integration — 2026-09-11

## 2026-09-30 IP:1108 업로드 진단 추가

키와 복사 버튼 아래에 **차량 정보 업로드** 상태를 추가했다. 별도 설정은 없다.
이 절은 아래 과거 기록의 “키와 키 복사 버튼만 표시” 설명보다 우선한다.

- 키가 준비됐다는 이유만으로 업로드 성공으로 표시하지 않는다. `/api/telemetry`가
  정상 HTTP 응답과 `{"ok": true}`를 반환해야 최근 성공 시각을 기록한다.
- 최근 성공/시도 시각, 첫 전송 대기, 전송 중, 지연, 송신 프로그램 동작 미확인,
  등록 미완료, 전송 중지, 설정/기기 ID 불일치를 구분한다.
- 실패 시 인증(401/403), 요청 제한(429), 서버 오류(5xx), DNS, TLS, 시간 초과,
  연결 실패, 데이터 생성/JSON 형식 오류, 저장 완료 응답 미확인을 구분해서
  한국어 원인과 다음 확인 방법을 표시한다. 최초 등록의 409 충돌도 따로 안내한다.
  “진단 정보”에는 프로그램 상태, 코드, 최근 오류 시각, HTTP 상태, 재시도 대기를 표시한다.
- 초기화/데이터 생성 단계에서 종료되는 오류도 기록한 뒤 기존처럼 manager에 복구를 맡긴다.
  전송 성공 후 로컬 GPS 캐시 쓰기만 실패했다면 업로드 실패가 아니라 별도 경고로 표시한다.
- 이전 성공 기록이 있어도 프로세스 종료, 45초 넘게 끊긴 heartbeat, 재부팅 후 새 전송
  미확인 상태를 현재 정상이라고 표시하지 않는다. 상태 조회 실패 시 화면의 이전 정상 표시도 지운다.

### 자원과 접근 범위

- `/api/health`는 로컬 기록만 읽는다. 페이지가 보일 때 5초마다 조회하며 숨겨지면 멈춘다.
  진단 화면 때문에 Cloud 요청/업로드 횟수가 늘어나지 않는다. 기존 페이지 진입의
  `/api/connect` 자동 연결 동작은 유지하며, 상태 조회 GET은 연결을 다시 활성화하지 않는다.
- 같은 물리 LAN 및 `X-Hylink-Request` 검사를 유지한다. 오프로드 진단 페이지는
  guard가 불건전할 때도 읽을 수 있지만, 키 조회/활성화는 기존의 더 엄격한 guard 조건을
  그대로 요구한다. 온로드에서는 페이지/진단 API를 거절하며 manager의 1108 서버 기동 조건도 그대로다.
- heartbeat는 `/dev/shm/hylink/telemetry.json`에 15초 간격과 전송 시도/결과 시점에 기록한다.
  재부팅용 `/data/hylink/telemetry_history.json`은 결과가 바뀔 때 또는 같은 결과가
  5분 이상 지속될 때의 완료 시도만 저장한다. 같은 오류의 프로세스 재시작도 이 제한을 유지한다.
  따라서 재부팅 뒤 과거 성공 기록은 최대 5분 정도 덜 최신일 수 있으며 화면에서 구분한다.
- 기록은 atomic replace 및 0600 권한을 사용한다. 진단 저장 실패가 전송을 중단시키지 않는다.
  기록에는 키/차량 위치/요청 본문/예외 원문을 넣지 않는다. 연결 설정의 해시는 서로 다른
  등록의 기록을 섞지 않기 위한 로컬 비교용이며 진단 API에는 내보내지 않는다.
- 주행 중 상태 정보 전송, 기존 백오프, 주차 기능 제한, 차량 제어 및 Panda 코드는 바꾸지 않았다.
  이 표시는 차량 상태 정보 전송용이며 사진·라이브·주행 기록 업로드 성공까지 인증하지 않는다.

### 검증 및 한계

- Hylink 단위/통합 테스트 **152개 통과**. 외부 HTTP를 차단한 상태에서 오류 주입,
  재부팅/프로세스 종료/오래된 기록, 저장 실패, 비밀정보 비노출, LAN/온로드 차단을 확인했다.
- `node openpilot/system/hylink/tests/check_health_page.cjs` 통과: 정상/오류/복구,
  실패 시 정상 표시 제거, 키 복사 두 경로, 읽기 전용 조회 및 숨겨진 페이지의 조회 중지.
- 실제 브라우저에서 합성 데이터만 사용하여 정상/401 실패 표시를 확인했고,
  밝은/어두운 모드 × 320/390/768/1100px × 기본/200% 글자 크기 **16개 조합**에서
  가로 넘침, 키 잘림, 복사 버튼 터치 크기를 검사했다. Python Ruff/컴파일, JS 문법 검사도 통과했다.
- 이 검증은 해당 사용자 콤마의 실제 고장 원인 확정이나 원격 설치를 의미하지 않는다.
  반복 재부팅에도 업로드가 멈추는 현상은 변경 설치 후의 진단/기기 로그로 확인해야 한다.
  GitHub에 코드가 올라가도 콤마에서 해당 버전으로 업데이트해야 새 진단 화면이 적용된다.
  실기기 설치/업로드 복구 확인은 별도이며 이 코드 검증에 포함되지 않는다.

## 2026-09-15 주차 기능의 추가 전압 제한 제거

- 사용자 요청으로 Hylink 자체의 **11.5V 미만 주차 기능 차단**을 제거했다.
  전압 수치만으로 라이브·자동 사진·충격 감지/업로드·주행 기록 업로드·Hylink 원격
  터미널을 차단하지 않는다. 아래 9월 14일의 11.5V 변경 기록보다 이 변경이 우선한다.
- 시동·Params 일치, Panda 오류/하트비트, 상태 신선도, green 온도 상태와 80°C 미만
  조건은 유지한다. 온로드에는 기존대로 주차 기능을 종료한다.
- 상태 정보 전송 주기, 앱/APK, 차량 제어, Panda 펌웨어, 콤마 자체 전원 관리 코드와
  `DisablePowerDown` 등 전원 설정은 변경하지 않는다. 자체 종료는 기존 지연·설정 조건을
  그대로 따르며, 이번 변경으로 배터리 보호나 즉시 전원 차단을 보장하는 것은 아니다.
- 11.0V와 기존 경계 11.499/11.500V를 포함해 Hylink 전압 제한이 없음을 검사하고,
  저전압에서도 나머지 보호 조건이 유지되는 회귀 테스트를 추가했다. Hylink 테스트
  **119개**, 기존 전원 관리 테스트 **13개**, 수정 Python 파일의 Ruff 및 사용자 문서
  검사를 통과했다. 실제 콤마 설치와 전압 강하 실험은 수행하지 않았다.

## 2026-09-14 실기기 후속 변경

- 배포 검증 범위: `9bffd531`까지의 실제 기기에서 부팅·키 조회·주차 기능을 확인했다.
  `03c4a8ec`의 아래 빠른 재연결 보완은 자동 회귀 검사만 통과한 상태이며,
  보완 설치 후 실제 장시간/강제 단절 재시험은 아직 하지 않았다. 전원 종료 원인과
  간헐적 Cloud 연결 종료 원인은 미확정이다. 현재 GitHub 소스를 무오류 실차 인증으로
  해석하지 않는다. [Hylink Dev 1.6.4 APK와 검증 범위](https://github.com/leehyuk1108/carrotpilot/tree/hylink-app/downloads).
- 라이브 중계가 끊겼을 때 업로드용 30초 백오프를 곧바로 적용하던 동작을 보완했다.
  실제 시청 연결이 있었던 경우만 약 1·2·4초(+최대 20% jitter)로 3회 복구하고,
  계속 실패하면 기존 30→60→120→240→300초 정책으로 돌아간다. 재연결만 성공한
  짧은 반복 연결은 빠른 복구 횟수를 무한히 충전하지 않는다(60초 초과 연결만 초기화).
  시청자가 없는 연결과 SSH는 기존 재시도 정책을 유지한다. guard·하트비트·전압
  기준·온로드 종료는 변경하지 않았다. 원격 연결 종료 자체가 없어졌다는 의미는 아니다.
  종료 단계·예외 종류·close code·단조 시각만 기록하며 키·영상·명령 내용은 기록하지 않는다.
- 사용자 요청으로 **라이브의 수동 사진 촬영 및 10·30초 영상 저장을 제거**했다.
  Hylink Dev 1.6.4에서 버튼과 전송 코드를 없애고, 콤마에서도 촬영 명령 처리,
  영상 버퍼·녹화 스레드·아카이브 업로드 작업을 제거했다. 구형 앱의 촬영 명령도
  받지 않는다. 라이브 보기, 자동 정기/충격 사진, 기존 사진·영상 조회/재생은 유지한다.
  기존 Cloud 저장 파일은 삭제하지 않았다. 아래 과거 기록의 수동 저장 설명보다
  이 변경이 우선한다. 장시간 라이브의 간헐적 전송 끊김 원인까지 해결됐다는 뜻은 아니다.
- 사용자 요청으로 Hylink 주차 기능의 최소 전압을 **11.5V**로 변경했다.
  11.499V는 차단하고 11.500V부터 허용한다. 시동·Panda 오류·상태 신선도·온도
  조건과 기기 자체 절전/저전압 종료는 그대로다. 차량 배터리 사용 시 기존 12.0V
  기준보다 방전 여유가 줄어든다. 온로드 텔레메트리 주기는 변경하지 않는다.
- 콤마 4 실기기에서 AGNOS 19.6.3-carrot 설치·전체 빌드·재부팅을 확인했다.
  Panda 오디오 초기화의 미사용 SAI1 쓰기와 DFSDM CH3 설정 순서가
  `registerDivergent`를 일으키는 것을 발견해 `665cce72`에서 수정했다.
  수정 펌웨어 서명 일치와 재부팅 후 오류 목록이 비어 있음을 확인했다.
  CAN/차량 제어/안전 제한을 변경한 수정은 아니다.
- 아래 9월 11일의 실기기 미검증 기록은 당시 상태다. 실제 주행 및 주차 기능
  전체의 end-to-end 검증 여부는 항목별로 구분해야 하며, 부팅만으로 인증하지 않는다.

## 후속 구현: 간단한 IP:1108 + Hylink Dev 1.6.1

이 절은 위의 9월 14일 변경을 제외한 현재 동작이다. 아래 **초기 d7a21a5d 기록**은 당시의 범위이며,
키 페이지·충격 사진·원격 터미널이 없다는 이전 설명은 이 후속 구현으로 대체된다.

### 바로 연결하기

1. `leehyuk1108/carrotpilot:wip`의 이 변경을 설치하고 재부팅한다.
2. 시동을 끄고 콤마와 휴대폰을 같은 개인 Wi-Fi/핫스팟에 연결한다.
3. 휴대폰 브라우저에서 `http://콤마IP:1108`을 연다. **키와 키 복사 버튼만** 표시한다.
   페이지를 열면 자동 등록하고 정보 전송·라이브/사진·충격 감지·원격 터미널을 모두 활성화한다.
   기존 차량의 키는 유지한다. 사용자 요청에 따라 이전 기능 선택·동의 체크박스를 없앴다.
   별도 인증번호나 SSH 명령은 필요 없다. 최초 등록에는 콤마의 인터넷 연결이 필요하다.
4. 키를 복사해서 **Hylink → 차량 → Wayon Cloud 키**에 붙여 넣고 `키 확인 및 연결`을 누른다.
   미연결 홈에도 연결 버튼이 있다. 새 키는 서버 확인 후에만 저장하며 실패 시 기존 키를 유지한다.
5. 별도 기능 설정은 없다. 콤마 `설정 → 기기 → Hylink 정보 전송 → 중지`는 유지한다.
   이미 열린 페이지의 조회는 중지를 취소하지 않는다. 페이지를 다시 열면 모두 재활성화한다.

자동 활성화는 키 페이지의 같은 LAN·출처 검사된 POST에서만 이루어진다.
GET 요청/미리 읽기/부팅만으로 새 등록을 만들지 않는다. 등록 실패 시 같은 보류 키로 재시도하며,
등록 중 시동이 켜지면 활성화하지 않는다. 기본 활성화는 주차 기능의 보호 조건을 해제하지 않는다.

키는 위치·영상·원격 터미널의 접근 권한이다. **같은 LAN의 다른 사람도 키를 볼 수 있는 방식**을
사용자가 명시적으로 선택했다. 신뢰하는 개인 네트워크에서만 사용한다. HTTP이므로 암호화된 LAN 키 전달이 아니다.
실제 로컬 인터페이스 주소/서브넷을 확인하며 외부 IP, DNS Host, 다른 서브넷, VPN 인터페이스를 거부한다.
키 응답은 캐시하지 않고, 교차 출처 요청과 iframe을 차단하며 요청/키를 로그에 남기지 않는다.
서버 종료 후 열려 있던 페이지도 다음 확인에서 표시한 키를 지운다. 이미 복사된 키를 회수하는 기능은 아니다.

### 온로드와 오프로드

| 기능 | 온로드 | 오프로드 |
|---|---|---|
| 위치·속도·ACC/조향/CAN 경고·기기 상태 전송 | 유지 | 유지 |
| `IP:1108` 키 페이지 | 프로세스 종료 | 같은 LAN에서 사용 |
| 주차 상태 guard | 프로세스 종료 | 2 Hz, 네트워크 없음 |
| qlog 요약, 정기 사진, 충격 사진 | 프로세스 종료 | 전원/온도/상태 조건 충족 시 |
| 라이브 보기 | 프로세스 종료 | 연결 후 기본 활성화 |
| 라이브 수동 사진/영상 저장 | 제거 | 제거 |
| IMU 충격 감지 | Hylink 감지 종료 | 연결 후 기본 활성화 |
| Hylink SSH 릴레이/셸 | 차단·종료 | 연결 후 기본 활성화, 최대 5분 |

Wayon/Sunnypilot의 `wayon_cloud_uploader` / `process_config`와 비교했다.
기본 heartbeat는 동일하게 **온로드 30초 / 오프로드 300초**, 주요 상태 변화는 최소 5초 / 15초 간격이다.
시동/운행 상태 전환은 정기 전송 시각을 기다리지 않는다. 전송은 저빈도 `deviceState`를 기준으로 깨어나며,
100 Hz carState 대기 루프나 온로드 사진/qlog 처리를 추가하지 않는다. 휴대폰의 전경 자동 갱신은 기존 30초 정책을 유지한다.
따라서 실시간 CAN 중계나 모든 짧은 경고의 영구 기록을 뜻하지 않는다. 원본 주행 로그는 별도다.

### 이번에 완성한 연결 경로

- 키 페이지 자동 연결 → 전체 기본 활성화 → 기기 config → manager → Cloud → 앱 기능 상태 표시.
- 연결된 차량에서 충격 이벤트가 발생하면 감지 후 사진을 `/api/impact-media`로 연결한다.
  저장했던 동일 사진으로 네트워크 재시도하며, 오래된 이벤트에 새 사진을 붙이지 않는다.
  45초 캡처 창, 카메라 경합/시동 전환/오류에서는 촬영 실패로 기록한다. 충격 순간/이전 영상은 아니다.
  앱의 충격 상세에서 내려받은 목록에 있는 연결 사진을 직접 열 수 있다.
- 원격 SSH는 기존 Athena, `SshEnabled`, `GithubSshKeys`, 시스템 SSH 서비스를 건드리지 않는 별도 구현이다.
  Cloud가 인증한 단기 공개키만 private tmpfs 파일에 저장하고 localhost:12222의 별도 sshd에 전달한다.
  transient systemd unit의 5분 상한과 guard/relay heartbeat를 사용한다. PAM 세션 분리는 사용하지 않아
  셸 자식들을 같은 cgroup에서 회수한다. 비밀번호·TCP/agent/X11 forwarding은 허용하지 않는다.
  SSH 명령 자체는 시스템을 변경할 수 있다. 이번 기본값에는 SSH도 포함되므로 연결 키를 공유하지 않는다.
  원격 차량 조작 UI는 추가하지 않았다.
- AGNOS는 호스트 키를 `/data/etc/ssh`에 둔다. 기존 키가 없으면 별도 Hylink 호스트 키만 생성한다.
  기준 소스: [AGNOS sshd 설정](https://github.com/commaai/agnos-builder/blob/8f7207a4f083ce3215f893a01b74613c507cc4bc/userspace/root/etc/ssh/sshd_config),
  [AGNOS 사용자/SSH 패키지 설정](https://github.com/commaai/agnos-builder/blob/8f7207a4f083ce3215f893a01b74613c507cc4bc/userspace/base_setup.sh).
- Android 터미널에서 출력 대기가 입력 전송을 막던 executor 교착 구조를 분리했다.
  라이브 초기 카메라 준비 시간, 앱 백그라운드 종료, 오래된 인증 응답도 보완했다.

### 검증과 남은 경계

관련 Hylink/manager camera/Trailblazer 테스트 **144개**가 통과했다. 앞선 별도 manager 회귀 검사 **7개**도 통과했다.
실제 Worker 소스와의 오프라인 데이터 계약, APK 단위 테스트 **5개**/빌드/서명 일치,
브라우저 키 입력과 24개 화면 크기·텍스트 배치 조합을 검증했다.
간단한 키 페이지는 자동 연결·기존 키 유지·복사 API/HTTP 대체 경로·중지 후 키 지우기·실패 후 복구와
16개 화면 조합(4개 너비 × 기본/2배 텍스트 × 밝은/어두운 테마)을 검증했다.
복사 테스트는 시스템 클립보드를 바꾸지 않는 대체 구현으로 호출 값과 피드백을 검사했다.
운영 Cloud 서버와 Wayon/Sunnypilot, My Traverse New는 변경하지 않는다.
트레일블레이저 제어·Panda·안전 코드·AGNOS 부트 파일도 이 변경에서 수정하지 않는다.

전체 스위트의 기존 GM fingerprint 검사 실패와 Mac manager의 `TimezoneName: None != ''` 실패는 별도로 남아 있다.
후자는 변경 전 `d7a21a5d` process_config를 메모리에 복원한 비교 실행에서도 동일하게 재현됐다.
Mac에는 `/dev/shm/params`, `rednose.helpers.ekf_sym_pyx` 등 실제 기기 환경과 다른 준비 오류도 있다.
이를 실차 부팅 성공이나 새 Hylink 기능의 실기기 성공으로 바꾸어 보고하지 않는다.

**실차 부팅·카메라/IMU·SSH/systemd·주행 중 자원 회수는 아직 실차 미검증이다.**
USB 휴대폰도 최종 빌드 시점에는 연결되어 있지 않아 이번 APK 설치 검증은 별도다.
전원이 꺼진 콤마 깨우기/배터리 보호 해제, Firebase 백그라운드 푸시 알림,
모든 과거 주행 backfill, 개인 차량 명령은 제공하지 않는다. 충격은 앱의 수신 기록에서 확인한다.

---

## 초기 d7a21a5d 기록 (아래는 후속 구현 전 상태)

## 결론 / 배포 상태

Hylink Android 앱의 화면을 콤마 UI로 복사한 작업이 아니다.
기존 앱이 사용하는 Wayon Cloud 규격에 맞춰 **차량 쪽 데이터 송신기**를 추가했다.
앱 소스, My Traverse New, Wayon/Sunnypilot, 운영 Cloud 서버, 실제 차량은 변경하지 않았다.

- 기준 브랜치: `leehyuk1108/carrotpilot:wip`
- 작업 전 기준: `ba70f690945fba2f167ce37eccde5bd7144586ab`
- 참고한 Wayon 코드: `7faab119a1c079d74ddf1c59e9def4c9b4b4f29c`
- **실차 접근 불가. 실제 부팅, 주행, 무선망, 카메라/IMU 하드웨어 검증은 수행하지 못했다.**
- 기본값은 전체 연동 OFF. 명시적으로 등록한 차량만 데이터를 전송한다.
- 기본 등록 후에는 읽기 전용 상태·GPS·주행 기록만 사용한다.
- 주차 사진·라이브·충격 감지는 별도 OFF. 실차 검증 전에는 활성화하지 않는 것이 이 배포의 전제다.
- 따라서 이 커밋은 **오프라인 검증을 마친 선택적 연동 코드**이지, 모든 하드웨어 기능의 무오류 인증이나 전체 앱 기능 완성 선언이 아니다.

## 범위

| 기능 | 구현 | 기본 등록 후 |
|---|---|---|
| 현재 차량 위치, 속도, ACC/조향 경고, 기기 온도·전원·연결 | 읽기 전용 cereal 구독 → `/api/telemetry` | 사용 |
| 종료된 최신 주행 경로·정차 품질·활성 시간 | qlog 요약 → `/api/trips` | 조건이 맞는 비주행 때 사용 |
| 홈 지도 차량만 / 전체 지도 내 위치+차량 | 기존 Android 앱 동작 그대로 | 휴대폰 위치 권한은 앱에서 별도 |
| 주차 사진, 광각+실내 라이브, 사진/10·30초 클립 저장 | 기존 앱의 Wayon 미디어 규격 | 별도 OFF, 하드웨어 미검증 |
| 주차 IMU 충격 이벤트 | 센서 관찰 → `/api/impact` | 별도 OFF, 오탐률 미검증 |
| 충격 직후 사진을 해당 이벤트에 연결 | 이번 버전에는 없음 | 충격 이벤트와 정기 사진은 별개 |
| Hylink 원격 SSH·브랜치 설치 | 이번 포트에서 제외 | 앱의 해당 버튼은 사용할 수 없음 |
| 개인 차량 도어 제어·Firebase 조회·앰비언트/CAN 명령 | 제외 | 사용 안 함 |
| BSM/TPMS/연료·충전 데이터 추정 | 하지 않음 | 없는 데이터를 정상 수치로 만들어 보내지 않음 |

**원격 SSH를 제외한 이유:** 기존 구현은 SSH 서비스 자동 시작, 임시 공개키 삽입,
Athena와의 PID 공유를 포함했다. 실차 검증 없는 이번 변경에서는 이 권한 확장을 가져오지 않았다.
기존 comma Athena/SSH 설정은 보존한다. 향후 추가하려면 별도 접근 제어·키 회수 검증이 필요하다.

## 기존 주행 코드 보호

다음 생산 코드는 기준 커밋과 동일하다.

- `opendbc_repo/opendbc/car`: 트레일블레이저 카운터·체크섬·버튼·가감속/조향 수정 포함
- `opendbc_repo/opendbc/safety`, `panda`: Panda 안전 정책 및 펌웨어
- `openpilot/selfdrive`: 제어·모델·UI
- `openpilot/system/camerad`, `loggerd`, `sensord`: 기존 하드웨어 구현
- 부트/AGNOS/모델 설치 스크립트 및 의존성 목록

기존 생산 코드 변경은 `system/manager/process_config.py`의 연동 프로세스 추가와
카메라·센서의 **명시적 비주행 사용 조건 추가**뿐이다.
등록하지 않으면 기존 조건과 동일하다. 주행 때 기존 camerad/sensord 조건도 그대로 참이다.
이것은 하드웨어 경쟁이 전혀 없다는 증명이 아니다. 미검증 미디어/IMU 기능을 OFF로 둔 이유다.

기존 manager 테스트의 `Params.put("정수키", "문자열")` 7곳도 현재 typed Params API에 맞는
`put_int`로 바꿨다. 생산 설정값이나 테스트 기대 결과는 바꾸지 않았다.

## 충돌 방지 구조

`openpilot/system/hylink/` 안에 격리했다.

| 프로세스 | 역할 / 제한 |
|---|---|
| hylink_guard | 네트워크 없이 0.5초마다 비주행 허용 상태 게시 |
| hylink_telemetry | 가벼운 상태 송신. 사진/로그 읽기로 막히지 않음 |
| hylink_worker | 낮은 CPU 우선순위의 비주행 qlog/사진/충격 이벤트 업로드 |
| hylink_live / hylink_relay | 인증된 Cloud ↔ localhost 영상 연결, 비주행만 |
| hylink_encoderd | 기존 `encoderd --stream` 사용. onroad에서 차단 |
| hylink_impact | 비주행 IMU 관찰만, CAN 읽기/송신 없음 |

### 시동을 켜면

1. fresh `pandaStates`에서 ignitionLine/ignitionCan을 확인하거나 manager가 onroad가 되면 차단한다.
2. 비주행 worker/live/relay/impact/stream encoder는 manager에서 SIGKILL로 중지한다.
   막힌 네트워크 호출이나 업로드 스레드가 정상 종료를 지연시키지 않도록 했다.
3. camerad는 Hylink가 직접 시작·종료하지 않는다. 기존 manager가 계속 소유한다.
4. `IsTakingSnapshot`, `IsOnroad`, `AthenadPid`, 운전 설정 Params를 Hylink가 조작하지 않는다.

차단은 소프트웨어 주기와 OS 스케줄링에 따른다. 하드 실시간이나 0ms 전환을 보장하지 않는다.
카메라의 offroad→onroad 연속 사용 및 encoder 자원 회수 타이밍은 실차 미검증 항목이다.

### 상태를 모르면

- deviceState/pandaStates가 없거나 invalid, 2.5초 초과, Panda unknown/heartbeat lost/fault이면 비주행 기능 차단.
- 점화와 Params가 모순되거나 green 이외 온도 상태, maxTempC 80도 이상이면 차단.
  Hylink의 별도 11.5V 제한은 2026-09-15에 제거했으며 기기 자체 전원 관리는 유지한다.
- 카메라 요청 파일은 살아 있는 PID와 2.5초 heartbeat가 모두 필요하다.
- 사진/라이브는 flock으로 하나만 카메라 요청을 소유한다. 프로세스가 죽으면 OS가 lock을 회수한다.
- 운전자 카메라 보기/기존 snapshot 요청, 광각 비활성화, C3X Lite에서는 미디어를 허용하지 않는다.
- 오래된 carState를 현재 속도 0/정상 CAN으로 표시하지 않는다.
- 오래된 GPS는 `fresh:false`, 마지막 위치임을 명시한다. 위도/경도 0 자체는 유효한 값이다.
- wip에 없는 Panda 좌/우 제어 허용 필드를 억지로 읽지 않는다.
- Navdy가 없는 차량을 Navdy 연결 고장으로 판정하지 않는다.

### 부하 / Cloud 예산

- 상태 heartbeat: 주행 30초, 비주행 300초. 주요 상태 변화는 각각 최소 5초/15초 간격.
- 실패 재시도: 채널별 30→60→120→240→최대 300초, jitter. 네트워크 오류가 운전 프로세스로 전파되지 않는다.
- 주행 로그: 최근 24시간의 최신 route만, 종료 후 45초 유예, 최대 4시간/240 segments.
  qlog만 읽으며 압축 파일당 32MiB, 해제 후 64MiB, 처리 45초 제한.
  누락 segment/오류/취소는 완료 업로드로 기록하지 않는다. 점 수 최대 720.
  제한에 걸린 route는 기록에 안 나타날 수 있으며, 모든 과거 주행 backfill은 제공하지 않는다.
- 사진: 별도 활성화 시 성공 간격 1시간. 광각·실내 촬영 및 Cloud 전송을 포함한다.
- 라이브: localhost 8765만 listen, Cloud 인증 relay만 연결. 세션 최대 5분, 앱 heartbeat 12초.
- 라이브 수동 저장용 버퍼·녹화 스레드·아카이브 업로드는 제거했다.
  기존 클립 파일의 앱 다운로드/재생은 유지한다. 자동 주차 사진 업로드와는 별개다.
- 주차 충격: 30초 안정화 뒤 감지, 큐 최대 64건. CAN 기반 도어 잠금/충격 원인 추정 없음.
- 기기 절전·저전압 종료를 해제하거나 강제 깨우지 않는다. 꺼진 기기를 원격으로 켜는 기능은 아니다.

## 등록 / 비활성화

차량 소유자가 위치·차량 데이터 전송에 동의한 경우에만, **차량의 wip 저장소 안에서**
해당 차량 Python 런타임으로 실행한다. 일반 설치 경로는 `/data/openpilot`이다.

```sh
cd /data/openpilot
python3 -m openpilot.system.hylink.setup enable
python3 -m openpilot.system.hylink.setup status
python3 -m openpilot.system.hylink.setup show-key
```

`show-key` 결과는 해당 소유자의 Hylink 앱에만 입력한다. Git/공개 로그/메신저 방에 올리지 않는다.
앱 데이터·로그인을 바꾸거나 서버의 다른 차량 키를 덮어쓰지 않는다.

저장 위치는 `/data/hylink/config.json`, 권한 0600.
자동 등록이나 인증 없는 1108번 LAN 키 표시 페이지는 없다.
등록 중 네트워크가 끊겨도 같은 pending 키로 재시도한다.

### 같은 콤마가 예전에 Wayon에 등록되어 있던 경우

서버가 HTTP 409를 반환하면 새 키로 덮어쓰지 않는다.
로컬 `/data/wayon_cloud/config.json`에 **동일한 DongleId의 기존 키**가 있다면:

```sh
python3 -m openpilot.system.hylink.setup import-legacy
python3 -m openpilot.system.hylink.setup enable
```

기존 파일은 보존하고 ID/키만 복사한다. 개인 차량 명령·설정은 복사하지 않는다.
다른 콤마의 config, 등록 완료된 Hylink 키의 덮어쓰기는 거절한다.
기존 키가 없으면 소유자의 키 복구/재등록 절차가 먼저 필요하다.

### 즉시 되돌리기

```sh
python3 -m openpilot.system.hylink.setup disable
```

다음 manager 점검 때 Hylink 프로세스가 중단된다. 키와 서버 기록은 삭제하지 않는다.
기존 주행 코드/설정 복구나 하드 리셋이 필요한 구조가 아니다.

`media-on/off`, `impact-on/off` 명령도 있지만 **이번 배포에서는 OFF 유지**를 권장한다.
실차 카메라/IMU/전환/주차 전력 검증 없이 이 명령을 실행해서 모든 기능이 검증됐다고 간주하지 않는다.

## 실행한 검증

실행 환경: macOS arm64, 이 wip의 Python 3.12/cereal/Params, 실제 차량 연결 없이 수행.
테스트의 HTTP 요청은 차단했고 서버 검증은 가짜 D1/KV와 실제 Worker 코드를 사용했다.

| 검증 | 결과 |
|---|---|
| Hylink 단위·통합 테스트 | 80개 통과 |
| 기존 manager/camera 조건 테스트 중 비기동 테스트 | 9개 통과 |
| 트레일블레이저 롱컨 회귀 테스트 | 42개 통과 |
| 미등록 상태의 기존 manager와 변경 후 조건 비교 | 1,440건 일치 |
| 현재 cereal → 새 송신기 → 실제 Wayon Worker → 앱 `/api/state` | 오프라인 계약 검증 통과 |
| 새 모듈 import / compileall / Ruff / git diff --check | 통과 |
| 전체 GM car 테스트 | 64 통과, 아래 기존 fingerprint 8 실패 |
| 전체 GM C safety 테스트 | 아래 C/Python 테스트 ABI 불일치로 완료 못 함 |

### 숨기지 않은 기존 테스트 문제

1. 기준 커밋의 원본 values/fingerprints/test 클래스를 별도 Python 프로세스에서 직접 읽어
   실행해도 다음 8 fingerprint 테스트가 동일하게 실패했다:
   VOLT_CC, BOLT_CC, YUKON_CC, CT6_CC, TRAILBLAZER_CC, MALIBU_CC, XT5_CC, TRAX.
   빈 목록 또는 카메라 진단 주소 0x24B가 없는 목록을 기존 테스트가 거절한다.
   이 작업에서 실제 차량 식별 테이블을 추정으로 바꾸지 않았다.
2. 추가 GM C safety 실행은 로컬 시험 중 segfault로 중단됐다.
   현재 `opendbc/safety/safety.h`의 `safety_fwd_hook(CANPacket_t *)`와
   기존 `tests/libsafety/libsafety_py.py`의 `safety_fwd_hook(int, int)` 선언이 다르다.
   이 시험 하네스 문제를 차량 firmware 오류라고 단정하지 않으며, C safety 전체 통과를 주장하지 않는다.
   해당 생산 코드/기존 시험 바이너리는 이 작업에서 수정하지 않았다.
3. 기존 manager 시험 3건은 typed Params에 문자열로 정수를 쓰는 시험 코드 때문에 실패했다.
   `put_int`로 고친 뒤 선택한 9건은 모두 통과했다.
4. manager 전체 prepare/실제 프로세스 전체 기동/실차 부팅은 실행하지 않았다.
   카메라·센서 실출력, 장시간 부하·배터리 소모, 실제 운전 중 경고 없음은 미확인이다.

### 재현 명령

저장소 런타임에서:

```sh
python3 -m pytest -n 0 -q openpilot/system/hylink/tests
python3 -m pytest -n 0 -q openpilot/system/manager/test/test_camera_config.py openpilot/system/manager/test/test_manager.py -k 'not manager_prepare and not set_params_with_default_value and not clean_exit and not startup_time'
python3 -m pytest -o addopts='' -q opendbc_repo/opendbc/car/gm/tests/test_gm.py::TestTrailblazerLongitudinalIntegrity
ruff check openpilot/system/hylink openpilot/system/manager/test/test_manager.py
python3 -m compileall -q openpilot/system/hylink openpilot/system/manager/process_config.py
git diff ba70f690945fba2f167ce37eccde5bd7144586ab -- opendbc_repo panda openpilot/selfdrive openpilot/system/loggerd openpilot/system/camerad openpilot/system/sensord
```

`tests/verify_cloud_contract.mjs`는 stdin으로 시험 telemetry JSON을 받고 첫 인자로
실제 Wayon `worker.js` 경로를 받는다. 외부 fetch는 차단하며 운영 서버나 실제 차량 데이터를 수정하지 않는다.

## 실차 접근이 가능해질 때 남은 확인

1. 기본 OFF에서 기존처럼 부팅·ACC/조향이 동작하는지.
2. 데이터 연동만 켜고 네트워크 단절/복구, 오래된 GPS 표시, 주행 후 trip 업로드.
3. 그 뒤에만 주차 미디어를 별도로 검증: 사진/라이브/클립, 카메라 사용 중 시동 켜기,
   앱 강제 종료, 통신 단절, 광각 OFF/드라이버 뷰 충돌.
4. 마지막으로 주차 IMU/오탐/소모 전력과 다음 주행의 sensord 상태 확인.

실차 확인 불가라는 제약을 코드 검증으로 없앤 것처럼 보고하지 않는다.
