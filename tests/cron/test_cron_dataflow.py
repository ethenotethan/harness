"""Tests for cron dataflow metadata (inputs / outputs / side_effects).

Phase 1 of the cron interflow graph: crons never dispatch to each other — they
communicate through data, so each job declares typed ``scheme:value`` resource
lists and cron→cron edges are inferred from ``cron-output:<id>`` inputs. These
tests cover normalization, the declared-vs-derived cross-checks, referential
integrity, acyclicity, backfill, and the store-wide ``validate_store`` sweep.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Isolated cron environment with temp HERMES_HOME."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "cron").mkdir()
    (hermes_home / "cron" / "output").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import cron.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")

    return hermes_home


class TestResourceListNormalization:
    def test_string_and_list_accepted_and_sorted_deduped(self):
        from cron.jobs import _normalize_resource_list, _INPUT_SCHEMES

        out = _normalize_resource_list(
            ["wiki:b", "file:a", "wiki:b", " file:a "],
            allowed_schemes=_INPUT_SCHEMES,
            field_name="inputs",
        )
        assert out == ["file:a", "wiki:b"]

        single = _normalize_resource_list(
            "https://example.com/x",
            allowed_schemes=_INPUT_SCHEMES,
            field_name="inputs",
        )
        assert single == ["https://example.com/x"]

    def test_none_and_empty_become_empty_list(self):
        from cron.jobs import _normalize_resource_list, _OUTPUT_SCHEMES

        assert _normalize_resource_list(
            None, allowed_schemes=_OUTPUT_SCHEMES, field_name="outputs"
        ) == []
        assert _normalize_resource_list(
            ["", "   "], allowed_schemes=_OUTPUT_SCHEMES, field_name="outputs"
        ) == []

    def test_scheme_lowercased(self):
        from cron.jobs import _normalize_resource_list, _OUTPUT_SCHEMES

        assert _normalize_resource_list(
            "WIKI:Reports/Daily", allowed_schemes=_OUTPUT_SCHEMES, field_name="outputs"
        ) == ["wiki:Reports/Daily"]

    def test_missing_colon_rejected(self):
        from cron.jobs import _normalize_resource_list, _INPUT_SCHEMES

        with pytest.raises(ValueError, match="not a typed reference"):
            _normalize_resource_list(
                "just-a-value", allowed_schemes=_INPUT_SCHEMES, field_name="inputs"
            )

    def test_unknown_scheme_rejected(self):
        from cron.jobs import _normalize_resource_list, _OUTPUT_SCHEMES

        with pytest.raises(ValueError, match="unknown scheme"):
            _normalize_resource_list(
                "telegram:me", allowed_schemes=_OUTPUT_SCHEMES, field_name="outputs"
            )

    def test_missing_value_rejected(self):
        from cron.jobs import _normalize_resource_list, _INPUT_SCHEMES

        with pytest.raises(ValueError, match="missing a value"):
            _normalize_resource_list(
                "wiki:   ", allowed_schemes=_INPUT_SCHEMES, field_name="inputs"
            )

    def test_non_string_entry_rejected(self):
        from cron.jobs import _normalize_resource_list, _INPUT_SCHEMES

        with pytest.raises(ValueError, match="must be 'scheme:value'"):
            _normalize_resource_list(
                [123], allowed_schemes=_INPUT_SCHEMES, field_name="inputs"
            )


class TestCreateStoresDataflow:
    def test_fields_stored_and_normalized(self, cron_env):
        from cron.jobs import create_job, get_job

        job = create_job(
            prompt="collect",
            schedule="every 1h",
            inputs=["https://api.example.com", "wiki:notes"],
            outputs="wiki:reports/daily",
            side_effects=["telegram:me"],
        )
        assert job["inputs"] == ["https://api.example.com", "wiki:notes"]
        assert job["outputs"] == ["wiki:reports/daily"]
        assert job["side_effects"] == ["telegram:me"]

        loaded = get_job(job["id"])
        assert loaded["inputs"] == ["https://api.example.com", "wiki:notes"]
        assert loaded["outputs"] == ["wiki:reports/daily"]
        assert loaded["side_effects"] == ["telegram:me"]

    def test_defaults_to_empty_lists(self, cron_env):
        from cron.jobs import create_job

        job = create_job(prompt="hello", schedule="every 1h")
        assert job["inputs"] == []
        assert job["outputs"] == []
        assert job["side_effects"] == []

    def test_output_scheme_rejected_in_inputs(self, cron_env):
        # telegram is a side-effect scheme; not valid as an input.
        from cron.jobs import create_job

        with pytest.raises(ValueError, match="unknown scheme 'telegram'"):
            create_job(prompt="x", schedule="every 1h", inputs=["telegram:me"])


class TestDeliverSideEffectCrossCheck:
    def test_external_deliver_requires_matching_side_effect(self, cron_env):
        from cron.jobs import create_job

        with pytest.raises(ValueError, match="side_effects declares no 'telegram:'"):
            create_job(
                prompt="x",
                schedule="every 1h",
                deliver="telegram",
                side_effects=["email:team@x.com"],
            )

    def test_matching_side_effect_passes(self, cron_env):
        from cron.jobs import create_job

        job = create_job(
            prompt="x",
            schedule="every 1h",
            deliver="telegram",
            side_effects=["telegram:me"],
        )
        assert job["deliver"] == "telegram"

    def test_undeclared_side_effects_not_blocked(self, cron_env):
        # Empty declaration is the legacy path — do not hard-block existing
        # callers that deliver externally without declaring dataflow.
        from cron.jobs import create_job

        job = create_job(prompt="x", schedule="every 1h", deliver="telegram")
        assert job["side_effects"] == []


class TestContextFromCrossCheck:
    def test_context_from_must_be_declared_input(self, cron_env):
        from cron.jobs import create_job

        upstream = create_job(prompt="produce", schedule="every 1h")
        with pytest.raises(ValueError, match="not declared as inputs"):
            create_job(
                prompt="consume",
                schedule="every 2h",
                context_from=upstream["id"],
                inputs=["wiki:something-else"],
            )

    def test_context_from_with_matching_input_passes(self, cron_env):
        from cron.jobs import create_job

        upstream = create_job(prompt="produce", schedule="every 1h")
        downstream = create_job(
            prompt="consume",
            schedule="every 2h",
            context_from=upstream["id"],
            inputs=[f"cron-output:{upstream['id']}"],
        )
        assert downstream["context_from"] == [upstream["id"]]

    def test_context_from_without_declared_inputs_not_blocked(self, cron_env):
        from cron.jobs import create_job

        upstream = create_job(prompt="produce", schedule="every 1h")
        downstream = create_job(
            prompt="consume", schedule="every 2h", context_from=upstream["id"]
        )
        assert downstream["context_from"] == [upstream["id"]]
        assert downstream["inputs"] == []


class TestReferentialIntegrityAndCycles:
    def test_cron_output_input_must_exist(self, cron_env):
        from cron.jobs import create_job

        with pytest.raises(ValueError, match="unknown job"):
            create_job(
                prompt="x",
                schedule="every 1h",
                inputs=["cron-output:doesnotexist"],
            )

    def test_self_reference_rejected_via_update(self, cron_env):
        from cron.jobs import create_job, update_job

        job = create_job(prompt="x", schedule="every 1h")
        with pytest.raises(ValueError, match="cycle"):
            update_job(job["id"], {"inputs": [f"cron-output:{job['id']}"]})

    def test_two_node_cycle_rejected(self, cron_env):
        from cron.jobs import create_job, update_job

        a = create_job(prompt="a", schedule="every 1h")
        b = create_job(
            prompt="b",
            schedule="every 1h",
            inputs=[f"cron-output:{a['id']}"],
        )
        # Now make A read B's output → A→B→A cycle.
        with pytest.raises(ValueError, match="cycle"):
            update_job(a["id"], {"inputs": [f"cron-output:{b['id']}"]})

    def test_linear_chain_allowed(self, cron_env):
        from cron.jobs import create_job

        a = create_job(prompt="a", schedule="every 1h")
        b = create_job(
            prompt="b", schedule="every 1h", inputs=[f"cron-output:{a['id']}"]
        )
        c = create_job(
            prompt="c", schedule="every 1h", inputs=[f"cron-output:{b['id']}"]
        )
        assert c["inputs"] == [f"cron-output:{b['id']}"]


class TestUpdateJobDataflow:
    def test_update_normalizes_and_stores(self, cron_env):
        from cron.jobs import create_job, update_job

        job = create_job(prompt="x", schedule="every 1h")
        updated = update_job(job["id"], {"outputs": ["WIKI:reports/x", "wiki:reports/x"]})
        assert updated["outputs"] == ["wiki:reports/x"]

    def test_update_clears_with_empty_list(self, cron_env):
        from cron.jobs import create_job, update_job

        job = create_job(
            prompt="x", schedule="every 1h", outputs=["wiki:reports/x"]
        )
        updated = update_job(job["id"], {"outputs": []})
        assert updated["outputs"] == []

    def test_update_bad_scheme_rejected(self, cron_env):
        from cron.jobs import create_job, update_job

        job = create_job(prompt="x", schedule="every 1h")
        with pytest.raises(ValueError, match="unknown scheme"):
            update_job(job["id"], {"inputs": ["bogus:thing"]})


class TestBackfill:
    def test_legacy_record_backfilled_on_read(self, cron_env):
        import json

        from cron.jobs import JOBS_FILE, create_job, get_job

        job = create_job(prompt="x", schedule="every 1h")
        # Simulate a pre-feature record by stripping the fields on disk.
        data = json.loads(JOBS_FILE.read_text())
        for record in data["jobs"]:
            record.pop("inputs", None)
            record.pop("outputs", None)
            record.pop("side_effects", None)
        JOBS_FILE.write_text(json.dumps(data))

        loaded = get_job(job["id"])
        assert loaded["inputs"] == []
        assert loaded["outputs"] == []
        assert loaded["side_effects"] == []


class TestValidateStore:
    def test_clean_store_reports_no_issues(self, cron_env):
        from cron.jobs import create_job, validate_store

        a = create_job(prompt="a", schedule="every 1h")
        create_job(prompt="b", schedule="every 1h", inputs=[f"cron-output:{a['id']}"])
        assert validate_store() == []

    def test_dangling_producer_reported(self, cron_env):
        import json

        from cron.jobs import JOBS_FILE, create_job, validate_store

        a = create_job(prompt="a", schedule="every 1h")
        create_job(prompt="b", schedule="every 1h", inputs=[f"cron-output:{a['id']}"])
        # Delete the producer directly on disk (drift create/update can't catch).
        data = json.loads(JOBS_FILE.read_text())
        data["jobs"] = [r for r in data["jobs"] if r["id"] != a["id"]]
        JOBS_FILE.write_text(json.dumps(data))

        issues = validate_store()
        assert any("unknown job" in i for i in issues)

    def test_cycle_formed_on_disk_reported(self, cron_env):
        import json

        from cron.jobs import JOBS_FILE, create_job, validate_store

        a = create_job(prompt="a", schedule="every 1h")
        b = create_job(prompt="b", schedule="every 1h")
        # Hand-edit both to reference each other → cycle in the stored graph.
        data = json.loads(JOBS_FILE.read_text())
        for record in data["jobs"]:
            if record["id"] == a["id"]:
                record["inputs"] = [f"cron-output:{b['id']}"]
            elif record["id"] == b["id"]:
                record["inputs"] = [f"cron-output:{a['id']}"]
        JOBS_FILE.write_text(json.dumps(data))

        issues = validate_store()
        assert any("cycle" in i for i in issues)
