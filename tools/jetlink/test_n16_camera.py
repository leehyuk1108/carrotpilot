"""Desk-only harness regressions: real schema/parser, simulated native boundaries.

No device, GPU, service, process signalling or driving publication is permitted.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'third_party/jetlink')]
from openpilot import cereal
from openpilot.cereal import log
from openpilot.selfdrive.modeld.jetlink.contracts import N16_SPEC, parse_outputs
from openpilot.selfdrive.modeld.parse_model_outputs import Parser
from tools.jetlink import bench_n16_camera as bench

MODEL_MS = (8., 10., 60.)


def forbidden(*args, **kwargs):
  raise AssertionError('native access/publication is forbidden in this suite')


@pytest.fixture(autouse=True)
def native(monkeypatch):
  messaging = NS(PubMaster=forbidden, SubMaster=forbidden, sub_sock=forbidden)
  hardware = NS(HARDWARE=NS(get_device_type=lambda: 'mici', set_power_save=forbidden))
  for name, module in {
    'openpilot.cereal.messaging': messaging,
    'openpilot.common.params': NS(Params=forbidden),
    'openpilot.common.realtime': NS(config_realtime_process=forbidden),
    'openpilot.common.swaglog': NS(cloudlog=NS(exception=forbidden)),
    'openpilot.system.hardware': hardware,
  }.items():
    monkeypatch.setitem(sys.modules, name, module)
  monkeypatch.setattr(cereal, 'messaging', messaging, raising=False)
  monkeypatch.setattr(bench, 'os', NS(path=NS(isfile=forbidden), kill=forbidden, pipe=forbidden,
    read=forbidden, write=forbidden, close=forbidden, SCHED_FIFO=1, SCHED_OTHER=0,
    sched_getscheduler=forbidden, sched_getparam=forbidden, sched_getaffinity=forbidden,
    sched_setscheduler=forbidden, sched_setaffinity=forbidden, sched_param=forbidden))
  monkeypatch.setattr(bench, 'subprocess', NS(Popen=forbidden, PIPE=-1))
  monkeypatch.setattr(bench, 'check_processes', forbidden)
  monkeypatch.setattr(bench, 'arm_watchdog', forbidden)
  monkeypatch.setattr(bench, 'process_identity', forbidden)
  return NS(messaging=messaging, hardware=hardware.HARDWARE)


@pytest.fixture
def calibration(monkeypatch):
  # Execute the real validator and C4 constants, with only unused native imports replaced.
  monkeypatch.setitem(sys.modules, 'openpilot.common.transformations.orientation',
                      NS(rot_from_euler=forbidden, euler_from_rot=forbidden))
  name = 'openpilot.selfdrive.locationd.calibrationd'
  spec = importlib.util.spec_from_file_location(name, ROOT / 'openpilot/selfdrive/locationd/calibrationd.py')
  module = importlib.util.module_from_spec(spec)
  monkeypatch.setitem(sys.modules, name, module)
  spec.loader.exec_module(module)
  return module


def saved_calibration(status='calibrated', blocks=5, rpy=(0., .04, -.02), valid=True,
                      kind='liveCalibration', stamp=10**18):
  event = log.Event.new_message(valid=valid, logMonoTime=stamp)
  child = event.init(kind)
  if kind == 'liveCalibration':
    child.calStatus, child.validBlocks, child.rpyCalib = status, blocks, list(rpy)
  return event.to_bytes()


@pytest.mark.parametrize('stamp', [1, 10**18])
def test_static_pose_retains_provenance_not_current_boot_freshness(calibration, stamp):
  raw, reads = saved_calibration(stamp=stamp), []
  def get(key):
    reads.append(key)
    return raw
  pose, metadata = bench.read_static_calibration(NS(get=get))
  assert reads == ['CalibrationParams']
  np.testing.assert_allclose(pose, [0., .04, -.02])
  assert calibration.is_calibration_valid(pose)
  assert metadata['valid_blocks'] == calibration.INPUTS_NEEDED
  assert metadata['source'] == 'CalibrationParams' and metadata['mode'] == 'static_recorded'
  assert metadata['live'] is False and metadata['saved_event_mono_ns'] == stamp
  assert metadata['sha256'] == hashlib.sha256(raw).hexdigest()
  assert 'unknown' in metadata['timestamp_boot'] and 'no freshness claim' in metadata['timestamp_boot']
  assert 'no dynamic pose or current mounting validation' in metadata['limitation']


@pytest.mark.parametrize('change', [
  {'valid': False}, {'kind': 'deviceState'}, {'status': 'uncalibrated'}, {'status': 'recalibrating'},
  {'status': 'invalid'}, {'blocks': 4}, {'blocks': -1}, {'rpy': (0, 0)}, {'rpy': (0, 0, 0, 0)},
  {'rpy': (np.nan, 0, 0)}, {'rpy': (0, np.inf, 0)}, {'rpy': (0, 0, -np.inf)},
  {'rpy': (0, -.2, 0)}, {'rpy': (0, .3, 0)}, {'rpy': (0, 0, .1)}, {'rpy': (0, 0, -.1)},
])
def test_static_calibration_rejects_invalid_saved_events(calibration, change):
  with pytest.raises(RuntimeError, match='CalibrationParams rejected'):
    bench.read_static_calibration(NS(get=lambda key: saved_calibration(**change)))


@pytest.mark.parametrize('raw', [None, b'', 'not bytes', b'x' * 65537, b'broken', saved_calibration()[:16]])
def test_static_calibration_rejects_missing_or_malformed_storage(calibration, raw):
  with pytest.raises(RuntimeError, match='CalibrationParams'):
    bench.read_static_calibration(NS(get=lambda key: raw))


@pytest.fixture
def desk():
  class SM(dict):
    pass
  sm = SM(deviceState=NS(started=False), pandaStates=[NS(controlsAllowed=False, ignitionLine=False,
    ignitionCan=False, safetyModel='noOutput', harnessStatus='notConnected')],
    managerState=NS(processes=[NS(name=name, running=False) for name in bench.FORBIDDEN]))
  sm.alive, sm.valid = (dict.fromkeys(bench.SAFETY_SERVICES, True) for _ in range(2))
  sm.recv_time = dict.fromkeys(bench.SAFETY_SERVICES, 99.9)
  sm.logMonoTime = dict.fromkeys(bench.SAFETY_SERVICES, 99_900_000_000)
  values = {'IsOffroad': True, 'IsOnroad': False}
  return NS(sm=sm, values=values, params=NS(get_bool=values.__getitem__))


def test_fresh_disconnected_desk_guard(desk):
  bench.validate_desk(desk.params, desk.sm, 100.)


@pytest.mark.parametrize('name', bench.SAFETY_SERVICES)
@pytest.mark.parametrize('field,value', [
  ('alive', False), ('valid', False), ('recv_time', 97.5), ('recv_time', 100.01),
  ('logMonoTime', 97_500_000_000), ('logMonoTime', 100_010_000_000),
])
def test_guard_rejects_stale_future_or_invalid_evidence(desk, name, field, value):
  getattr(desk.sm, field)[name] = value
  with pytest.raises(RuntimeError, match='safety state'):
    bench.validate_desk(desk.params, desk.sm, 100.)


@pytest.mark.parametrize('case', ['onroad', 'not_offroad', 'started', 'no_panda', 'incomplete_manager', *bench.FORBIDDEN])
def test_guard_rejects_vehicle_or_process_activity(desk, case):
  if case in ('onroad', 'not_offroad'):
    desk.values['IsOnroad' if case == 'onroad' else 'IsOffroad'] = case == 'onroad'
  elif case == 'started':
    desk.sm['deviceState'].started = True
  elif case == 'no_panda':
    desk.sm['pandaStates'].clear()
  elif case == 'incomplete_manager':
    desk.sm['managerState'].processes.clear()
  else:
    next(p for p in desk.sm['managerState'].processes if p.name == case).running = True
  with pytest.raises(RuntimeError):
    bench.validate_desk(desk.params, desk.sm, 100.)


@pytest.mark.parametrize('field,value', [('controlsAllowed', True), ('ignitionLine', True), ('ignitionCan', True),
                                        ('safetyModel', 'silent'), ('harnessStatus', 'normal')])
def test_guard_checks_every_panda(desk, field, value):
  other = NS(**vars(desk.sm['pandaStates'][0]))
  setattr(other, field, value)
  desk.sm['pandaStates'].append(other)
  with pytest.raises(RuntimeError, match='vehicle not disconnected'):
    bench.validate_desk(desk.params, desk.sm, 100.)


@pytest.fixture
def camera_context():
  class Context(dict):
    pass
  context = Context()
  for name in ('roadCameraState', 'wideRoadCameraState'):
    state = log.Event.new_message(valid=True).init(name)
    state.frameId, state.timestampSof, state.timestampEof = 10, 99_880_000_000, 99_900_000_000
    context[name] = state
  context.seen, context.alive, context.valid = (dict.fromkeys(context, True) for _ in range(3))
  context.logMonoTime = dict.fromkeys(context, 99_950_000_000)
  context.recv_time = dict.fromkeys(context, 99.99)
  meta = NS(frame_id=10, timestamp_sof=99_880_000_000, timestamp_eof=99_900_000_000)
  return context, meta


@pytest.mark.parametrize('delta', [-1, 0, 1])
def test_camera_states_allow_only_correlated_adjacent_frames(camera_context, delta):
  context, meta = camera_context
  for state in context.values():
    state.frameId += delta
    state.timestampSof += delta * 77_000_000
    state.timestampEof += delta * 77_000_000
  result = bench.camera_state_evidence(context, meta, meta, 100.)
  for name, evidence in result.items():
    assert evidence['valid'] and evidence['frame_id'] == 10 + delta
    assert evidence['vipc_frame_delta'] == delta and evidence['vipc_sof_delta_ns'] == delta * 77_000_000
    assert evidence['event_mono_ns'] == context.logMonoTime[name]
    assert evidence['capture_age_ms'] == pytest.approx(100. - delta * 77.)


@pytest.mark.parametrize('service', ['roadCameraState', 'wideRoadCameraState'])
@pytest.mark.parametrize('case', [
  'unseen', 'invalid', 'dead', 'stale_event', 'future_event', 'stale_receive', 'future_receive',
  'stale_capture', 'future_capture', 'zero_sof', 'reversed_exposure', 'id_behind', 'id_ahead',
  'same_id_wrong_sof', 'same_id_wrong_eof', 'adjacent_wrong_time', 'adjacent_wrong_direction', 'adjacent_too_far',
])
def test_camera_state_rejections_for_each_producer(camera_context, service, case):
  context, meta = camera_context
  state = context[service]
  if case in ('unseen', 'invalid', 'dead'):
    getattr(context, {'unseen': 'seen', 'invalid': 'valid', 'dead': 'alive'}[case])[service] = False
  elif case in ('stale_event', 'future_event'):
    context.logMonoTime[service] = 99_750_000_000 if case == 'stale_event' else 100_001_000_000
  elif case in ('stale_receive', 'future_receive'):
    context.recv_time[service] = 99.75 if case == 'stale_receive' else 100.001
  elif case in ('stale_capture', 'future_capture'):
    state.timestampSof, state.timestampEof = ((99_700_000_000, 99_750_000_000) if case == 'stale_capture'
                                             else (100_000_000_000, 100_001_000_000))
  elif case in ('zero_sof', 'reversed_exposure'):
    state.timestampSof = 0 if case == 'zero_sof' else state.timestampEof + 1
  elif case in ('id_behind', 'id_ahead'):
    state.frameId += -2 if case == 'id_behind' else 2
  elif case == 'same_id_wrong_sof':
    state.timestampSof -= 1
  elif case == 'same_id_wrong_eof':
    state.timestampEof -= 1
  else:
    state.frameId -= 1
    shift = {'adjacent_wrong_time': 0, 'adjacent_wrong_direction': 50_000_000, 'adjacent_too_far': -100_000_001}[case]
    state.timestampSof += shift
    state.timestampEof += shift
  with pytest.raises(RuntimeError, match=service):
    bench.camera_state_evidence(context, meta, meta, 100.)


@pytest.fixture
def output():
  return parse_outputs(Parser(), N16_SPEC, np.zeros(N16_SPEC.output_nelem, np.float32))


def test_real_n16_parser_output_contract(output):
  bench.checked(output)
  assert output['action'].shape == (1, 4)


@pytest.mark.parametrize('name', ['plan', 'plan_stds', 'lead', 'pose', 'lane_lines'])
@pytest.mark.parametrize('failure', ['missing', 'shape', 'nan'])
def test_output_rejections(output, name, failure):
  if failure == 'missing':
    del output[name]
  elif failure == 'shape':
    output[name] = output[name].ravel()
  else:
    output[name].flat[-1] = np.nan
  with pytest.raises(RuntimeError, match='parsed|nonfinite'):
    bench.checked(output)


@pytest.mark.parametrize('value', [None, {}, {'plan': np.zeros((1, 33, 15))}])
def test_incomplete_output_rejected(value):
  with pytest.raises(RuntimeError):
    bench.checked(value)


@pytest.mark.parametrize('case', ['healthy', 'absent', 'single', 'invalid', 'gap', 'duplicate', 'slow', 'stalled', 'late_start', 'expired'])
def test_dm_metrics_require_full_valid_20hz_window(case):
  rows = [{'mono_ns': 100_000_000_000 + i * 50_000_000, 'frame_id': i, 'valid': True} for i in range(241)]
  now, started = 112.1, 100.
  if case in ('absent', 'single'):
    rows = rows[:int(case == 'single')]
  elif case == 'invalid':
    rows[100]['valid'] = False
  elif case in ('gap', 'duplicate'):
    rows[100]['frame_id'] += 1 if case == 'gap' else -1
  elif case == 'slow':
    for i, row in enumerate(rows):
      row['mono_ns'] += i * 1_000_000
    now += .24
  elif case == 'stalled':
    rows[100]['mono_ns'] += 200_000_000
  elif case == 'late_start':
    started -= 1.
  elif case == 'expired':
    now += 1.
  result = bench.dm_summary(rows, now, started)
  assert result['strict_20hz_pass'] is (case == 'healthy')
  assert result['received'] == len(rows)
  if case == 'healthy':
    assert result['hz'] == 20. and result['valid'] == 241 and result['span_seconds'] == 12.
    assert result['max_gap_seconds'] == .05


@pytest.mark.parametrize('normal,unsafe', [(True, False), (False, False), (True, True)])
def test_watchdog_restores_only_after_safe_recheck(monkeypatch, native, tmp_path, normal, unsafe):
  powers, closed = [], []
  monkeypatch.setattr(native.hardware, 'set_power_save', powers.append)
  monkeypatch.setattr(bench, 'DeskGuard', lambda: NS(check=lambda: bench.require(not unsafe, 'unsafe restore')))
  monkeypatch.setattr(bench, 'process_identity', lambda pid: None)
  monkeypatch.setattr(bench, 'CPU_ONLINE', NS(read_text=lambda: '0-3'))
  monkeypatch.setattr(bench, 'select', NS(select=lambda *a: ([42], [], [])))
  for name, value in {'sched_setscheduler': lambda *a: None, 'sched_setaffinity': lambda *a: None,
                      'sched_param': lambda *a: None, 'read': lambda *a: b'D' if normal else b'', 'close': closed.append}.items():
    monkeypatch.setattr(bench.os, name, value)
  target = tmp_path / 'restore.json'
  assert bench.watchdog(42, 123, 'identity', .01, target, '0-3') == int(unsafe)
  saved = json.loads(target.read_text())
  assert saved['restored'] is not unsafe and powers == ([] if unsafe else [True]) and closed == [42]
  assert saved['reason'] == ('cleanup' if normal else 'timeout_or_parent_exit')


@pytest.mark.parametrize('failure,error', [
  (None, None), ('identity', 'exact pinned N16'), ('nav', 'navigation unexpectedly active'),
  ('camera', 'stale camera pair'), ('context', 'stale roadCameraState'),
  ('guard', 'desk changed'), ('live_calibration', 'stale liveCalibration'),
])
def test_bench_provenance_metrics_and_fail_closed(monkeypatch, native, calibration, output, tmp_path, failure, error):
  clock, calls, reads, checks = NS(now=100., frame=0), [], [], []
  monkeypatch.setattr(bench, 'time', NS(monotonic=lambda: clock.now))
  monkeypatch.setattr(bench.os.path, 'isfile', lambda path: path == '/AGNOS')
  for name, value in {'sched_getscheduler': lambda *a: 1, 'sched_getparam': lambda *a: 54,
                      'sched_getaffinity': lambda *a: {7}, 'sched_setscheduler': lambda *a: None,
                      'sched_setaffinity': lambda *a: None, 'write': lambda *a: None, 'close': lambda *a: None}.items():
    monkeypatch.setattr(bench.os, name, value)
  native.hardware.set_power_save = lambda value: calls.append(('power', value))
  monkeypatch.setattr(bench, 'CPU_ONLINE', NS(read_text=lambda: '0-7'))
  def get(key):
    reads.append(key)
    return saved_calibration()
  def check():
    checks.append(clock.now)
    bench.require(failure != 'guard' or clock.now < 100.6, 'desk changed')
    return {'safe': True, 'mono': clock.now}
  params = NS(get=get, get_float=lambda key: 100.)
  monkeypatch.setattr(bench, 'DeskGuard', lambda: NS(params=params, check=check))
  restore_path = tmp_path / 'run.json.restore.json'
  restore_path.write_text('{"restored": true}')
  restore = NS(poll=lambda: None, wait=lambda **kw: None, returncode=0, stdout=NS(close=lambda: None))
  monkeypatch.setattr(bench, 'arm_watchdog', lambda *a: (restore, 42))

  class Context(dict):
    def __init__(self, services):
      expected = ['deviceState', 'roadCameraState', 'wideRoadCameraState']
      if failure == 'live_calibration':
        expected.append('liveCalibration')
      assert services == expected  # Static mode neither subscribes to nor publishes calibration.
      super().__init__(deviceState=NS(deviceType='mici'), roadCameraState=NS(sensor='test'), wideRoadCameraState=NS(sensor='test'))
      if failure == 'live_calibration':
        self['liveCalibration'] = NS(calStatus='calibrated', rpyCalib=[0., .04, -.02])
      self.seen, self.valid, self.alive = (dict.fromkeys(services, True) for _ in range(3))
    def update(self, timeout):
      self.logMonoTime = dict.fromkeys(self, int((clock.now - .001) * 1e9))
      self.recv_time = dict.fromkeys(self, clock.now - .001)
      for name in ('roadCameraState', 'wideRoadCameraState'):
        self[name].frameId = clock.frame
        self[name].timestampSof = int((clock.now - .01 - .005) * 1e9)
        self[name].timestampEof = int((clock.now - .01) * 1e9)
      if failure in ('context', 'live_calibration'):
        self.logMonoTime['roadCameraState' if failure == 'context' else 'liveCalibration'] -= 10_000_000_000

  native.messaging.SubMaster = Context
  native.messaging.sub_sock = lambda service, **kw: None
  native.messaging.drain_sock = lambda sock: []
  camera = NS(width=1344, height=760, valid=False, stride=1408, uv_offset=1081344,
              buffer_len=1626112, connect=lambda blocking: True)
  class Vision:
    def __new__(cls, *args):
      return camera
    available_streams = staticmethod(lambda *a, **kw: [0, 1])
  def receive(*args):
    clock.now += .05
    clock.frame += 1
    age = 1. if failure == 'camera' else .01
    meta = NS(frame_id=clock.frame, timestamp_sof=int((clock.now - age - .005) * 1e9),
              timestamp_eof=int((clock.now - age) * 1e9))
    return camera, meta, camera, meta

  link = NS(state=lambda: {'state': 'ready'}, FAULT_HOLD_SECONDS=5.)
  model = NS(FAULT=tmp_path / 'production-fault')
  class Joining:
    def __init__(self, *args):
      self.spec, self.frame, self.fault_until = N16_SPEC, 0, 0.
      self.active, self.connection, self.preparation = False, None, None
      self.views = {'nav_map': np.zeros(1), 'navigation': np.zeros(1)}
      if failure == 'nav':
        self.views['navigation'].fill(1)
      self.warp = NS(timings=(.001, .002, .003))
      self.client = self.remote()
    def remote(self):
      remote = NS(spec=None if failure == 'identity' else N16_SPEC, timings=(1000, 2000, 3000), closed=False)
      remote.close = lambda: setattr(remote, 'closed', True)
      return remote
    @property
    def output_valid(self):
      return clock.now >= self.fault_until
    def run(self, bufs, transforms, inputs, prepare_only):
      assert set(inputs) == {'desire_pulse', 'traffic_convention', 'action_t'}
      if self.client is not None and self.client.closed:
        self.active, self.client = False, None
        self.fault_until = clock.now + 5.
        model.FAULT.write_text(str(clock.now))
      elif self.join_allowed:
        self.client = self.client or self.remote()
        self.active = True
      self.frame += 1
      clock.now += MODEL_MS[clock.frame % len(MODEL_MS)] / 1000
      return output
  model.JoiningModel = Joining
  class Shadow:
    @classmethod
    def optional(cls, *args):
      forbidden()
  def warp(pose, intrinsics, wide):
    np.testing.assert_allclose(pose, [0., .04, -.02 - np.radians(1.)], atol=1e-7)
    return np.eye(3, dtype=np.float32)
  def schedule(core, priority):
    assert (core, priority) == (7, 54)
  old_fault, old_shadow = model.FAULT, Shadow.__dict__['optional']
  modules = {
    'msgq.visionipc': NS(VisionIpcClient=Vision, VisionStreamType=NS(VISION_STREAM_ROAD=0, VISION_STREAM_WIDE_ROAD=1)),
    'openpilot.common.realtime': NS(config_realtime_process=schedule),
    'openpilot.common.transformations.camera': NS(DEVICE_CAMERAS={('mici', 'test'): NS(fcam=NS(intrinsics=0), ecam=NS(intrinsics=1))}),
    'openpilot.common.transformations.model': NS(get_warp_matrix=warp),
    'openpilot.selfdrive.modeld.camera_sync': NS(receive_camera_pair=receive),
    'openpilot.selfdrive.modeld.jetlink': NS(link=link, model=model),
    'openpilot.selfdrive.modeld.modeld': NS(ModelState=lambda *a: object()),
    'openpilot.selfdrive.modeld.signal_shadow': NS(ShadowClient=Shadow),
  }
  for name, module in modules.items():
    monkeypatch.setitem(sys.modules, name, module)
  monkeypatch.setattr(bench, 'layout', lambda *a: None)  # Layout has its own native-independent contract test below.
  source = 'live' if failure == 'live_calibration' else 'params'
  args = NS(seconds=30, output=tmp_path / 'run.json', require_dm=False, calibration_source=source)
  report = {'frames': [], 'dm_frames': [], 'safety_checks': []}
  if error is None:
    bench.bench(args, report)
  else:
    with pytest.raises(RuntimeError, match=error):
      bench.bench(args, report)
  assert model.FAULT == old_fault and Shadow.__dict__['optional'] is old_shadow
  assert report['restoration']['restored'] and calls == [('power', False)]
  assert reads == ([] if source == 'live' else ['CalibrationParams'])
  if error is not None:
    assert not report.get('completed', False)
    if source == 'live':
      assert 'calibration' not in report  # Stale live data must not silently use stored params.
    return
  assert report['completed'] and report['rejoined'] and report['restoration']['restored']
  assert report['n16_sha256'] == N16_SPEC.sha256
  assert 'unused' in report['camera_validity']['visionipc_valid']
  assert report['camera_validity']['max_age_seconds'] == .25
  assert len(checks) > 50 and checks[0] == 100.
  assert max(np.diff(checks)) < .7  # Guard remains periodic through IPC fault and rejoin.
  assert not report['calibration']['live'] and report['camera_context']['calibration_mono_ns'] is None
  assert report['camera_context']['calibration_mode'] == 'static_recorded'
  np.testing.assert_allclose(report['calibration']['rpy_recorded'], [0., .04, -.02])
  assert report['ipc_fault']['invalid_until'] - report['ipc_fault']['fault_at'] == 5.
  rows = report['frames']
  assert len(rows) > 100 and all(r['road_frame_id'] == r['wide_frame_id'] == r['ipc_frame'] for r in rows)
  assert [r['road_frame_id'] for r in rows] == list(range(1, len(rows) + 1))
  assert all(r['output_present'] for r in rows) and any(not r['output_valid'] for r in rows)
  for row in rows:
    assert set(row['camera_state_evidence']) == {'roadCameraState', 'wideRoadCameraState'}
    for evidence in row['camera_state_evidence'].values():
      assert evidence['valid'] and evidence['frame_id'] == row['road_frame_id']
      assert evidence['vipc_frame_delta'] == evidence['vipc_sof_delta_ns'] == evidence['vipc_eof_delta_ns'] == 0
  durations = np.array([MODEL_MS[r['road_frame_id'] % len(MODEL_MS)] for r in rows])
  np.testing.assert_allclose([r['run_ms'] for r in rows], durations, atol=1e-6)
  np.testing.assert_allclose([r['capture_age_start_ms'] for r in rows], 10., atol=1e-5)
  np.testing.assert_allclose([r['capture_age_end_ms'] for r in rows], 10. + durations, atol=1e-5)
  external = [r for r in rows if r['phase'] == 'external']
  expected_ms = np.array([MODEL_MS[r['road_frame_id'] % len(MODEL_MS)] for r in external])
  assert report['external_timing'] == pytest.approx({'frames': len(external), 'mean_ms': expected_ms.mean(),
    'p95_ms': np.percentile(expected_ms, 95), 'max_ms': expected_ms.max(), 'over_50ms': int((expected_ms > 50).sum())})
  assert all(r['warp_ms'] == [1., 2., 3.] and r['server_us'] == [1000, 2000, 3000] for r in external)


@pytest.mark.parametrize('field', [None, 'width', 'height', 'stride', 'uv_offset', 'data'])
def test_camera_layout_contract_without_native_buffers(field):
  from openpilot.system.camerad.cameras.nv12_info import get_nv12_info
  stride, y_height, _, size = get_nv12_info(1344, 760)
  buf = NS(width=1344, height=760, stride=stride, uv_offset=stride * y_height, data=range(size))
  if field is None:
    bench.layout(buf, 1344, 760)
  else:
    setattr(buf, field, range(size - 1) if field == 'data' else getattr(buf, field) + 1)
    with pytest.raises(RuntimeError, match='NV12 layout mismatch'):
      bench.layout(buf, 1344, 760)
