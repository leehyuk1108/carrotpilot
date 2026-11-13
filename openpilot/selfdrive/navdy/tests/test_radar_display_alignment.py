"""Navdy wiring of the display-only radar alignment (car/radar_display_alignment.py)."""
from types import SimpleNamespace as NS

from openpilot.selfdrive.car.radar_display_alignment import TRAVERSE as TRAVERSE_NAME, for_fingerprint
from openpilot.selfdrive.navdy import bridge
from openpilot.selfdrive.navdy.display import navdy_lead_geometry
from openpilot.selfdrive.navdy.tests.test_bridge import Messages
from openpilot.selfdrive.navdy.tests.test_display import lead, model, payload
from openpilot.selfdrive.navdy.tests.test_vehicles import data, point
from openpilot.selfdrive.navdy.vehicles import VehicleOverlay

TRAVERSE = for_fingerprint(TRAVERSE_NAME)


def leads(**kwargs):
  return NS(leadOne=lead(**kwargs), leadTwo=lead(status=False))


def test_radar_lead_on_the_right_line_moves_into_its_own_lane():
  # Lanes at +-1.8 m. Radar says 2.0 m right at 30 m: the right lane as measured,
  # 2.0 - (0.29 + 0.0064 * 30) = 1.518 m (own lane) once aligned.
  raw = navdy_lead_geometry(model(), leads(dRel=30.0, yRel=-2.0))['navVehicles'][0]
  fixed = navdy_lead_geometry(model(), leads(dRel=30.0, yRel=-2.0), radar_alignment=TRAVERSE)['navVehicles'][0]
  assert (raw['lane'], raw['lateralM']) == ('right', 2.0)
  assert (fixed['lane'], fixed['lateralM']) == ('center', 1.52)
  assert fixed['screenX'] < raw['screenX']
  same = ('trackId', 'distanceM', 'relativeSpeedMps', 'source', 'longitudinalLead', 'confidence', 'screenY')
  assert {k: fixed[k] for k in same} == {k: raw[k] for k in same}


def test_vision_lead_is_already_in_the_camera_frame():
  raw = navdy_lead_geometry(model(), leads(dRel=30.0, yRel=-1.0, radar=False))
  assert navdy_lead_geometry(model(), leads(dRel=30.0, yRel=-1.0, radar=False), radar_alignment=TRAVERSE) == raw


def render(overlay, radar, *, timestamp, radar_alignment):
  m = model()
  result = payload(model=m)
  overlay.update(result, m, radar, timestamp=timestamp, now=timestamp, radar_alignment=radar_alignment)
  return result


def confirmed(overlay, radar, alignment, start=100.):
  render(overlay, radar, timestamp=start, radar_alignment=alignment)
  return render(overlay, radar, timestamp=start + .2, radar_alignment=alignment)['navVehicles']


def test_raw_track_is_corrected_before_lane_choice():
  radar = data([point(7, distance=30., lateral=-2.0)])  # liveTracks yRel is left-positive
  assert [(v['lane'], v['lateralM']) for v in confirmed(VehicleOverlay(), radar, None)] == [('right', 2.0)]
  assert [(v['lane'], v['lateralM']) for v in confirmed(VehicleOverlay(), radar, TRAVERSE)] == [('center', 1.52)]


def test_smoothing_gate_compares_corrected_values():
  overlay = VehicleOverlay()
  confirmed(overlay, data([point(7, distance=30., lateral=-2.0)]), TRAVERSE)
  moved = render(overlay, data([point(7, distance=30., lateral=-3.0)]), timestamp=100.4, radar_alignment=TRAVERSE)
  assert [v['lateralM'] for v in moved['navVehicles']] == [round(1.518 + 0.55 * 1.0, 2)]  # smoothed, not restarted


def test_turning_the_correction_on_or_off_needs_two_new_samples():
  overlay = VehicleOverlay()
  radar = data([point(7, distance=30., lateral=-2.0)])
  assert confirmed(overlay, radar, None)
  assert render(overlay, radar, timestamp=100.4, radar_alignment=TRAVERSE)['navVehicles'] == []
  assert [v['lateralM'] for v in render(overlay, radar, timestamp=100.6, radar_alignment=TRAVERSE)['navVehicles']] == [1.52]
  assert render(overlay, radar, timestamp=100.8, radar_alignment=None)['navVehicles'] == []


def test_bridge_passes_the_correction_to_leads_and_raw_tracks(monkeypatch):
  clock = NS(now=100.)
  monkeypatch.setattr(bridge.time, 'monotonic', lambda: clock.now)
  sm = Messages()
  sm.data['radarState'] = leads(dRel=30.0, yRel=-2.0)
  sm.data['liveTracks'] = data([point(7, distance=20., lateral=2.4)])
  overlay = VehicleOverlay()
  bridge.live_payload(sm, 1, vehicles=overlay, radar_alignment=TRAVERSE)
  clock.now = 100.2
  sm.recv_time['liveTracks'] = clock.now
  sm.logMonoTime['liveTracks'] = int(clock.now * 1e9)
  result = bridge.live_payload(sm, 2, vehicles=overlay, radar_alignment=TRAVERSE)
  by_id = {v['trackId']: v for v in result['navVehicles']}
  assert (by_id[42]['lane'], by_id[42]['lateralM']) == ('center', 1.52)
  assert (by_id[7]['lane'], by_id[7]['lateralM']) == ('left', round(-2.4 - (0.29 + 0.0064 * 20), 2))
  assert bridge.live_payload(sm, 3, vehicles=VehicleOverlay())['navVehicles'][0]['lateralM'] == 2.0
