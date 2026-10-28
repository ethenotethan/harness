"""Fork-owned schema guard: cron jobs link their graph node to the code they run.

Upstream removed its own version of this file in a low-value-test purge; the
fork keeps this one check because `source_files` is what the cron dataflow graph
(Portal's runtime graph) is built from.
"""


def test_cronjob_schema_declares_source_files():
    """`source_files` is an array of paths and the tool description asks for it,
    so an agent creating a job links its node to the code it runs."""
    from tools.cronjob_tools import CRONJOB_SCHEMA

    props = CRONJOB_SCHEMA["parameters"]["properties"]
    assert props["source_files"]["type"] == "array"
    assert props["source_files"]["items"] == {"type": "string"}
    assert "script" in props["source_files"]["description"]
    assert "source_files" in CRONJOB_SCHEMA["description"]
