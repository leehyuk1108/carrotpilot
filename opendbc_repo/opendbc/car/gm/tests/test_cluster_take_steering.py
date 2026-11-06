"""GmClusterTakeSteering: PSCMStatus copy to the camera. Real GM codec and controller; no device I/O."""
from collections import defaultdict

import pytest

from opendbc.car import Bus, structs
from opendbc.car.gm import gmcan
from opendbc.car.gm.carcontroller import cluster_take_steering_requested
from opendbc.car.gm.values import CAR, CanBus, DBC
from opendbc.car.gm.tests.test_display_speed import MemoryParams, runtime, build_cp, controller_state  # noqa: F401

PSCM = 0x184


def pscm_checksum(dat):
  # 0x184 PSCMStatusChecksum: (b0+b1+b2+b3+b6+b7 + counter + 48) & 0x3FF, counter in byte4 bits6-3
  return (dat[0] + dat[1] + dat[2] + dat[3] + dat[6] + dat[7] + ((dat[4] >> 3) & 0xF) + 48) & 0x3FF


def frame_checksum(dat):
  return ((dat[4] & 0x3) << 8) | dat[5]


def hands_on_bit(dat):
  return (dat[2] >> 5) & 1


@pytest.mark.parametrize("mode,level,expected", [
  (0, 0, False), (0, 1, False), (0, 2, False), (0, 3, False),
  (1, 0, False), (1, 1, False), (1, 2, True), (1, 3, True),
  (2, 0, False), (2, 1, True), (2, 2, True), (2, 3, True),
  (3, 3, False), (-1, 3, False),
])
def test_request_truth_table(mode, level, expected):
  hud = structs.CarControl.HUDControl(driverMonitoringAlert=level)
  assert cluster_take_steering_requested(mode, hud) is expected


def car_pscm_status(packer, counter, hands_on, delivered, driver, status):
  """A valid car-side PSCMStatus as CarState would hold it (signals incl. a correct checksum)."""
  values = {
    "HandsOffSWDetectionMode": 1,
    "HandsOffSWlDetectionStatus": hands_on,
    "LKATorqueDeliveredStatus": status,
    "LKADriverAppldTrq": driver,
    "LKATorqueDelivered": delivered,
    "LKATotalTorqueDelivered": delivered + driver,
    "RollingCounter": counter,
    "PSCMStatusChecksum": 0,
  }
  _, dat, _ = packer.make_can_msg("PSCMStatus", 0, values)
  values["PSCMStatusChecksum"] = pscm_checksum(dat)
  _, dat, _ = packer.make_can_msg("PSCMStatus", 0, values)
  assert frame_checksum(dat) == pscm_checksum(dat)
  return values, dat


@pytest.mark.parametrize("hands_on", [0, 1])
@pytest.mark.parametrize("hands_off", [False, True])
@pytest.mark.parametrize("counter", range(16))
@pytest.mark.parametrize("torque", [(0., 0.), (1.5, -0.3), (-2.4, 0.8), (10.23, 10.23), (-10.24, -10.24)])
def test_pscm_copy_bit_and_checksum(runtime, hands_on, hands_off, counter, torque):
  packer = runtime.CANPacker(DBC[CAR.CHEVROLET_TRAVERSE][Bus.pt])
  values, car_dat = car_pscm_status(packer, counter, hands_on, torque[0], torque[1], 1)
  address, dat, bus = gmcan.create_pscm_status(packer, CanBus.CAMERA, values, hands_off=hands_off)
  assert (address, bus) == (PSCM, CanBus.CAMERA)
  assert hands_on_bit(dat) == (0 if hands_off else 1)
  assert frame_checksum(dat) == pscm_checksum(dat)
  # only the hands-on bit and the checksum may differ from the car frame
  for i, (a, b) in enumerate(zip(dat, car_dat, strict=True)):
    if i == 2:
      assert a & ~0x20 == b & ~0x20
    elif i == 4:
      assert a & ~0x3 == b & ~0x3
    elif i != 5:
      assert a == b


def test_default_copy_matches_previous_behavior(runtime):
  packer = runtime.CANPacker(DBC[CAR.CHEVROLET_TRAVERSE][Bus.pt])
  for counter in range(16):
    for hands_on in (0, 1):
      values, _ = car_pscm_status(packer, counter, hands_on, 0.7, -0.2, 1)
      _, dat, _ = gmcan.create_pscm_status(packer, CanBus.CAMERA, values)
      assert hands_on_bit(dat) == 1
      assert frame_checksum(dat) == pscm_checksum(dat)


class ModeParams(MemoryParams):
  mode = 0

  def __init__(self, *args):
    super().__init__(*args)
    self.values["GmClusterTakeSteering"] = ModeParams.mode


def run_controller(runtime, monkeypatch, mode, levels, hands_on=1):
  monkeypatch.setattr(ModeParams, "mode", mode)
  monkeypatch.setattr(runtime.controller, "Params", ModeParams)
  cp = build_cp(runtime)
  controller = runtime.controller.CarController(DBC[CAR.CHEVROLET_TRAVERSE], cp)
  packer = runtime.CANPacker(DBC[CAR.CHEVROLET_TRAVERSE][Bus.pt])
  state = controller_state()
  cc = structs.CarControl(enabled=True, latActive=True)
  frames = []
  others = []
  for tick, level in enumerate(levels):
    values, _ = car_pscm_status(packer, tick % 16, hands_on, 0.5, 0.1, 1)
    state.pscm_status = defaultdict(int, values)
    cc.hudControl.driverMonitoringAlert = level
    _, sends = controller.update(cc.as_reader(), state, 1_000_000_000 + tick * 10_000_000)
    pscm = [(d, b) for a, d, b in sends if a == PSCM]
    frames += [(tick, level, d, b) for d, b in pscm]
    others.append([(a, d, b) for a, d, b in sends if a != PSCM])
  return frames, others


@pytest.mark.parametrize("mode", [0, 1, 2])
@pytest.mark.parametrize("hands_on", [0, 1])
def test_controller_only_changes_camera_pscm_bit(runtime, monkeypatch, mode, hands_on):
  levels = [0] * 60 + [1] * 60 + [2] * 60 + [3] * 60 + [0] * 60
  frames, others = run_controller(runtime, monkeypatch, mode, levels, hands_on)
  _, baseline_others = run_controller(runtime, monkeypatch, 0, levels, hands_on)
  assert others == baseline_others                 # every other message is byte-identical to mode 0
  assert len(frames) == len(levels) // 10          # still 10 Hz, camera bus only
  for tick, level, dat, bus in frames:
    assert bus == CanBus.CAMERA
    assert frame_checksum(dat) == pscm_checksum(dat)
    expect_off = (mode == 1 and level >= 2) or (mode == 2 and level >= 1)
    assert hands_on_bit(dat) == (0 if expect_off else 1), (tick, level)


def test_mode_is_read_live(runtime, monkeypatch):
  monkeypatch.setattr(ModeParams, "mode", 0)
  monkeypatch.setattr(runtime.controller, "Params", ModeParams)
  controller = runtime.controller.CarController(DBC[CAR.CHEVROLET_TRAVERSE], build_cp(runtime))
  packer = runtime.CANPacker(DBC[CAR.CHEVROLET_TRAVERSE][Bus.pt])
  state = controller_state()
  cc = structs.CarControl(enabled=True, latActive=True)
  cc.hudControl.driverMonitoringAlert = 2
  bits = []
  for tick in range(200):
    if tick == 100:
      ModeParams.mode = 1
    values, _ = car_pscm_status(packer, tick % 16, 1, 0., 0., 1)
    state.pscm_status = defaultdict(int, values)
    _, sends = controller.update(cc.as_reader(), state, 1_000_000_000 + tick * 10_000_000)
    bits += [(tick, hands_on_bit(d)) for a, d, b in sends if a == PSCM]
  assert all(bit == 1 for tick, bit in bits if tick < 100)
  assert all(bit == 0 for tick, bit in bits if tick >= 100)   # picked up at the next 0.5 s refresh (tick 100)
