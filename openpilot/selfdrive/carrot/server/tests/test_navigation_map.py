import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest
from aiohttp import ClientError, web
from aiohttp.test_utils import TestClient, TestServer


@pytest.fixture
def feature(monkeypatch):
  name = 'openpilot.selfdrive.carrot.server.features.navigation_test'
  spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / 'features/navigation.py')
  module = importlib.util.module_from_spec(spec)
  monkeypatch.setitem(sys.modules, name, module)
  spec.loader.exec_module(module)
  values = {}
  monkeypatch.setattr(module, 'get_param_value', lambda key, default: values.get(key, default))
  monkeypatch.setattr(module, 'set_param_value', lambda key, value: values.__setitem__(key, value))
  module.values = values
  return module


@asynccontextmanager
async def client_for(feature, http=None):
  app = web.Application()
  if http is not None:
    app['http'] = http
  feature.register(app)
  async with TestClient(TestServer(app)) as client:
    yield client


def test_destination_roundtrip_clears_waypoints_without_changing_source(feature):
  feature.values.update(CarrotNaviSource=0, NavDestinationWaypoints='[[1,2]]')
  place = {'latitude': 37., 'longitude': 127., 'place_name': 'Test <script>name</script>'}
  async def run():
    async with client_for(feature) as client:
      assert (await client.post('/api/navigation/destination', json=place)).status == 200
      assert json.loads(feature.values['NavDestination']) == place
      assert feature.values['NavDestinationWaypoints'] == ''
      response = await client.get('/api/navigation/destination')
      body = await response.json()
      assert body['destination'] == place
      assert body['navigation_source'] == 'network' and body['destination_editable']
      assert not body['map_ready'] and not body['model_input_ready']
      assert body['pipeline_status'] == 'not_connected'
      assert response.headers['Cache-Control'] == 'no-store'
      assert (await client.delete('/api/navigation/destination')).status == 200
      assert feature.values['NavDestination'] == ''
      assert feature.values['CarrotNaviSource'] == 0
  asyncio.run(run())


@pytest.mark.parametrize('payload', [
  [], {}, None, {'latitude': 91, 'longitude': 127}, {'latitude': 0, 'longitude': 181},
  {'latitude': None, 'longitude': 0}, {'latitude': True, 'longitude': 0},
  {'latitude': '37', 'longitude': 127}, {'latitude': float('nan'), 'longitude': 0},
  {'latitude': 0, 'longitude': float('inf')}, {'latitude': 0, 'longitude': 0, 'place_name': {}},
  {'latitude': 0, 'longitude': 0, 'place_name': 'x' * 257},
  {'latitude': 0, 'longitude': 0, 'place_name': '\nname'},
  {'latitude': 0, 'longitude': 0, 'place_name': 'External Navi'},
])
def test_rejects_invalid_destination_without_writing(feature, payload):
  async def run():
    async with client_for(feature) as client:
      assert (await client.post('/api/navigation/destination', data=json.dumps(payload),
                                headers={'Content-Type': 'application/json'})).status == 400
      assert feature.values == {}
  asyncio.run(run())


@pytest.mark.parametrize('values', [
  {'CarrotNaviSource': 1}, {'CarrotNaviSource': 'invalid'},
  {'NavDestination': json.dumps({'latitude': 37, 'longitude': 127, 'place_name': 'External Navi'})},
])
def test_preserves_navdy_and_external_destination(feature, values):
  feature.values.update(values)
  async def run():
    async with client_for(feature) as client:
      status = await (await client.get('/api/navigation/destination')).json()
      assert not status['destination_editable']
      assert (await client.post('/api/navigation/destination', json={'latitude': 0, 'longitude': 0})).status == 409
      assert (await client.delete('/api/navigation/destination')).status == 409
      assert feature.values == values
  asyncio.run(run())


@pytest.mark.parametrize('headers', [{'Origin': 'http://example.invalid'}, {'Origin': 'null'},
                                    {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}])
def test_rejects_cross_origin_operations(feature, headers):
  async def run():
    async with client_for(feature) as client:
      for method, path in [('GET', 'destination'), ('POST', 'destination'), ('DELETE', 'destination'),
                           ('GET', 'search?q=Seoul'), ('GET', 'map.png')]:
        assert (await client.request(method, '/api/navigation/' + path, headers=headers)).status == 403
      assert feature.values == {}
  asyncio.run(run())


def test_request_size_type_json_and_storage_errors(feature, monkeypatch):
  async def run():
    async with client_for(feature) as client:
      url = '/api/navigation/destination'
      assert (await client.post(url, data='{}')).status == 415
      assert (await client.post(url, data='{', headers={'Content-Type': 'application/json'})).status == 400
      assert (await client.post(url, json={'latitude': 0, 'longitude': 0, 'extra': 'x' * 4096})).status == 413
      def fail(*args):
        raise OSError('private path')
      monkeypatch.setattr(feature, 'set_param_value', fail)
      for response in [await client.post(url, json={'latitude': 0, 'longitude': 0}), await client.delete(url)]:
        assert response.status == 503
        assert 'private path' not in await response.text()
  asyncio.run(run())


def test_malformed_saved_destination_and_missing_key(feature):
  feature.values['NavDestination'] = '{'
  async def run():
    async with client_for(feature) as client:
      assert (await (await client.get('/api/navigation/destination')).json())['destination'] is None
      assert (await client.get('/api/navigation/search?q=Seoul')).status == 409
      for query in ('', 'a', 'x' * 201):
        assert (await client.get('/api/navigation/search', params={'q': query})).status == 400
      response = await client.get('/api/navigation/map.png')
      assert response.status == 204
      assert response.headers['X-Nav-Map-Status'] == 'not_connected'
  asyncio.run(run())


def mock_search(*, payload=None, status=200, error=None, block=None):
  async def chunks(_):
    for i in range(0, len(payload), 7):
      yield payload[i:i + 7]
  @asynccontextmanager
  async def get(url, **kwargs):
    assert url == 'https://api.mapbox.com/search/geocode/v6/forward'
    assert kwargs['params']['access_token'] == 'pk.test-private'
    assert kwargs['params']['permanent'] == 'true'
    assert kwargs['timeout'].total == 8
    assert not kwargs['allow_redirects']
    if block:
      await block.wait()
    if error:
      raise error
    yield NS(status=status, content=NS(iter_chunked=chunks))
  return NS(get=get)


def test_search_fragmented_response_and_rate_limit_without_token_disclosure(feature):
  feature.values['MapboxPublicKey'] = 'pk.test-private'
  payload = json.dumps({'features': [{'geometry': {'coordinates': [127., 37.]},
                                     'properties': {'full_address': 'Test destination'}}]}).encode()
  async def run():
    async with client_for(feature, mock_search(payload=payload)) as client:
      response = await client.get('/api/navigation/search?q=Seoul')
      body = await response.text()
      assert response.status == 200 and 'pk.test-private' not in body
      assert json.loads(body)['results'][0]['latitude'] == 37.
      assert (await client.get('/api/navigation/search?q=Seoul')).status == 429
  asyncio.run(run())


@pytest.mark.parametrize('kwargs', [
  {'payload': b'{bad json'}, {'payload': b'{"features": {}}'},
  {'payload': b'{"features":[{"geometry":{"coordinates":[true,37]}}]}'},
  {'payload': b'x' * (1024 * 1024 + 1)}, {'status': 401}, {'status': 302},
  {'error': ClientError('URL contains pk.test-private')}, {'error': TimeoutError()},
])
def test_bad_search_is_bounded_and_redacted(feature, kwargs):
  feature.values['MapboxPublicKey'] = 'pk.test-private'
  async def run():
    async with client_for(feature, mock_search(**kwargs)) as client:
      response = await client.get('/api/navigation/search?q=Seoul')
      assert response.status == 502
      assert 'pk.test-private' not in await response.text()
  asyncio.run(run())


def test_search_concurrency_limit(feature):
  feature.values['MapboxPublicKey'] = 'pk.test-private'
  async def run():
    event = asyncio.Event()
    async with client_for(feature, mock_search(payload=b'{"features":[]}', block=event)) as client:
      first = asyncio.create_task(client.get('/api/navigation/search?q=Seoul'))
      for _ in range(100):
        if client.app[feature.SEARCH_STATE].lock.locked():
          break
        await asyncio.sleep(.005)
      assert client.app[feature.SEARCH_STATE].lock.locked()
      assert (await client.get('/api/navigation/search?q=Busan')).status == 429
      event.set()
      assert (await first).status == 200
  asyncio.run(run())
