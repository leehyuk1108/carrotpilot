import json
import subprocess

from openpilot.selfdrive.navdy.diagnostics import LinkDiagnostics


def recorder():
  emitted = []
  return LinkDiagnostics(lambda name, **event: emitted.append((name, event))), emitted


def receiver(clears=205, session=39):
  return {'wireless': {'session': session, 'clears': clears, 'owner': 'bluetooth', 'usbUpdates': 0},
          'displayPower': {'state': 'offroad', 'fresh': True, 'invalidations': 220, 'generation': 525},
          'modelNavigation': {'privateRoute': [[1, 2]]}, 'navigationFeedback': {'privateDestination': 'SECRET'}}


def feedback(diagnostics, response):
  diagnostics.feedback(response, transport='bluetooth', received_at=20, elapsed_ms=55, wireless={'rttMs': 60})


def test_historical_counters_are_baseline_not_a_new_failure():
  diagnostics, emitted = recorder()
  feedback(diagnostics, receiver())
  for _ in range(100):
    feedback(diagnostics, receiver())
  assert len(emitted) == 1
  assert emitted[0][1]['kind'] == 'receiver_baseline'
  assert diagnostics.snapshot()['lastFailure'] is None


def test_new_clear_and_session_change_are_preserved_without_claiming_cause():
  diagnostics, emitted = recorder()
  feedback(diagnostics, receiver())
  feedback(diagnostics, receiver(clears=206))
  feedback(diagnostics, receiver(clears=0, session=1))
  assert [event['kind'] for _, event in emitted] == ['receiver_baseline', 'receiver_changed', 'receiver_changed']
  event = emitted[1][1]
  assert event['previous']['wireless']['clears'] == 205
  assert event['current']['wireless']['clears'] == 206
  assert event['receivedAt'] == 20
  assert event['roundTripMs'] == 55
  assert event['batchLatestWireless']['rttMs'] == 60
  assert diagnostics.snapshot()['lastFailure'] is None


def test_no_events_from_sequence_or_latency_alone_or_private_feedback():
  diagnostics, emitted = recorder()
  response = receiver()
  feedback(diagnostics, response)
  response['wireless']['receivedSeq'] = 9000
  response['wireless']['appliedSeq'] = 9000
  response['displayPower']['offroadMs'] = 999999
  feedback(diagnostics, response)
  assert len(emitted) == 1
  assert 'SECRET' not in json.dumps(diagnostics.snapshot())
  assert 'privateRoute' not in json.dumps(diagnostics.snapshot())


def test_failure_survives_recovery_and_bounded_ring_eviction():
  diagnostics, _ = recorder()
  feedback(diagnostics, receiver())
  diagnostics.failure(phase='exchange', transport='bluetooth', error=TimeoutError('Navdy wireless v2 response deadline'),
                      connected_at=10, wireless={'rttMs': 900, 'peer': 'PRIVATE', 'ackAgeMs': float('inf')})
  failure = diagnostics.snapshot()['lastFailure']
  feedback(diagnostics, receiver(clears=207, session=40))
  for _ in range(40):
    diagnostics.record('transport_open', transport='bluetooth', connectedAt=30)
  snapshot = diagnostics.snapshot()
  assert len(snapshot['events']) == 16
  assert all(event['runId'] == snapshot['runId'] for event in snapshot['events'])
  assert snapshot['lastFailure'] == failure
  assert failure['receiver']['wireless']['session'] == 39
  assert failure['phase'] == 'exchange'
  assert failure['errorType'] == 'TimeoutError'
  assert failure['wireless'] == {'rttMs': 900}
  snapshot['lastFailure']['reason'] = 'mutated'
  assert diagnostics.snapshot()['lastFailure']['reason'] == 'Navdy wireless v2 response deadline'
  json.dumps(diagnostics.snapshot(), allow_nan=False)


def test_logging_failure_cannot_escape_into_display_transport():
  def failed(*args, **kwargs):
    raise OSError('logging unavailable')
  diagnostics = LinkDiagnostics(failed)
  feedback(diagnostics, receiver())
  diagnostics.failure(phase='status', transport='usb', error=OSError('disk'), connected_at=0, wireless=None)
  assert diagnostics.snapshot()['lastFailure']['phase'] == 'status'


def test_missing_receiver_diagnostics_do_not_invent_reset():
  diagnostics, emitted = recorder()
  feedback(diagnostics, receiver())
  feedback(diagnostics, {'navigationFeedback': None})
  assert len(emitted) == 1


def test_usb_heartbeat_counter_does_not_evict_failure_history():
  diagnostics, emitted = recorder()
  for count in range(100):
    response = receiver()
    response['wireless']['usbUpdates'] = count
    feedback(diagnostics, response)
  assert len(emitted) == 1
  assert diagnostics.receiver['wireless']['usbUpdates'] == 99


def test_exception_commands_paths_addresses_and_data_are_not_persisted():
  diagnostics, emitted = recorder()
  errors = [subprocess.TimeoutExpired(['tool', 'PRIVATE'], 5),
            subprocess.CalledProcessError(1, ['tool', 'PRIVATE']),
            PermissionError(13, 'PRIVATE', '/PRIVATE'), ValueError('PRIVATE'), ConnectionError('PRIVATE')]
  for error in errors:
    diagnostics.failure(phase='connect', transport='bluetooth', error=error, connected_at=0, wireless=None)
  assert 'PRIVATE' not in json.dumps(emitted)
  assert 'PRIVATE' not in json.dumps(diagnostics.snapshot())


def test_source_state_changes_are_distinct_from_radio_failures():
  diagnostics, emitted = recorder()
  sources = {key: {'usable': True, 'reason': ''}
             for key in ('deviceState', 'pandaStates', 'carState', 'selfdriveState')}
  payload = {'displayPower': 'onroad', 'ambientState': 'driving'}
  diagnostics.sources(sources, payload)
  diagnostics.sources(sources, payload)
  sources['pandaStates'] = {'usable': False, 'reason': 'stale'}
  diagnostics.sources(sources, {'displayPower': 'unknown', 'ambientState': 'disconnected'})
  assert len(emitted) == 2
  assert emitted[1][1]['kind'] == 'source_changed'
  assert emitted[1][1]['state']['missing'] == {'pandaStates': 'stale'}
  assert diagnostics.snapshot()['lastFailure'] is None
  diagnostics.sources(sources, payload, can_timeout=True)
  assert emitted[-1][1]['state']['missing']['carState'] == 'can_timeout'
