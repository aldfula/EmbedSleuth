"""Fixed MPU6050 sampling procedure, local plan approval and deterministic units."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math

from embedsleuth.contracts import ToolResult
from embedsleuth.policy import POLICY_VERSION
from embedsleuth.profiles import load_component_profile


def procedure():
    return {"revision": "0.2.0", "samples": 5, "interval_ms": 50, "settle_ms": 100,
            "data_register": 59, "length": 14,
            "wake_register": 107, "clear_mask": 64, "restore_original": True,
            "reset_allowed": False, "range_changes_allowed": False,
            "limitations": ["Reads may acknowledge configured interrupt status.",
                            "Power or transport loss can prevent confirmed restoration.",
                            "Repeated snapshots do not establish independent fresh sensor updates."]}


def plan_identifier(plan):
    return "sampling-" + hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def valid_plan(plan, now):
    try:
        return (type(plan) is dict and plan.get("procedure") == procedure()
                and plan.get("policy_version") == POLICY_VERSION
                and datetime.fromisoformat(plan["created_at"]) <= now < datetime.fromisoformat(plan["expires_at"]))
    except (KeyError, TypeError, ValueError):
        return False


def decode_samples(observations):
    """Validate fixed raw bytes before deriving values; never classify health."""
    config, raw = observations["configuration"], observations["samples"]
    if (type(config) is not dict or type(raw) is not list or len(raw) != 5
            or type(config.get("gyro_config")) is not int or type(config.get("accel_config")) is not int
            or any(not 0 <= config[key] <= 255 or config[key] & 0xe7
                   for key in ("gyro_config", "accel_config"))):
        raise ValueError("Invalid sampling configuration")
    power = config.get("pwr_mgmt_1")
    if (type(power) is not int or not 0 <= power <= 255 or power & 0xb8 or (power & 7) > 3
            or config.get("user_ctrl") != 0 or config.get("pwr_mgmt_2") != 0
            or type(observations.get("wakeup_attempted")) is not bool
            or observations["wakeup_attempted"] != bool(power & 0x40)
            or observations.get("restoration_confirmed") is not True
            or (power & 0x40 and (type(observations.get("restored_pwr_mgmt_1")) is not int
                                 or observations["restored_pwr_mgmt_1"] != power))):
        raise ValueError("Invalid restoration evidence")
    accel = (16384, 8192, 4096, 2048)[(config["accel_config"] >> 3) & 3]
    gyro = (131, 65.5, 32.8, 16.4)[(config["gyro_config"] >> 3) & 3]
    samples = []
    for packet in raw:
        if (type(packet) is not list or len(packet) != 14
                or any(type(value) is not int or not 0 <= value <= 255 for value in packet)):
            raise ValueError("Invalid sampling bytes")
        words = [int.from_bytes(bytes(packet[index:index+2]), "big", signed=True) for index in range(0, 14, 2)]
        samples.append({"raw": words, "acceleration_g": [value / accel for value in words[:3]],
                        "temperature_c": words[3] / 340 + 36.53,
                        "angular_velocity_dps": [value / gyro for value in words[4:]]})
    warnings = []
    if all(sample["raw"] == samples[0]["raw"] for sample in samples):
        warnings.append("identical_snapshots_do_not_prove_a_fault")
    if any(abs(value) >= 32767 for sample in samples for value in sample["raw"]):
        warnings.append("near_digital_limit")
    if any(not -40 <= sample["temperature_c"] <= 85 for sample in samples):
        warnings.append("temperature_outside_operating_range_is_not_a_calibrated_ambient_measurement")
    return {"samples": samples, "warnings": warnings, "health_assessment": "unknown",
            "acceleration_norm_g": [math.sqrt(sum(value*value for value in sample["acceleration_g"]))
                                    for sample in samples]}


class SamplingMixin:
    def _sampling_ready(self, state, target, bus_ref, address):
        from embedsleuth.core import SessionError
        if state["mode"] != "sensor_check":
            raise SessionError("policy_denied", "Sampling requires an explicit sensor_check run")
        if denied := self._preflight(state, target):
            raise SessionError(*denied)
        env = state["environment"] or {}
        profile = load_component_profile(state["profile_id"])
        identities = [operation["result"] for operation in state["operations"].values()
                      if operation["arguments"] == {"bus_ref": bus_ref, "address": address}]
        if (profile.version != state["profile_version"] or profile.review_status != "reviewed"
                or profile.sampling is None or profile.sampling.review_status != "reviewed"
                or self.store.is_quarantined(state["target_id"]) or not target.conflicting_use_excluded
                or bus_ref != "bus-" + str(target.expected_bus) or address not in target.expected_addresses
                or not (state["binding"] or {}).get("expected_connection")
                or not 0 <= self.clock() - state.get("environment_at", float("-inf")) < self.freshness
                or env.get("adapter_present") is not True or env.get("access_allowed") is not True
                or env.get("combined_transfer_supported") is not True or env.get("dependency_available") is not True
                or type(env.get("driver_bound")) is not dict
                or env["driver_bound"].get(str(address)) is not False
                or not env.get("adapter_fingerprint") or env.get("bus") != target.expected_bus
                or not any(item["status"] == "ok" and item["assessment"] == "pass"
                           and self._same_binding(state, item["observations"])
                           and item["observations"].get("adapter_fingerprint") == env["adapter_fingerprint"]
                           for item in identities)):
            raise SessionError("policy_denied", "Fresh inspected environment and matching identity are required")

    def prepare_sensor_sampling(self, run_id, bus_ref, address):
        with self._locked(run_id) as (state, target):
            self._sampling_ready(state, target, bus_ref, address)
            now = self.utcnow()
            plan = {"run_id": run_id, "target_id": state["target_id"], "bus_ref": bus_ref, "address": address,
                    "registration_digest": state["target_digest"], "profile_version": state["profile_version"],
                    "policy_version": POLICY_VERSION, "boot_id": state["binding"]["boot_id"],
                    "host_key_fingerprint": state["binding"]["host_key_fingerprint"],
                    "adapter_fingerprint": state["environment"]["adapter_fingerprint"],
                    "created_at": now.isoformat(), "expires_at": (now + timedelta(seconds=120)).isoformat(),
                    "procedure": procedure()}
            plan_id = plan_identifier(plan)
            current = state.get("sampling_plan")
            if current and current.get("consumed"):
                from embedsleuth.core import SessionError
                raise SessionError("policy_denied", "This run has already consumed a sampling plan")
            state["sampling_plan"] = {"plan_id": plan_id, "plan": plan, "approved": False, "consumed": False}
            state["state"] = "sampling_planned"
            self.store.save_state(run_id, state)
            self._write_report(state)
            return {"run_id": run_id, "plan_id": plan_id, "plan": plan, "approval_required": True}

    def sample_sensor_data(self, run_id, plan_id, operation_id):
        from embedsleuth.core import SessionError
        with self._locked(run_id) as (state, target):
            arguments = {"tool": "sample_sensor_data", "plan_id": plan_id}
            existing = state["operations"].get(operation_id)
            if existing:
                if existing["arguments"] != arguments:
                    return self._deny(state, "operation_conflict", "Operation ID names another request")
                return ToolResult.model_validate_json(json.dumps(existing["result"]))
            saved = state.get("sampling_plan") or {}
            plan = saved.get("plan", {})
            if (saved.get("plan_id") != plan_id or plan_identifier(plan) != plan_id
                    or saved.get("approved") is not True or saved.get("consumed") is not False
                    or not valid_plan(plan, self.utcnow())
                    or plan.get("run_id") != run_id or plan.get("target_id") != state["target_id"]
                    or plan.get("registration_digest") != self._target_digest(target)
                    or plan.get("profile_version") != state["profile_version"]
                    or any(plan.get(key) != (state["binding"] or {}).get(key)
                           for key in ("boot_id", "host_key_fingerprint"))
                    or plan.get("adapter_fingerprint") != (state["environment"] or {}).get("adapter_fingerprint")):
                return self._deny(state, "policy_denied", "A current unused local approval for this plan is required")
            try:
                self._sampling_ready(state, target, plan["bus_ref"], plan["address"])
            except SessionError as exc:
                return self._deny(state, exc.code, str(exc))
            saved["consumed"] = True
            self.store.save_state(run_id, state)
            request = {"op": "sample_sensor_data", "bus": target.expected_bus,
                       "addresses": list(target.expected_addresses), "address": plan["address"],
                       "expected_boot_id": plan["boot_id"], "expected_adapter_fingerprint": plan["adapter_fingerprint"],
                       "plan_id": plan_id}
            response = self._remote(state, target, "sample_sensor_data", request, operation_id)
            observations = response.observations
            status, code, message, assessment = response.status, response.error_code, response.message, "unknown"
            if status == "ok":
                try:
                    if (response.truncated or response.remote_state_unknown or not self._same_binding(state, observations)
                            or observations.get("adapter_fingerprint") != plan["adapter_fingerprint"]
                            or observations.get("address") != plan["address"]
                            or observations.get("plan_id") != plan_id
                            or observations.get("restoration_confirmed") is not True):
                        raise ValueError("Unbound or unconfirmed sampling observation")
                    observations = observations | {"decoded": decode_samples(observations)}
                    assessment = "pass"  # The bounded acquisition, never sensor health.
                    state["state"] = "sampled"
                except (KeyError, TypeError, ValueError):
                    status, code, message = "error", "remote_state_unknown", "Sampling or restoration evidence was invalid"
                    self._quarantine(state)
            if status != "ok":
                state["state"], state["environment"] = "blocked", None
                if observations.get("wakeup_attempted") and observations.get("restoration_confirmed") is not True:
                    self._quarantine(state)
            return self._record(state, status=status, assessment=assessment, observations=observations,
                                code=code, message=message, truncated=response.truncated, operation_id=operation_id)


def approve_plan(store, registry, run_id, plan_id, *, now=None):
    """Trusted local operator API, deliberately absent from MCP contracts."""
    from embedsleuth.core import DiagnosticCore
    now = now or datetime.now(timezone.utc)
    initial = store.load_state(run_id)
    with store.target_lock(initial["target_id"]), store.run_lock(run_id):
        state = store.load_state(run_id)
        saved = state.get("sampling_plan") or {}
        plan = saved.get("plan", {})
        if (state["mode"] != "sensor_check" or state["state"] not in ("identified", "probe_ready", "sampling_planned")
                or state["pending_operation"] is not None or state["remote_state"] != "idle"
                or store.cancel_requested(run_id) or store.is_quarantined(state["target_id"])
                or plan_identifier(plan) != plan_id or saved.get("plan_id") != plan_id
                or plan.get("run_id") != run_id or plan.get("target_id") != state["target_id"]
                or saved.get("consumed") is not False or not valid_plan(plan, now)
                or plan.get("registration_digest") != DiagnosticCore._target_digest(registry.get(state["target_id"]))):
            raise ValueError("Plan cannot be approved; request a fresh plan")
        saved["approved"] = True
        saved["approved_at"] = now.isoformat()
        store.append_event(run_id, {"action": "local_sampling_approval", "plan_id": plan_id, "at": now.isoformat()})
        store.save_state(run_id, state)
