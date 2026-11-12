"""Manual hold state, real CAN output and compiled Panda hooks; no vehicle I/O."""
# ruff: noqa: INP001
import ctypes
from collections import defaultdict
import itertools
from time import monotonic_ns

import pytest

from opendbc.car import Bus, gen_empty_fingerprint, structs
from opendbc.car.gm.manual_auto_hold import MANUAL_HOLD_CARS, ManualAutoHold, manual_hold_supported
from opendbc.car.gm.values import CAR, DBC, GMSafetyFlags
from opendbc.car.gm.tests.test_display_speed import MemoryParams, build_cp, controller_state, runtime as desktop_runtime


@pytest.fixture(scope="module")
def runtime():
  yield from desktop_runtime.__wrapped__()


def state(**changes):
  values = dict(canValid=True, gearShifter="drive", standstill=True, brakePressed=True, vEgo=0., vEgoRaw=0.)
  values.update(changes)
  return structs.CarState(**values)


def armed_hold():
  hold = ManualAutoHold()
  hold.update(state(), 80, False)
  assert hold.active and hold.command(state(), False) == 400
  return hold


@pytest.mark.parametrize("pedal", [0, 10, 79, 80, 100, 255])
@pytest.mark.parametrize("active", [False, True])
def test_strong_pedal_only_while_disengaged(pedal, active):
  hold = ManualAutoHold()
  hold.update(state(), pedal, active)
  assert hold.active == (pedal >= 80 and not active)


def test_arm_before_stop_but_never_brake_while_moving():
  hold = ManualAutoHold()
  hold.update(state(standstill=False, vEgo=1., vEgoRaw=1.), 90, False)
  assert hold.armed and not hold.active
  assert hold.command(state(standstill=False, vEgo=1., vEgoRaw=1.), False) == 0
  hold.update(state(), 30, False)
  assert hold.active


def test_release_before_stop_cancels_arming():
  hold = ManualAutoHold()
  hold.update(state(standstill=False, vEgo=1., vEgoRaw=1.), 90, False)
  hold.update(state(standstill=False, vEgo=.5, vEgoRaw=.5, brakePressed=False), 0, False)
  hold.update(state(), 30, False)
  assert not hold.active and not hold.armed


@pytest.mark.parametrize("peak,remains", [(10, False), (79, False), (80, True), (100, True)])
def test_initial_release_and_next_tap(peak, remains):
  hold = armed_hold()
  for _ in range(100):
    hold.update(state(), 20, False)
    assert hold.active
  for _ in range(100):
    hold.update(state(brakePressed=False), 0, False)
    assert hold.active
  hold.update(state(), 10, False)
  assert hold.active
  hold.update(state(), peak, False)
  hold.update(state(brakePressed=False), 0, False)
  assert hold.active == remains


@pytest.mark.parametrize("changes", [dict(gasPressed=True), dict(regenBraking=True), dict(parkingBrake=True),
  dict(gearShifter="park"), dict(gearShifter="reverse"), dict(gearShifter="neutral"), dict(canValid=False),
  dict(canTimeout=True), dict(accFaulted=True), dict(cruiseState={"enabled": True}), dict(standstill=False),
  dict(vEgo=.05), dict(vEgoRaw=.05), dict(vEgo=-.05), dict(vEgo=float("nan")), dict(vEgoRaw=float("inf"))])
def test_ineligible_state_clears(changes):
  hold = armed_hold()
  cs = state(**changes)
  assert hold.command(cs, False) == 0
  hold.update(cs, 0, False)
  assert not hold.active and not hold.armed


@pytest.mark.parametrize("pedal", [-1, 256, float("nan"), float("inf")])
def test_invalid_pedal_clears(pedal):
  hold = armed_hold()
  hold.update(state(), pedal, False)
  assert not hold.active


def test_engaged_state_cannot_carry_manual_hold():
  hold = armed_hold()
  assert hold.command(state(), True) == 0
  hold.update(state(), 100, True)
  hold.update(state(brakePressed=False), 0, False)
  assert not hold.armed and not hold.active


CAM_HOLD_CARS = [CAR.CHEVROLET_TRAILBLAZER, CAR.CHEVROLET_TRAX]


def hold_cp(runtime, candidate, alpha=True, pedal=False, camera=True):
  # Real camera-long cars report the stock camera (0x320) and ECMAcceleratorPos.
  fingerprint = gen_empty_fingerprint()
  fingerprint[0][0xBE] = 6
  if camera:
    fingerprint[2][0x320] = 3
  if pedal:
    fingerprint[0][0x201] = 6
  return runtime.interface.CarInterface.get_params(candidate, fingerprint, [], alpha, True, False)


@pytest.mark.parametrize("candidate", [CAR.CHEVROLET_TRAVERSE, *CAM_HOLD_CARS, CAR.CHEVROLET_TRAILBLAZER_CC,
                                       CAR.CHEVROLET_BOLT_EUV, CAR.CHEVROLET_EQUINOX, CAR.CHEVROLET_VOLT])
@pytest.mark.parametrize("alpha,pedal", [(True, False), (False, False), (True, True)])
def test_feature_flag_scope(runtime, candidate, alpha, pedal):
  cp = hold_cp(runtime, candidate, alpha, pedal)
  expected = candidate in MANUAL_HOLD_CARS and alpha and not pedal
  assert manual_hold_supported(cp) == expected
  assert bool(cp.safetyConfigs[0].safetyParam & GMSafetyFlags.MANUAL_AUTO_HOLD) == expected
  if expected:
    sdgm = GMSafetyFlags.HW_SDGM if candidate == CAR.CHEVROLET_TRAVERSE else 0
    assert cp.safetyConfigs[0].safetyParam == int(GMSafetyFlags.HW_CAM | GMSafetyFlags.HW_CAM_LONG |
                                                  GMSafetyFlags.MANUAL_AUTO_HOLD | sdgm)


@pytest.mark.parametrize("candidate", CAM_HOLD_CARS)
def test_camera_car_without_camera_cannot_hold(runtime, candidate):
  cp = hold_cp(runtime, candidate, camera=False)
  assert cp.safetyConfigs[0].safetyParam & GMSafetyFlags.NO_CAMERA
  assert not manual_hold_supported(cp)


@pytest.mark.parametrize("long_state", ["off", "stopping", "starting"])
def test_real_controller_brakes_only_no_set_resume(runtime, long_state):
  cp = build_cp(runtime)
  controller = runtime.controller.CarController(DBC[CAR.CHEVROLET_TRAVERSE], cp)
  controller.v_cruise_carrot.autoCruiseControl = 2
  cs = controller_state()
  cs.out = state(activateCruise=1)
  cs.manual_auto_hold = armed_hold()
  cc = structs.CarControl()
  cc.actuators.longControlState = long_state
  decoder = runtime.CANParser(DBC[CAR.CHEVROLET_TRAVERSE][Bus.pt], [("ASCMGasRegenCmd", 25)], 0)
  for tick in range(100):
    now = 1_000_000_000 + tick * 10_000_000
    out, packets = controller.update(cc.as_reader(), cs, now)
    assert all(addr != 0x1E1 for addr, _, _ in packets)
    for addr, data, bus in packets:
      if addr == 0x315:
        assert bus == 2 and len(data) == 5 and data[0] >> 4 == 0xA
        assert (-int.from_bytes(data[:2], "big")) & 0xFFF == 400
        assert out.brake == 400
      if addr == 0x2CB:
        decoder.update([(now, [(addr, data, bus)])])
        assert not decoder.vl["ASCMGasRegenCmd"]["GasRegenCmdActive"]
        assert decoder.vl["ASCMGasRegenCmd"]["GasRegenCmd"] == controller.params.INACTIVE_REGEN


def camera_controller_state():
  # Idle stock camera: its 0x2CB counter keeps arriving with authority off.
  cs = controller_state()
  cs.cam_ascm_2cb_counter_ts_nanos = 1
  cs.cam_ascm_2cb_counter_updated = True
  cs.cam_stock_long_active = False
  cs.cam_acc_status = defaultdict(int, ACCAlwaysOne=1, ACCAlwaysOne2=1)
  return cs


@pytest.mark.parametrize("candidate", CAM_HOLD_CARS)
@pytest.mark.parametrize("long_state", ["off", "stopping", "starting"])
def test_camera_controller_brakes_on_powertrain_no_set_resume(runtime, candidate, long_state):
  cp = hold_cp(runtime, candidate)
  controller = runtime.controller.CarController(DBC[candidate], cp)
  controller.v_cruise_carrot.autoCruiseControl = 2
  cs = camera_controller_state()
  cs.out = state(activateCruise=1)
  cs.manual_auto_hold = armed_hold()
  cc = structs.CarControl()
  cc.actuators.longControlState = long_state
  decoder = runtime.CANParser(DBC[candidate][Bus.pt], [("ASCMGasRegenCmd", 25)], 0)
  brakes = 0
  for tick in range(100):
    now = 1_000_000_000 + tick * 10_000_000
    cs.cam_ascm_2cb_counter = (tick // 4) % 4
    out, packets = controller.update(cc.as_reader(), cs, now)
    assert all(addr != 0x1E1 for addr, _, _ in packets)
    for addr, data, bus in packets:
      if addr == 0x315:
        brakes += 1
        assert bus == 0 and len(data) == 5 and data[0] >> 4 == 0xA
        assert (-int.from_bytes(data[:2], "big")) & 0xFFF == 400
        assert out.brake == 400
      if addr == 0x2CB:
        assert bus == 0
        decoder.update([(now, [(addr, data, bus)])])
        assert not decoder.vl["ASCMGasRegenCmd"]["GasRegenCmdActive"]
  assert brakes > 0


@pytest.mark.parametrize("candidate", CAM_HOLD_CARS)
@pytest.mark.parametrize("enabled,long_active", [(False, False), (True, True)])
@pytest.mark.parametrize("long_state", ["off", "pid", "stopping"])
def test_camera_no_manual_trigger_leaves_output_unchanged(runtime, candidate, enabled, long_active, long_state):
  cp = hold_cp(runtime, candidate)
  before_cp = structs.CarParams(**cp.to_dict())
  before_cp.safetyConfigs[0].safetyParam &= ~int(GMSafetyFlags.MANUAL_AUTO_HOLD)
  before = runtime.controller.CarController(DBC[candidate], before_cp)
  after = runtime.controller.CarController(DBC[candidate], cp)
  before_cs, after_cs = camera_controller_state(), camera_controller_state()
  after_cs.manual_auto_hold = ManualAutoHold()
  for cs in (before_cs, after_cs):
    cs.cam_stock_long_active = enabled
  cc = structs.CarControl(enabled=enabled, longActive=long_active)
  cc.actuators.longControlState = long_state
  cc.actuators.accel = -1.
  for tick in range(100):
    now = 1_000_000_000 + tick * 10_000_000
    for cs in (before_cs, after_cs):
      cs.cam_ascm_2cb_counter = (tick // 4) % 4
    old_out, old_frames = before.update(cc.as_reader(), before_cs, now)
    new_out, new_frames = after.update(cc.as_reader(), after_cs, now)
    assert old_out.to_dict() == new_out.to_dict() and old_frames == new_frames


@pytest.mark.parametrize("enabled,long_active", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("long_state", ["off", "pid", "stopping", "starting"])
def test_no_manual_trigger_leaves_output_unchanged(runtime, enabled, long_active, long_state):
  cp = build_cp(runtime)
  before_cp = structs.CarParams(**cp.to_dict())
  before_cp.safetyConfigs[0].safetyParam &= ~int(GMSafetyFlags.MANUAL_AUTO_HOLD)
  before = runtime.controller.CarController(DBC[CAR.CHEVROLET_TRAVERSE], before_cp)
  after = runtime.controller.CarController(DBC[CAR.CHEVROLET_TRAVERSE], cp)
  before_cs, after_cs = controller_state(), controller_state()
  after_cs.manual_auto_hold = ManualAutoHold()
  cc = structs.CarControl(enabled=enabled, longActive=long_active)
  cc.actuators.longControlState = long_state
  cc.actuators.accel = -1.
  for tick in range(100):
    now = 1_000_000_000 + tick * 10_000_000
    old_out, old_frames = before.update(cc.as_reader(), before_cs, now)
    new_out, new_frames = after.update(cc.as_reader(), after_cs, now)
    assert old_out.to_dict() == new_out.to_dict() and old_frames == new_frames


@pytest.mark.parametrize("candidate", [CAR.CHEVROLET_TRAVERSE, *CAM_HOLD_CARS])
def test_real_interface_update_apply_and_freshness(runtime, monkeypatch, native, candidate):
  monkeypatch.setattr(MemoryParams, "put_int", MemoryParams.put_int_nonblocking, raising=False)
  cp = build_cp(runtime) if candidate == CAR.CHEVROLET_TRAVERSE else hold_cp(runtime, candidate)
  safety = hold_safety(native, cp.safetyConfigs[0].safetyParam, 2 if candidate == CAR.CHEVROLET_TRAVERSE else 0)
  ci = runtime.interface.CarInterface(cp)
  ci.CS.update(ci.can_parsers)
  packer = runtime.CANPacker(DBC[candidate][Bus.pt])
  drive = next(k for k, v in ci.CS.shifter_values.items() if v == "D")
  values = {"ECMPRDNL2": {"PRNDL2": drive}, "ECMEngineStatus": {"BrakePressed": 1},
            "ECMAcceleratorPos": {"BrakePedalPos": 80}, "BCMDoorBeltStatus": {"LeftSeatBelt": 1}}
  cc = structs.CarControl()

  def packets():
    return [packer.make_can_msg(msg.name, parser.bus, values.get(msg.name, {}))
            for parser in ci.can_parsers.values() for msg in parser.message_states.values()]
  for tick in range(5):
    now = monotonic_ns()
    can_frames = packets()
    for addr, data, bus in can_frames:
      if bus == 0 and addr in (0x34A, 0xC9, 0x1C4, 0xBE):
        assert rx(safety, addr, data)
    ret = ci.update([(now, can_frames)])
    assert ret.canValid and not ret.canTimeout
    _, sends = ci.apply(cc.as_reader(), now, model_v2="model", radar_state="radar")
    for addr, data, bus in sends:
      if addr in (0x315, 0x2CB):
        accepted = safety.gm_test_frame(1, addr, bus, data, len(data))
        assert accepted, (tick, addr, can_frames)
      if addr == 0x315 and tick:
        assert bus == safety.bus and (-int.from_bytes(data[:2], "big")) & 0xFFF == 400
  assert ci.CS.out.brakeHoldActive and ci.CS.manual_auto_hold.active
  assert ci.CS.modelV2 == "model" and ci.CS.radarState == "radar"
  values["ECMEngineStatus"]["BrakePressed"] = 0
  values["ECMAcceleratorPos"]["BrakePedalPos"] = 0
  now = monotonic_ns()
  assert ci.update([(now, packets())]).brakeHoldActive
  ci.apply(cc.as_reader(), now)
  assert ci.CS.out.brakeHoldActive
  ci.apply(cc.as_reader(), now + 500_000_001)
  assert not ci.CS.out.brakeHoldActive and not ci.CS.manual_auto_hold.active


def test_trailblazer_hold_drops_without_camera_references(runtime, monkeypatch):
  # The Trailblazer only sends 0x315 on the stock camera's 0x2CB cycle. Without
  # fresh 0x2CB/0x370 references the hold must clear, not show while no brake is
  # sent, and must not resume from the old press once the references return.
  monkeypatch.setattr(MemoryParams, "put_int", MemoryParams.put_int_nonblocking, raising=False)
  clock = [monotonic_ns()]
  monkeypatch.setattr(runtime.interface, "monotonic_ns", lambda: clock[0])
  candidate = CAR.CHEVROLET_TRAILBLAZER
  ci = runtime.interface.CarInterface(hold_cp(runtime, candidate))
  ci.CS.update(ci.can_parsers)
  packer = runtime.CANPacker(DBC[candidate][Bus.pt])
  drive = next(k for k, v in ci.CS.shifter_values.items() if v == "D")
  values = {"ECMPRDNL2": {"PRNDL2": drive}, "ECMEngineStatus": {"BrakePressed": 1},
            "ECMAcceleratorPos": {"BrakePedalPos": 80}, "BCMDoorBeltStatus": {"LeftSeatBelt": 1}}
  cc = structs.CarControl()

  def tick(refs=True):
    clock[0] += 10_000_000
    can_frames = [packer.make_can_msg(msg.name, parser.bus, values.get(msg.name, {}))
                  for parser in ci.can_parsers.values() for msg in parser.message_states.values()]
    if not refs:
      can_frames = [f for f in can_frames if not (f[2] == 2 and f[0] in (0x2CB, 0x370))]
    ret = ci.update([(clock[0], can_frames)])
    _, sends = ci.apply(cc.as_reader(), clock[0])
    brakes = [(-int.from_bytes(data[:2], "big")) & 0xFFF for addr, data, bus in sends if addr == 0x315]
    assert all(bus == 0 for addr, _, bus in sends if addr == 0x315)
    return ret, brakes

  for _ in range(10):
    tick()
  ret, brakes = tick()
  assert ret.brakeHoldActive and brakes == [400]
  values["ECMEngineStatus"]["BrakePressed"] = 0
  values["ECMAcceleratorPos"]["BrakePedalPos"] = 0
  for _ in range(5):
    ret, brakes = tick()
    assert ret.brakeHoldActive and brakes == [400]

  for i in range(30):
    ret, brakes = tick(refs=False)
    assert ret.canValid and brakes == []
    if i >= 20:  # 200 ms without references
      assert not ret.brakeHoldActive and not ci.CS.manual_auto_hold.active

  for _ in range(20):
    ret, brakes = tick()
    assert not ret.brakeHoldActive and brakes in ([], [0])
  assert brakes == [0]


@pytest.fixture(scope="module")
def native(runtime, tmp_path_factory):
  from opendbc.safety.tests.test_gm_sdgm import native_safety
  lib = native_safety.__wrapped__(tmp_path_factory)
  lib.set_timer.argtypes = [ctypes.c_uint32]
  return lib


HOLD_FLAGS = int(GMSafetyFlags.HW_CAM | GMSafetyFlags.HW_CAM_LONG | GMSafetyFlags.HW_SDGM | GMSafetyFlags.MANUAL_AUTO_HOLD)
CAM_HOLD_FLAGS = int(GMSafetyFlags.HW_CAM | GMSafetyFlags.HW_CAM_LONG | GMSafetyFlags.MANUAL_AUTO_HOLD)
HOLD_MODES = {"sdgm": (HOLD_FLAGS, 2), "camera": (CAM_HOLD_FLAGS, 0)}  # flags, friction brake bus


class HoldSafety:
  def __init__(self, lib, flags, bus):
    self.lib, self.flags, self.bus = lib, flags, bus

  def __getattr__(self, name):
    return getattr(self.lib, name)


def hold_safety(native, flags, bus):
  assert native.set_safety_hooks(structs.CarParams.SafetyModel.gm, flags) == 0
  native.init_tests()
  return HoldSafety(native, flags, bus)


@pytest.fixture(params=list(HOLD_MODES))
def safety(native, request):
  return hold_safety(native, *HOLD_MODES[request.param])


@pytest.fixture
def sdgm_safety(native):
  return hold_safety(native, *HOLD_MODES["sdgm"])


@pytest.fixture
def camera_safety(native):
  return hold_safety(native, *HOLD_MODES["camera"])


def rx(lib, addr, data):
  return lib.gm_test_frame(0, addr, 0, data, len(data))


def inputs(lib, pedal=80, pressed=True, speed=0, gas=0, cruise=0, order=(0, 1, 2, 3), omit=None):
  messages = [(0x34A, speed.to_bytes(2, "big") * 2 + b"\x00"),
              (0xC9, bytes([0, 0, 0, 0x20, 0, int(pressed), 0, 0])),
              (0x1C4, bytes([0, cruise << 5, 0, 0, 0, gas, 0, 0])),
              (0xBE, bytes([0, pedal, 0, 0, 0, 0]))]
  for i in order:
    addr, data = messages[i]
    if addr != omit:
      assert rx(lib, addr, data)


def tx_brake(lib, value=400, bus=None, mode=0xA, length=5):
  bus = lib.bus if bus is None else bus
  raw = (-value) & 0xFFF
  data = (bytes([mode << 4 | raw >> 8, raw & 255]) + bytes(6))[:length]
  return bool(lib.gm_test_frame(1, 0x315, bus, data, len(data)))


@pytest.mark.parametrize("order", list(itertools.permutations(range(4))))
def test_native_initial_arm_packet_order(safety, order):
  assert not tx_brake(safety)
  inputs(safety, order=order)
  inputs(safety, order=order)
  assert tx_brake(safety)
  assert not safety.get_controls_allowed()


def test_native_all_brake_values_buses_modes_lengths(safety):
  inputs(safety)
  for bus in range(4):
    for value in range(4096):
      assert tx_brake(safety, value, bus) == (bus == safety.bus and value <= 400)
  for mode in range(16):
    assert tx_brake(safety, mode=mode) == (mode == 0xA)
  for length in range(9):
    assert tx_brake(safety, length=length) == (length == 5)
  assert not safety.get_controls_allowed()


@pytest.mark.parametrize("omit", [0x34A, 0xC9, 0x1C4, 0xBE])
def test_native_missing_stale_inputs(safety, omit):
  inputs(safety, omit=omit)
  assert not tx_brake(safety)
  inputs(safety)
  assert tx_brake(safety)
  safety.set_timer(500001)
  inputs(safety, pedal=0, pressed=False, omit=omit)
  assert not tx_brake(safety)
  inputs(safety, pedal=0, pressed=False)
  assert not tx_brake(safety)


@pytest.mark.parametrize("change", [dict(speed=11), dict(gas=1), dict(cruise=1), dict(cruise=3)])
def test_native_motion_gas_cruise_clears(safety, change):
  inputs(safety)
  inputs(safety, pedal=0, pressed=False, **change)
  assert not tx_brake(safety)
  inputs(safety, pedal=0, pressed=False)
  assert not tx_brake(safety)


@pytest.mark.parametrize("peak", [10, 79, 80, 100])
@pytest.mark.parametrize("order", list(itertools.permutations(range(4))))
def test_native_retains_permission_across_asynchronous_pedal_frames(safety, peak, order):
  inputs(safety)
  inputs(safety, pedal=0, pressed=False, order=order)
  assert tx_brake(safety)
  inputs(safety, pedal=peak, order=order)
  inputs(safety, pedal=0, pressed=False, order=order)
  assert tx_brake(safety)
  assert tx_brake(safety, 0)  # Host light-tap cancellation remains permitted.


def test_native_weak_or_moving_brake(safety):
  inputs(safety, pedal=79)
  assert not tx_brake(safety)
  inputs(safety, speed=174)
  assert not tx_brake(safety)
  inputs(safety, pedal=0, pressed=False, speed=174)
  inputs(safety, pedal=0, pressed=False)
  assert not tx_brake(safety)
  inputs(safety, speed=100)
  assert not tx_brake(safety)
  inputs(safety, pedal=30)
  assert tx_brake(safety)


def test_native_stale_tx_and_timer_wrap(safety):
  safety.set_timer(0xFFFFFF00)
  inputs(safety)
  safety.set_timer(100000)
  assert tx_brake(safety)
  safety.set_timer(500001)
  assert not tx_brake(safety)


def test_native_relay_failure_and_regen_block(safety):
  inputs(safety)
  assert rx(safety, 0xBD, bytes([0x10]) + bytes(6))
  assert not tx_brake(safety)
  assert rx(safety, 0xBD, bytes(7))
  inputs(safety)
  assert tx_brake(safety)
  safety.set_relay_malfunction(True)
  assert not tx_brake(safety)


@pytest.mark.parametrize("flag", [GMSafetyFlags.HW_CAM, GMSafetyFlags.HW_CAM_LONG,
                                 GMSafetyFlags.HW_SDGM, GMSafetyFlags.MANUAL_AUTO_HOLD])
def test_native_feature_flag_and_mode_reset(safety, flag):
  # Toggling HW_SDGM moves the brake to the other harness bus, never to both.
  inputs(safety)
  assert tx_brake(safety)
  assert safety.set_safety_hooks(structs.CarParams.SafetyModel.gm, safety.flags ^ int(flag)) == 0
  inputs(safety)
  assert not tx_brake(safety)
  assert safety.set_safety_hooks(structs.CarParams.SafetyModel.gm, safety.flags) == 0
  inputs(safety, pedal=0, pressed=False)
  assert not tx_brake(safety)


def test_native_no_acceleration_permission(safety, runtime):
  # Both harnesses: an armed or held stop never becomes gas/ACC permission, even
  # from a stale active 0x2CB after the host released the hold with a light tap.
  from opendbc.car.gm import gmcan
  inputs(safety)
  packer = runtime.CANPacker(DBC[CAR.CHEVROLET_TRAVERSE][Bus.pt])
  for release in (False, True):
    if release:
      inputs(safety, pedal=0, pressed=False)
      inputs(safety, pedal=20)
      inputs(safety, pedal=0, pressed=False)
    for amount, active in [(-500, False), (-500, True), (0, False), (100, True)]:
      addr, data, bus = gmcan.create_gas_regen_command(packer, 0, amount, 0, active, False)
      assert bool(safety.gm_test_frame(1, addr, bus, data, len(data))) == (amount == -500 and not active)
      assert not safety.get_controls_allowed()
    # Requests rejected for bus or length leave no permission behind either.
    addr, data, _ = gmcan.create_gas_regen_command(packer, 0, 100, 0, True, False)
    for bus, payload in [(1, data), (2, data), (0, data[:7])]:
      assert not safety.gm_test_frame(1, addr, bus, payload, len(payload))
      assert not safety.get_controls_allowed()
    assert not tx_brake(safety, mode=0xD)
    assert tx_brake(safety)


@pytest.mark.parametrize("address,length", [(0x34A, 4), (0xC9, 7), (0x1C4, 7), (0xBE, 5)])
def test_native_malformed_input_drops_permission(safety, address, length):
  inputs(safety)
  assert tx_brake(safety)
  rx(safety, address, bytes(length))
  assert not tx_brake(safety)


@pytest.mark.parametrize("extra", [GMSafetyFlags.CC_LONG, GMSafetyFlags.NO_ACC, GMSafetyFlags.PEDAL_LONG,
                                  GMSafetyFlags.GAS_INTERCEPTOR, GMSafetyFlags.HW_ASCM_LONG, GMSafetyFlags.EV,
                                  GMSafetyFlags.NO_CAMERA])
def test_native_incompatible_mode_cannot_enable_hold(safety, extra):
  assert safety.set_safety_hooks(structs.CarParams.SafetyModel.gm, safety.flags | int(extra)) == 0
  inputs(safety)
  assert not tx_brake(safety)


@pytest.mark.parametrize("candidate", [CAR.CHEVROLET_TRAVERSE, *CAM_HOLD_CARS])
def test_host_release_produces_zero_brake_accepted_by_panda(runtime, native, candidate):
  traverse = candidate == CAR.CHEVROLET_TRAVERSE
  cp = build_cp(runtime) if traverse else hold_cp(runtime, candidate)
  safety = hold_safety(native, *HOLD_MODES["sdgm" if traverse else "camera"])
  controller = runtime.controller.CarController(DBC[candidate], cp)
  cs = controller_state() if traverse else camera_controller_state()
  cs.out = state()
  cs.manual_auto_hold = armed_hold()
  inputs(safety)
  cc = structs.CarControl().as_reader()
  for tick, (pedal, pressed) in enumerate([(80, True), (0, False), (20, True), (0, False)]):
    cs.out.brakePressed = pressed
    cs.manual_auto_hold.update(cs.out, pedal, False)
    inputs(safety, pedal=pedal, pressed=pressed)
    controller.frame = tick * 4
    cs.cam_ascm_2cb_counter = tick % 4
    _, frames = controller.update(cc, cs, 1_000_000_000 + tick * 40_000_000)
    brakes = [msg for msg in frames if msg[0] == 0x315]
    assert len(brakes) == 1
    addr, data, bus = brakes[0]
    assert bus == safety.bus
    assert (-int.from_bytes(data[:2], "big")) & 0xFFF == (0 if tick == 3 else 400)
    assert safety.gm_test_frame(1, addr, bus, data, len(data))
    assert not safety.get_controls_allowed()


@pytest.mark.parametrize("raw_first", [True, False])
def test_single_strong_sample_and_pressed_switch_order(safety, raw_first):
  inputs(safety, pedal=0, pressed=False)
  strong = (0xBE, bytes([0, 80, 0, 0, 0, 0]))
  pressed = (0xC9, bytes([0, 0, 0, 0x20, 0, 1, 0, 0]))
  for message in ([strong, pressed] if raw_first else [pressed, strong]):
    assert rx(safety, *message)
  assert rx(safety, 0xBE, bytes([0, 79, 0, 0, 0, 0]))
  assert tx_brake(safety)
  inputs(safety, pedal=0, pressed=False)
  assert tx_brake(safety)
  assert not safety.get_controls_allowed()
