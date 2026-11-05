# 폰 실시간 주행 화면 (Traverse Live) — 2026-11-05

폰 앱이 콤마와 같은 네트워크(와이파이 또는 폰 핫스팟)에 있으면 주행 화면(도로·차선·주변 차량·브레이크등·깜빡이)을 실시간으로 그린다. 그리기는 전부 폰이 하고 콤마는 데이터만 보낸다.

## 콤마 쪽

- 기존 `ws://<콤마>:7000/ws/raw_multiplex?services=carState,selfdriveState,carControl,modelV2,radarState`
  - 당근웹이 이미 제공하던 원본 capnp 중계(conflate, 약 20 Hz). 변경 없음.
- 새 `ws://<콤마>:7000/ws/viz_can` (`openpilot/selfdrive/carrot/server/features/viz_can.py`)
  - cereal 서비스에 없는 GM 프레임만 100 ms마다 묶어 보낸다.
  - 보내는 프레임:
    - bus 1: 카메라 물체 0x420–0x42C / 0x440–0x44C(25 Hz), 차선 종류 0x350–0x356, 레이더 0x460–0x47F(13.6 Hz), LHT 0x135
    - bus 0: 0x140, 0x142, 0x3FC, 0x1F5
    - bus 2: 0x510
  - 형식 v2: `b'VC'`, u8 버전=2, u16 개수, 그 뒤 14바이트 고정 레코드 (u8 bus, u16 addr, u16 age_ms, u8 len, data[8])
  - 접속자가 있을 때만 `can`을 구독한다(conflate 아님). 읽기 전용이다. cereal 발행이나 CAN 송신을 하지 않는다.
- 콤마 CPU 실측
  - 방법: 기기에 저장된 000001dc--6 rlog(초당 5,100 프레임)를 /tmp 스크립트로 처리했다. 오프로드 클럭 상태였다.
  - pycapnp로 모든 프레임을 읽는 방식: 50 ms 묶음 기준 코어 하나의 3.2%
  - `pack_raw()` 방식: 50 ms 묶음 1.5%, 실제로 쓰는 100 ms 묶음 0.9%
    - capnp 바이트를 numpy로 직접 읽는다. 버스·주소는 한 번에 조회표로 거르고, 필요한 프레임만 데이터를 읽는다.
    - 예상과 다른 구조면 pycapnp로 돌아간다.
    - 실제 rlog 3구간에서 pycapnp 결과와 바이트 단위로 같았다.
  - 그 밖의 비용: 기존 원본 중계로 나가는 양은 앱 1대당 약 205 KiB/s다(modelV2 172 KiB/s). 해석 없이 넘기는 복사 비용뿐이다.
- 테스트: `openpilot/selfdrive/carrot/server/tests/test_viz_can.py`
  - Mac에서는 `features/__init__.py`가 msgq 네이티브를 불러와 pytest 수집이 안 된다. 그래서 모듈 파일을 직접 불러 4개 테스트를 통과시켰다.

## 폰 앱 (레포 밖: `~/Documents/traverse-viz-app`)

- three.js 화면을 WebView로 감싼 APK다. 화면 코드는 `drive_viz` v7 영상 렌더러를 JS로 옮긴 것이다.
  - 차선 시간 필터, 정확한 점선 위치, 제자리 페이드 추적기, Kenney(CC0) 차량 모델
- capnp 해석은 당근웹 `js/realtime/raw_capnp.js`를 그대로 쓴다.
- 콤마 찾기:
  - 앱이 폰의 사설 IPv4 /24 대역을 7000번 포트로 훑고, 응답에 `carrot`가 있는 호스트에 붙는다.
  - 찾은 주소는 저장한다. 주소를 직접 넣을 수도 있다.
- Mac 개발용 가짜 콤마: `server/mock_comma.py`. rlog를 실시간으로 재생하며 위 두 엔드포인트와 같은 형식으로 보낸다.
- 폰 확인: Fold7 커버·안쪽 화면에서 가짜 콤마에 붙여 60 fps로 동작했다(JS 프레임당 약 0.7+1.8 ms). 실차와 콤마 연결 확인은 아직이다.
