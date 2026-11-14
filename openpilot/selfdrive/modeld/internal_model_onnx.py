"""Read the parts of an ONNX file the internal model manager needs, in pure Python.

get_model_metadata.make_metadata_dict parses the whole graph with tinygrad, which folds scalar initializers on
tinygrad's default device (the GPU on a comma). The manager must not touch the GPU outside its compile child, so
it reads the model's inputs, outputs and metadata here instead, skipping initializers and nodes by length.
"""
from __future__ import annotations

import codecs
import mmap
import pickle
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def _varint(b, i: int) -> tuple[int, int]:
  value = shift = 0
  while True:
    c = b[i]
    i += 1
    value |= (c & 0x7f) << shift
    shift += 7
    if c < 0x80:
      return value, i


def fields(b, start: int, end: int) -> Iterator[tuple[int, int, int, int]]:
  """(field id, wire type, value start, value end) of one protobuf message; varints yield their own byte range."""
  i = start
  while i < end:
    key, i = _varint(b, i)
    fid, wire = key >> 3, key & 7
    if wire == 0:
      _, j = _varint(b, i)
    elif wire == 1:
      j = i + 8
    elif wire == 5:
      j = i + 4
    elif wire == 2:
      n, i = _varint(b, i)
      j = i + n
    else:
      raise ValueError(f"unsupported protobuf wire type {wire}")
    if j > end:
      raise ValueError("truncated protobuf")
    yield fid, wire, i, j
    i = j


def raw_data_offsets(b) -> dict[str, tuple[int, int]]:
  """initializer name -> byte range of its raw_data (ModelProto.graph=7, GraphProto.initializer=5)."""
  out = {}
  for fid, _w, s, e in fields(b, 0, len(b)):
    if fid != 7:
      continue
    for gfid, _gw, gs, ge in fields(b, s, e):
      if gfid != 5:
        continue
      name, raw = None, None
      for tfid, _tw, ts, te in fields(b, gs, ge):
        if tfid == 8:
          name = bytes(b[ts:te]).decode()
        elif tfid == 9:
          raw = (ts, te)
      if name is not None and raw is not None:
        out[name] = raw
  return out


def metadata_offsets(b) -> dict[str, tuple[int, int]]:
  """metadata_props key -> byte range of its value (ModelProto.metadata_props=14)."""
  out = {}
  for fid, _w, s, e in fields(b, 0, len(b)):
    if fid != 14:
      continue
    key, value = None, None
    for mfid, _mw, ms, me in fields(b, s, e):
      if mfid == 1:
        key = bytes(b[ms:me]).decode()
      elif mfid == 2:
        value = (ms, me)
    if key is not None and value is not None:
      out[key] = value
  return out


def _value_info(b, start: int, end: int) -> tuple[str, tuple[int, ...]]:
  """ValueInfoProto (name=1, type=2) -> name and shape; symbolic dims are 0 like get_model_metadata."""
  name, shape = "", []
  for fid, _w, s, e in fields(b, start, end):
    if fid == 1:
      name = bytes(b[s:e]).decode()
    elif fid == 2:  # TypeProto.tensor_type=1 -> TypeProto.Tensor.shape=2 -> TensorShapeProto.dim=1
      for tfid, _tw, ts, te in fields(b, s, e):
        if tfid != 1:
          continue
        for sfid, _sw, ss, se in fields(b, ts, te):
          if sfid != 2:
            continue
          for dfid, _dw, ds, de in fields(b, ss, se):
            if dfid != 1:
              continue
            dim = 0
            for xfid, xwire, xs, _xe in fields(b, ds, de):
              if xfid == 1 and xwire == 0:  # dim_value; dim_param stays 0
                dim = _varint(b, xs)[0]
            shape.append(dim)
  return name, tuple(shape)


def read_metadata(path: str | Path) -> dict[str, Any]:
  """Same result as get_model_metadata.make_metadata_dict, without tinygrad."""
  with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as b:
    inputs: dict[str, tuple[int, ...]] = {}
    outputs: dict[str, tuple[int, ...]] = {}
    props: dict[str, str] = {}
    for fid, _w, s, e in fields(b, 0, len(b)):
      if fid == 7:  # graph: input=11, output=12
        for gfid, _gw, gs, ge in fields(b, s, e):
          if gfid in (11, 12):
            name, shape = _value_info(b, gs, ge)
            (inputs if gfid == 11 else outputs)[name] = shape
    for key, (s, e) in metadata_offsets(b).items():
      props[key] = bytes(b[s:e]).decode()
  output_slices = props.get("output_slices")
  if output_slices is None:
    raise ValueError("output_slices not found in metadata")
  return {
    "model_checkpoint": props.get("model_checkpoint"),
    "output_slices": pickle.loads(codecs.decode(output_slices.encode(), "base64")),
    "input_shapes": inputs,
    "output_shapes": outputs,
  }
