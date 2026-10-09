"""Brand translation: a customer's master <-> `capturedFurniture` JSON.

* `extract` reads a master `.pptx` into the brand: theme colours and fonts, layouts with their
  placeholders, the workzone, and `capturedFurniture` (raw shape XML, media, theme, backgrounds).
* `synth_master` turns `capturedFurniture` back into a real master the engine imports
  (`app.engine.importer.import_master`), with Cover, Divider and Content layouts.
* `furniture_inject` is the OPC-level injector `synth_master` uses.
* `workzone` is the brand's clean content rectangle and the header band above it.

There is no HTTP route in this package: the brand-pptx and master-import flows call `extract`
internally (Slide Studio's `/v1/brand-extract` is gone, migration plan rev 3). Ported from Slide
Studio `server/brand/*` (migration plan §4.1, K).
"""
