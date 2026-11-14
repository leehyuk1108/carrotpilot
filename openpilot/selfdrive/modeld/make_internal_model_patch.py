#!/usr/bin/env python3
"""Make a weight patch (internal_model_patches.py) that turns the stock driving_supercombo.onnx into a model that
differs only in its policy weights. Runs on a PC with the onnx package; the comma only applies the byte copies.

The stock file combines a vision and a policy export (carrot-cd210-combiner). Upstream published some policies
only in another export (e.g. opset 17 instead of 20), so tensor names differ. The reference policy is the export
of the stock policy weights in the same format as the target policy: each stock tensor is matched to it by value,
and its name picks the target tensor. Every tensor that differs between reference and target must be matched, and
the stock tensors that are left must be equal in both (index constants), or this stops.

  make_internal_model_patch.py stock.onnx reference_policy.onnx target_policy.onnx --out spec.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from openpilot.selfdrive.modeld.internal_model_onnx import metadata_offsets, raw_data_offsets


def make_patch(stock_path: Path, reference_path: Path, target_path: Path) -> dict:
  import onnx
  from onnx import numpy_helper

  def tensors(model):
    out = {t.name: t for t in model.graph.initializer}
    for node in model.graph.node:
      if node.op_type == "Constant":
        for attr in node.attribute:
          if attr.name == "value":
            out["const:" + node.output[0]] = attr.t
    return out

  def key(t):
    a = numpy_helper.to_array(t)
    return tuple(a.shape), str(a.dtype), a.tobytes()

  stock_model, reference, target = (onnx.load(str(p)) for p in (stock_path, reference_path, target_path))
  ref_t, tgt_t = tensors(reference), tensors(target)
  if set(ref_t) != set(tgt_t):
    raise SystemExit("reference and target policies have different tensors")
  changed = {n for n in ref_t if key(ref_t[n]) != key(tgt_t[n])}
  by_value: dict = {}
  for name, t in ref_t.items():
    by_value.setdefault(key(t), []).append(name)

  mapping = {}
  for t in stock_model.graph.initializer:
    if not t.name.startswith("policy/"):
      continue
    names = by_value.get(key(t), [])
    if not names:
      continue  # index constants of the stock export; checked below through `changed`
    targets = {key(tgt_t[n]) for n in names}
    if len(targets) != 1:
      raise SystemExit(f"{t.name}: ambiguous match {names}")
    if names[0] in changed:
      mapping[t.name] = names[0]
  missing = changed - set(mapping.values())
  if missing:
    raise SystemExit(f"target tensors without a stock tensor: {sorted(missing)}")
  if any(n.startswith("const:") for n in mapping.values()):
    raise SystemExit("target weights stored in Constant nodes are not supported")

  stock_b, target_b = stock_path.read_bytes(), target_path.read_bytes()
  s_raw, t_raw = raw_data_offsets(stock_b), raw_data_offsets(target_b)
  copies = []
  for stock_name, target_name in sorted(mapping.items()):
    (ds, de), (ts, te) = s_raw[stock_name], t_raw[target_name]
    if de - ds != te - ts:
      raise SystemExit(f"{stock_name}: {de - ds} bytes != {te - ts}")
    copies.append([ds, ts, de - ds])

  # model_checkpoint "vision+policy": replace the policy part with the target's (same length for uuid/step labels)
  (sms, sme), (tms, tme) = metadata_offsets(stock_b)["model_checkpoint"], metadata_offsets(target_b)["model_checkpoint"]
  label = stock_b[sms:sme].decode()
  plus = label.index("+") + 1
  if len(label) - plus != tme - tms:
    raise SystemExit("checkpoint labels differ in length")
  copies.append([sms + plus, tms, tme - tms])

  out = bytearray(stock_b)
  for dst, src, n in copies:
    out[dst:dst + n] = target_b[src:src + n]
  return {
    "template_sha256": hashlib.sha256(stock_b).hexdigest(), "template_size": len(stock_b),
    "part_sha256": hashlib.sha256(target_b).hexdigest(), "part_size": len(target_b),
    "sha256": hashlib.sha256(out).hexdigest(), "size": len(out), "copies": copies,
  }


if __name__ == "__main__":
  p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  p.add_argument("stock", type=Path)
  p.add_argument("reference_policy", type=Path)
  p.add_argument("target_policy", type=Path)
  p.add_argument("--out", type=Path, required=True)
  args = p.parse_args()
  spec = make_patch(args.stock, args.reference_policy, args.target_policy)
  args.out.write_text(json.dumps(spec))
  print(f"{len(spec['copies'])} copies, result {spec['size']} bytes sha256 {spec['sha256']}")
