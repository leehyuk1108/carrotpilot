"""Asset checks need no native messaging modules or audio output device."""
import array
import ast
import hashlib
import json
from pathlib import Path
import wave

import pytest


ASSETS = Path(__file__).resolve().parents[2] / 'assets'
MANIFEST = json.loads((ASSETS / 'sounds/official_audio.json').read_text())


def test_pinned_upstream_and_complete_language_mapping():
  assert MANIFEST['commit'] == '9fba666fd485983b5849dfc62550fa1fd37fe338'
  assert MANIFEST['repository'] == 'https://github.com/commaai/openpilot'
  assert set(MANIFEST['directories']) == {'sounds', 'sounds_eng', 'sounds_chs'}
  assert MANIFEST['mapping'] == {
    'engage.wav': 'engage.wav', 'engage_tizi.wav': 'engage.wav',
    'disengage.wav': 'disengage.wav', 'disengage_tizi.wav': 'disengage.wav',
    'refuse.wav': 'refuse.wav', 'prompt.wav': 'warning.wav',
    'prompt_distracted.wav': 'dm_warning.wav', 'warning_soft.wav': 'critical.wav',
    'warning_immediate.wav': 'dm_critical.wav',
  }
  assert set(MANIFEST['mapping'].values()) == set(MANIFEST['sources'])


@pytest.mark.parametrize('directory', MANIFEST['directories'])
@pytest.mark.parametrize('target,source', MANIFEST['mapping'].items())
def test_all_copies_match_upstream_lfs_payloads(directory, target, source):
  data = (ASSETS / directory / target).read_bytes()
  spec = MANIFEST['sources'][source]
  assert data[:4] == b'RIFF'
  assert len(data) == spec['bytes']
  assert hashlib.sha256(data).hexdigest() == spec['sha256']


@pytest.mark.parametrize('directory', MANIFEST['directories'])
@pytest.mark.parametrize('target,source', MANIFEST['mapping'].items())
def test_payloads_decode_as_nonempty_48khz_mono_pcm16(directory, target, source):
  spec = MANIFEST['sources'][source]
  with wave.open(str(ASSETS / directory / target), 'rb') as sound:
    assert sound.getcomptype() == 'NONE'
    assert sound.getnchannels() == 1
    assert sound.getsampwidth() == 2
    assert sound.getframerate() == 48000
    assert sound.getnframes() == spec['frames']
    pcm = sound.readframes(sound.getnframes())
  assert len(pcm) == spec['frames'] * 2
  assert max(abs(value) for value in array.array('h', pcm)) > 0


@pytest.mark.parametrize('directory', MANIFEST['directories'])
def test_all_existing_sound_routes_resolve_without_generic_fallback(directory):
  tree = ast.parse((ASSETS.parent / 'ui/soundd.py').read_text())
  names = {node.elts[0].value for node in ast.walk(tree) if isinstance(node, ast.Tuple)
           and node.elts and isinstance(node.elts[0], ast.Constant)
           and isinstance(node.elts[0].value, str) and node.elts[0].value.endswith('.wav')}
  assert set(MANIFEST['mapping']).issubset(names)
  for name in names:
    assert (ASSETS / directory / name).is_file(), name


@pytest.mark.parametrize('directory', MANIFEST['directories'])
@pytest.mark.parametrize('stem', ['engage', 'disengage'])
def test_tizi_aliases_are_latest_engage_and_disengage(directory, stem):
  assert (ASSETS / directory / f'{stem}.wav').read_bytes() == (ASSETS / directory / f'{stem}_tizi.wav').read_bytes()
