"""Independent Phase 2 review regressions. No SSH or I2C execution."""

from test_core import make_harness, ready, read


def test_report_does_not_complete_current_generation_using_previous_boot_identity(make_harness):
    h = make_harness()
    run = ready(h)
    assert read(h, run).assessment == "pass"
    h.transport.overrides["inspect_target"] = {"boot_id": "boot-2"}
    h.transport.overrides["inspect_i2c_environment"] = {"boot_id": "boot-2"}
    assert h.core.inspect_target(run).status == "blocked"
    assert h.core.inspect_i2c_environment(run).status == "blocked"
    # Old identity remains historical evidence but is not a probe of boot-2.
    assert h.core.finalize_report(run).outcome == "partial"


def test_durable_intent_delay_cannot_dispatch_after_active_budget(make_harness, monkeypatch):
    h = make_harness(freshness=120)
    run = ready(h)
    h.clock.now += 119
    save_state = h.store.save_state

    def slow_intent_save(run_id, state):
        save_state(run_id, state)
        if (state.get("pending_operation") or {}).get("tool") == "read_device_identity" and state["remote_state"] == "running":
            h.clock.now += 2

    monkeypatch.setattr(h.store, "save_state", slow_intent_save)
    result = read(h, run)
    assert result.status == "blocked"
    assert h.transport.count("read_device_identity") == 0


def test_durable_intent_delay_cannot_dispatch_with_expired_environment(make_harness, monkeypatch):
    h = make_harness()
    run = ready(h)
    h.clock.now += 29
    save_state = h.store.save_state

    def slow_intent_save(run_id, state):
        save_state(run_id, state)
        if (state.get("pending_operation") or {}).get("tool") == "read_device_identity" and state["remote_state"] == "running":
            h.clock.now += 2

    monkeypatch.setattr(h.store, "save_state", slow_intent_save)
    result = read(h, run)
    assert result.status == "blocked"
    assert h.transport.count("read_device_identity") == 0


def test_metadata_only_reinspection_does_not_complete_without_environment(make_harness):
    h = make_harness()
    run = ready(h)
    assert read(h, run).assessment == "pass"
    assert h.core.inspect_target(run).status == "ok"
    assert h.store.load_state(run)["environment"] is None
    assert h.core.finalize_report(run).outcome == "partial"


def test_old_adapter_identity_does_not_cover_new_adapter_environment(make_harness):
    h = make_harness()
    run = ready(h)
    assert read(h, run).assessment == "pass"
    h.transport.overrides["inspect_i2c_environment"] = {"adapter_fingerprint": "c" * 64}
    assert h.core.inspect_i2c_environment(run).status == "ok"
    report = h.core.finalize_report(run)
    assert report.outcome == "partial"
    # Historical evidence remains valid and visible rather than disappearing.
    assert any(f.rule_id == "identity-match" and f.assessment == "pass" for f in report.findings)


def test_failed_target_refresh_cannot_erase_original_boot_binding(make_harness):
    from embedsleuth.transport import ProbeResponse
    h = make_harness()
    run = ready(h)
    assert read(h, run).assessment == "pass"
    h.transport.overrides["inspect_target"] = ProbeResponse(
        status="error", error_code="authentication_failed", message="Simulated refresh failure",
    )
    assert h.core.inspect_target(run).status == "error"
    h.transport.overrides["inspect_target"] = {"boot_id": "boot-2"}
    assert h.core.inspect_target(run).status == "blocked"
    assert h.core.get_report(run).outcome == "partial"
    assert h.transport.count("read_device_identity") == 1


def test_short_configured_ttl_bounds_transport_deadline(make_harness):
    h = make_harness(run_ttl=5)
    run = ready(h)
    h.clock.now += 4
    assert read(h, run).assessment == "pass"
    assert h.transport.calls[-1][1]["timeout"] == 1


def test_ttl_expiring_during_durable_intent_preserves_interrupted_report(make_harness, monkeypatch):
    h = make_harness(run_ttl=5)
    run = ready(h)
    h.clock.now += 4
    save_state = h.store.save_state
    def slow_intent_save(run_id, state):
        save_state(run_id, state)
        if (state.get("pending_operation") or {}).get("tool") == "read_device_identity" and state["remote_state"] == "running":
            h.clock.now += 2
    monkeypatch.setattr(h.store, "save_state", slow_intent_save)
    assert read(h, run).error.code == "run_expired"
    assert h.transport.count("read_device_identity") == 0
    assert h.core.get_report(run).outcome == "interrupted"
