"""Car-supplied fcw (GM Traverse stock camera FCW) and the model/planner fcw register once per frame.

A duplicate fcw in one frame restarts the alert, which then outranks a later takeover alert of the
same priority. Runs the real update_events FCW block and AlertManager from this checkout.
"""
import copy
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pytest

from openpilot.selfdrive.selfdrived.tests import test_phone_forward_risk_integration as harness
from openpilot.selfdrive.selfdrived.tests.test_alert_presentation import load_events


def run(monkeypatch, *, car_fcw, model_fcw, takeover_frame=50, frames=61):
  monkeypatch.setattr(harness, "ROOT", Path(__file__).resolve().parents[1])  # this checkout's selfdrived
  m = load_events()
  am_module = harness.load_classes('alertmanager.py', {'copy': copy, 'defaultdict': defaultdict, 'dataclass': dataclass,
                                                       'Alert': m.Alert, 'EmptyAlert': m.EmptyAlert})
  state, _, _ = harness.controller(m, phone=False)
  state.sm['modelV2'].meta.hardBrakePredicted = model_fcw
  am = am_module.AlertManager()
  counts, selected = [], []
  for frame in range(frames):
    state.events = m.Events()
    if car_fcw:
      state.events.add(m.EventName.fcw)  # as car_specific adds it from carState.stockFcw
    state.update_events()
    if frame >= takeover_frame:
      state.events.add(m.EventName.controlsMismatch)
    counts.append(state.events.names.count(m.EventName.fcw))
    am.add_many(frame, state.events.create_alerts([m.ET.PERMANENT, m.ET.IMMEDIATE_DISABLE]))
    am.process_alerts(frame, set())
    selected.append(am.current_alert.alert_type)
  return counts, selected


@pytest.mark.parametrize("car_fcw,model_fcw,expected", [(True, True, 1), (True, False, 1), (False, True, 1), (False, False, 0)])
def test_fcw_is_registered_once(monkeypatch, car_fcw, model_fcw, expected):
  counts, _ = run(monkeypatch, car_fcw=car_fcw, model_fcw=model_fcw)
  assert set(counts) == {expected}


@pytest.mark.parametrize("car_fcw,model_fcw", [(True, True), (True, False), (False, True)])
def test_takeover_alert_still_wins_over_an_ongoing_fcw(monkeypatch, car_fcw, model_fcw):
  _, selected = run(monkeypatch, car_fcw=car_fcw, model_fcw=model_fcw)
  assert set(selected[:50]) == {'fcw/permanent'}
  assert set(selected[50:]) == {'controlsMismatch/immediateDisable'}
