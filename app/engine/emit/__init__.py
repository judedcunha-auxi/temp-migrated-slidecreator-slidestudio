"""Emission: IR → an editable `.pptx` on the customer's own master.

`pptx.py` (WP4) is the entry point; `shapes.py`, `text.py` and `template.py` (all WP4) hold the
geometry, typography and template work; `charts.py` (StageFlow-derived, licence-gated: D3) makes native charts;
`draw.py` is StageFlow's helper layer, imported as-is (licence-gated: D3). See docs/licensing.md.

The rule that shapes every file here: the output must be *editable*. A picture of a slide would pass
a pixel gate and fail the product.
"""
