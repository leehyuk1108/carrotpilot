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

from openpilot.selfdrive.navigation import preview as nav_preview

from ..config import WEB_DIR
from ..services.params import get_param_value, set_param_value

MAX_RESPONSE = 1024 * 1024
MAX_DESTINATION = 4096
MAX_STATUS = 65536
MODEL_STATUS_PATH = Path('/dev/shm/carrot-jetlink-model.json')
MODEL_STATUS_MAX_AGE = 2.5
MAP_REASONS = frozenset(('ready', 'idle', 'route', 'position', 'instruction', 'tiles',
                         'route_position', 'context_changed', 'slow', 'waiting', 'offroad'))
INPUT_REASONS = frozenset(('ready', 'unsupported_model', 'navigation_not_yet_available',
                           'route_unavailable', 'tbt_unavailable', 'context_expired',
                           'map_not_yet_available', 'navigation_unavailable'))


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
    **navigation_status(source),
  })


def read_bounded(path, limit):
  with path.open('rb') as stream:
    value = stream.read(limit + 1)
  if len(value) > limit:
    raise ValueError('oversized navigation data')
  return value


def map_status(source):
  # Navdy currently provides guidance, not a complete route. In particular, a
  # recent PNG from the previous network source must not become its route.
  if source != 'network':
    return ('route' if source == 'navdy' else 'source_unavailable'), False
  try:
    paths = (nav_preview.PREVIEW_PATH, nav_preview.STATUS_PATH)
    if not all(path.is_file() for path in paths):
      return 'waiting', False
    if not all(nav_preview.preview_fresh(path) for path in paths):
      return 'stale', False
    if nav_preview.STATUS_PATH.stat().st_size > MAX_STATUS:
      return 'unavailable', False
    reason = nav_preview.preview_reason()
    return (reason if reason in MAP_REASONS else 'unavailable'), True
  except (OSError, ValueError, TypeError, AttributeError):
    return 'unavailable', False


def navigation_status(source):
  map_reason, map_fresh = map_status(source)
  model, fresh = {}, False
  reason = 'model_status_missing'
  try:
    model = json.loads(read_bounded(MODEL_STATUS_PATH, MAX_STATUS))
    if not isinstance(model, dict):
      raise ValueError('invalid model status')
    updated = model.get('updated')
    fresh = (type(updated) in (int, float) and math.isfinite(updated) and updated > 0
             and 0 <= time.monotonic() - updated < MODEL_STATUS_MAX_AGE)
    reason = 'model_status_stale'
  except FileNotFoundError:
    pass
  except (OSError, ValueError, TypeError, OverflowError):
    model = {}
    reason = 'model_status_unavailable'
  flags = {key: fresh and model.get(key) is True for key in (
    'active', 'nav_model', 'nav_input_supported', 'nav_route_available', 'nav_tbt_available', 'nav_map_available')}
  if fresh:
    if not flags['active']:
      reason = 'model_inactive'
    elif not flags['nav_model'] or not flags['nav_input_supported']:
      reason = 'unsupported_model'
    else:
      reported = model.get('nav_input_reason')
      reason = reported if isinstance(reported, str) and reported in INPUT_REASONS else 'navigation_unavailable'
      if reason == 'ready':
        if not flags['nav_route_available']:
          reason = 'route_unavailable'
        elif not flags['nav_tbt_available']:
          reason = 'tbt_unavailable'
        elif not flags['nav_map_available']:
          reason = 'map_not_yet_available'
  if source != 'network':
    reason = 'route_unavailable' if source == 'navdy' else 'source_unavailable'
    for key in ('nav_route_available', 'nav_tbt_available', 'nav_map_available'):
      flags[key] = False
  # Preview and inference travel independently. Neither a destination nor PNG
  # proves that a live N16 model has accepted route, guidance and map inputs.
  return {
    'map_ready': map_reason == 'ready', 'map_reason': map_reason, 'map_status_fresh': map_fresh,
    'model_input_ready': reason == 'ready', 'pipeline_status': reason, 'reason': reason,
    'model_status_fresh': fresh, 'model_active': flags.pop('active'), **flags,
  }


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
  # Read-only viewing requests rendering, but never takes destination ownership.
  try:
    nav_preview.WEB_DEMAND_PATH.touch()
  except OSError:
    pass
  source = navigation_source()
  reason, _ = map_status(source)
  headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
  if reason == 'ready':
    try:
      image = read_bounded(nav_preview.PREVIEW_PATH, MAX_RESPONSE)
      if not image.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('invalid preview')
      reason, _ = map_status(navigation_source())
      if reason == 'ready':
        return web.Response(body=image, content_type='image/png', headers={**headers, 'X-Nav-Map-Status': reason})
    except (OSError, ValueError):
      reason = 'unavailable'
  return web.Response(status=204, headers={**headers, 'X-Nav-Map-Status': reason})


def register(app):
  app[SEARCH_STATE] = SearchState()
  app.router.add_get('/navigation', page)
  app.router.add_get('/api/navigation/search', search)
  app.router.add_get('/api/navigation/destination', get_destination)
  app.router.add_post('/api/navigation/destination', set_destination)
  app.router.add_delete('/api/navigation/destination', clear_destination)
  app.router.add_get('/api/navigation/map.png', preview)
