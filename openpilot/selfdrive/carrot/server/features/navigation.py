"""Destination management; model-input availability is independent of this UI."""
import asyncio
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
import time
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from ..config import WEB_DIR
from ..services.params import get_param_value, set_param_value

MAX_RESPONSE = 1024 * 1024
MAX_DESTINATION = 4096


@dataclass
class SearchState:
  lock: asyncio.Lock = field(default_factory=asyncio.Lock)
  next_request: float = 0.


SEARCH_STATE = web.AppKey('navigation_search', SearchState)


def reply(payload, status=200):
  return web.json_response(payload, status=status, headers={'Cache-Control': 'no-store'})


def same_origin(request):
  origin = request.headers.get('Origin')
  if request.headers.get('Sec-Fetch-Site', 'same-origin') not in ('same-origin', 'none'):
    raise web.HTTPForbidden(text='same-origin requests only')
  if origin:
    parsed = urlsplit(origin)
    if parsed.scheme != request.scheme or parsed.netloc != request.host:
      raise web.HTTPForbidden(text='same-origin requests only')


def destination(value):
  if not isinstance(value, dict):
    raise ValueError('invalid destination')
  coords = [value['latitude'], value['longitude']]
  if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in coords):
    raise ValueError('invalid coordinates')
  lat, lon = map(float, coords)
  if not math.isfinite(lat) or not math.isfinite(lon) or abs(lat) > 85.05112878 or abs(lon) > 180:
    raise ValueError('invalid coordinates')
  name = value.get('place_name', 'Destination')
  if not isinstance(name, str) or not name.strip() or len(name) > 256 or any(ord(c) < 32 for c in name):
    raise ValueError('invalid name')
  return {'latitude': lat, 'longitude': lon, 'place_name': name.strip()}


def navigation_source():
  value = get_param_value('CarrotNaviSource', 0)
  return {'0': 'network', '1': 'navdy'}.get(str(value), 'unknown')


def stored_destination():
  try:
    raw = get_param_value('NavDestination', '')
    return destination(json.loads(raw)) if raw else None
  except (ValueError, KeyError, TypeError, OverflowError):
    return None


async def page(request):
  return web.FileResponse(Path(WEB_DIR) / 'navigation.html', headers={'Cache-Control': 'no-cache'})


async def search(request):
  same_origin(request)
  query = request.query.get('q', '').strip()
  if not 2 <= len(query) <= 200:
    return reply({'error': 'Enter 2-200 characters.'}, 400)
  token = str(get_param_value('MapboxPublicKey', '') or '').strip()
  if not token:
    return reply({'error': 'Set the Mapbox Public key in Settings > Network & Map.'}, 409)
  state = request.app[SEARCH_STATE]
  if state.lock.locked() or time.monotonic() < state.next_request:
    return reply({'error': 'Please wait before searching again.'}, 429)
  async with state.lock:
    state.next_request = time.monotonic() + 1.
    try:
      params = {'q': query, 'access_token': token, 'limit': '5', 'autocomplete': 'false', 'permanent': 'true'}
      async with request.app['http'].get(
        'https://api.mapbox.com/search/geocode/v6/forward', params=params,
        timeout=aiohttp.ClientTimeout(total=8), allow_redirects=False,
      ) as response:
        if response.status != 200:
          return reply({'error': f'Mapbox search failed (HTTP {response.status}).'}, 502)
        body = bytearray()
        async for chunk in response.content.iter_chunked(65536):
          body.extend(chunk)
          if len(body) > MAX_RESPONSE:
            raise ValueError('oversized response')
        features = json.loads(body)['features']
        if not isinstance(features, list):
          raise ValueError('invalid results')
        results = []
        for feature in features[:5]:
          lon, lat = feature['geometry']['coordinates'][:2]
          props = feature.get('properties', {})
          results.append(destination({'latitude': lat, 'longitude': lon,
                                      'place_name': (props.get('full_address') or props.get('name') or query)[:256]}))
        return reply({'results': results})
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError):
      # Never include the upstream URL or exception: both can contain the token.
      return reply({'error': 'Mapbox search is unavailable.'}, 502)


async def get_destination(request):
  same_origin(request)
  current = stored_destination()
  source = navigation_source()
  return reply({
    'destination': current, 'key_configured': bool(str(get_param_value('MapboxPublicKey', '') or '').strip()),
    'navigation_source': source, 'source_scope': 'configured',
    'destination_editable': source == 'network' and (current or {}).get('place_name') != 'External Navi',
    # This branch has no validated mapsd -> N16 route-generation contract yet.
    # A stored destination, selected N16 model or old PNG is not proof of input.
    'map_ready': False, 'model_input_ready': False, 'pipeline_status': 'not_connected',
    'reason': 'The N16 route/map input pipeline is not connected in this build.',
  })


def destination_write_error():
  if navigation_source() != 'network':
    return reply({'error': 'Navdy navigation is selected. The web cannot replace its destination.'}, 409)
  if (stored_destination() or {}).get('place_name') == 'External Navi':
    return reply({'error': 'An external navigation destination is active. End it in its source app first.'}, 409)
  return None


async def set_destination(request):
  same_origin(request)
  if request.content_type != 'application/json':
    return reply({'error': 'Expected application/json.'}, 415)
  try:
    raw = await request.clone(client_max_size=MAX_DESTINATION).read()
    value = destination(json.loads(raw))
    if value['place_name'] == 'External Navi':
      raise ValueError('reserved destination')
  except (ValueError, KeyError, TypeError, OverflowError):
    return reply({'error': 'Invalid destination.'}, 400)
  blocked = destination_write_error()
  if blocked is not None:
    return blocked
  try:
    # Clear old waypoints before exposing the new destination to navd.
    set_param_value('NavDestinationWaypoints', '')
    set_param_value('NavDestination', json.dumps(value, ensure_ascii=False, allow_nan=False))
  except Exception:
    return reply({'error': 'Could not save destination.'}, 503)
  return reply({'destination': value})


async def clear_destination(request):
  same_origin(request)
  blocked = destination_write_error()
  if blocked is not None:
    return blocked
  try:
    set_param_value('NavDestination', '')
    set_param_value('NavDestinationWaypoints', '')
  except Exception:
    return reply({'error': 'Could not clear destination.'}, 503)
  return reply({'destination': None})


async def preview(request):
  same_origin(request)
  return web.Response(status=204, headers={'Cache-Control': 'no-store', 'X-Nav-Map-Status': 'not_connected'})


def register(app):
  app[SEARCH_STATE] = SearchState()
  app.router.add_get('/navigation', page)
  app.router.add_get('/api/navigation/search', search)
  app.router.add_get('/api/navigation/destination', get_destination)
  app.router.add_post('/api/navigation/destination', set_destination)
  app.router.add_delete('/api/navigation/destination', clear_destination)
  app.router.add_get('/api/navigation/map.png', preview)
