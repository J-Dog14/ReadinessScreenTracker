"""Readiness Screen v2 c3d processing, vendored from OctaneBiomechBackend
(uais/python/athleticScreen/v2: c3d_reader, kinetics, shape, session_meta, schema->as_schema;
 uais/python/readinessScreen/v2: isometric, scan_sessions->process, schema).
Keep PROCESSOR_VERSION and the math in sync with the backend - both write the same warehouse tables."""
PROCESSOR_VERSION = "rs2.0.0"
