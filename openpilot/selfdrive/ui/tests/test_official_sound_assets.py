"""Asset-only checks also run on hosts without native messaging or audio output."""
import array
import ast
import hashlib
import json
from pathlib import Path
import unittest
import wave


ASSETS = Path(__file__).resolve().parents[2] / 'assets'
MANIFEST = json.loads((ASSETS / 'sounds/official_audio.json').read_text())


class TestOfficialSoundAssets(unittest.TestCase):
  def test_pinned_upstream_and_complete_language_mapping(self):
    self.assertEqual(MANIFEST['commit'], '9fba666fd485983b5849dfc62550fa1fd37fe338')
    self.assertEqual(MANIFEST['repository'], 'https://github.com/commaai/openpilot')
    self.assertEqual(set(MANIFEST['directories']), {'sounds', 'sounds_eng', 'sounds_chs'})
    self.assertEqual(MANIFEST['mapping'], {
      'engage.wav': 'engage.wav', 'engage_tizi.wav': 'engage.wav',
      'disengage.wav': 'disengage.wav', 'disengage_tizi.wav': 'disengage.wav',
      'refuse.wav': 'refuse.wav', 'prompt.wav': 'warning.wav',
      'prompt_distracted.wav': 'dm_warning.wav', 'warning_soft.wav': 'critical.wav',
      'warning_immediate.wav': 'dm_critical.wav',
    })
    self.assertEqual(set(MANIFEST['mapping'].values()), set(MANIFEST['sources']))

  def test_all_copies_match_upstream_lfs_payloads(self):
    for directory in MANIFEST['directories']:
      for target, source in MANIFEST['mapping'].items():
        with self.subTest(directory=directory, target=target):
          data = (ASSETS / directory / target).read_bytes()
          spec = MANIFEST['sources'][source]
          self.assertEqual(data[:4], b'RIFF')
          self.assertEqual(len(data), spec['bytes'])
          self.assertEqual(hashlib.sha256(data).hexdigest(), spec['sha256'])

  def test_payloads_decode_as_nonempty_48khz_mono_pcm16(self):
    for directory in MANIFEST['directories']:
      for target, source in MANIFEST['mapping'].items():
        with self.subTest(directory=directory, target=target):
          spec = MANIFEST['sources'][source]
          with wave.open(str(ASSETS / directory / target), 'rb') as sound:
            self.assertEqual(sound.getcomptype(), 'NONE')
            self.assertEqual(sound.getnchannels(), 1)
            self.assertEqual(sound.getsampwidth(), 2)
            self.assertEqual(sound.getframerate(), 48000)
            self.assertEqual(sound.getnframes(), spec['frames'])
            pcm = sound.readframes(sound.getnframes())
          self.assertEqual(len(pcm), spec['frames'] * 2)
          samples = array.array('h', pcm)
          self.assertGreater(max(abs(value) for value in samples), 0)

  def test_all_existing_sound_routes_resolve_without_generic_fallback(self):
    tree = ast.parse((ASSETS.parent / 'ui/soundd.py').read_text())
    names = {node.elts[0].value for node in ast.walk(tree) if isinstance(node, ast.Tuple)
             and node.elts and isinstance(node.elts[0], ast.Constant)
             and isinstance(node.elts[0].value, str) and node.elts[0].value.endswith('.wav')}
    self.assertTrue(set(MANIFEST['mapping']).issubset(names))
    for directory in MANIFEST['directories']:
      for name in names:
        with self.subTest(directory=directory, filename=name):
          self.assertTrue((ASSETS / directory / name).is_file())

  def test_tizi_aliases_are_latest_engage_and_disengage(self):
    for directory in MANIFEST['directories']:
      for stem in ('engage', 'disengage'):
        with self.subTest(directory=directory, stem=stem):
          self.assertEqual((ASSETS / directory / f'{stem}.wav').read_bytes(),
                           (ASSETS / directory / f'{stem}_tizi.wav').read_bytes())


if __name__ == '__main__':
  unittest.main()
