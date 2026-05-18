import json

from layer2 import state


def test_phase1_init_and_load(tmp_path, monkeypatch):
    pc = tmp_path / "phase_config.json"
    pc.write_text(json.dumps({"phase": 1, "active": True}))
    monkeypatch.setattr(state, "PHASE_CONFIG_PATH", pc)

    state._phase1_init(first_reward=9000.0, fixed_risk=2000.0,
                        stages=[109000.0, 109500.0, 110000.0])
    p1 = state._phase1_load()
    assert p1["first_reward"] == 9000.0
    assert p1["fixed_risk"] == 2000.0
    assert p1["stages"] == [109000.0, 109500.0, 110000.0]
    assert p1["active_stage_index"] == 0
    # persisted to disk
    on_disk = json.loads(pc.read_text())
    assert on_disk["phase1"]["fixed_risk"] == 2000.0


def test_save_phase_preserves_phase1_block_when_caller_omits_it(tmp_path, monkeypatch):
    """Regression: /resume, /stop etc. serialize the in-memory _phase_state dict,
    which never carries the 'phase1' sub-block. Saving it must NOT wipe the
    phase1 reward:risk/stages block written separately by _phase1_init."""
    pc = tmp_path / "phase_config.json"
    pc.write_text(json.dumps({"phase": 1, "active": True}))
    monkeypatch.setattr(state, "PHASE_CONFIG_PATH", pc)

    state._phase1_init(9000.0, 2000.0, [109000.0, 109500.0, 110000.0])

    # Simulate the exact /resume write: a _phase_state-shaped dict with NO phase1 key.
    resume_state = {"phase": 1, "active": True, "last_signal_ts": "never"}
    state._save_phase(resume_state)

    p1 = state._phase1_load()
    assert p1.get("stages") == [109000.0, 109500.0, 110000.0]
    assert p1.get("fixed_risk") == 2000.0


def test_save_phase_still_drops_caller_removed_toplevel_keys(tmp_path, monkeypatch):
    """The fix must be targeted: top-level keys the caller intentionally popped
    (e.g. /resume pops 'daily_halted') must still be removed from disk. Only the
    subsystem-owned 'phase1' block is carried forward."""
    pc = tmp_path / "phase_config.json"
    pc.write_text(json.dumps({"phase": 1, "active": True}))
    monkeypatch.setattr(state, "PHASE_CONFIG_PATH", pc)
    state._phase1_init(9000.0, 2000.0, [109000.0, 109500.0, 110000.0])

    # Disk now has phase1 + a transient daily_halted flag.
    state._save_phase({"phase": 1, "active": True, "daily_halted": True})
    assert json.loads(pc.read_text())["daily_halted"] is True

    # /resume pops daily_halted and saves the _phase_state dict without it.
    state._save_phase({"phase": 1, "active": True})

    on_disk = json.loads(pc.read_text())
    assert "daily_halted" not in on_disk          # pop honoured
    assert on_disk["phase1"]["stages"] == [109000.0, 109500.0, 110000.0]  # block kept


def test_save_phase_ignores_stale_phase1_snapshot_after_restart(tmp_path, monkeypatch):
    """Regression: after a service restart, _phase_state = _load_phase() carries a
    frozen 'phase1' snapshot. A _phase_state save (/resume, signal path, …) must
    NOT write that stale snapshot over a ratchet the running bot has advanced."""
    pc = tmp_path / "phase_config.json"
    pc.write_text(json.dumps({"phase": 1, "active": True}))
    monkeypatch.setattr(state, "PHASE_CONFIG_PATH", pc)
    state._phase1_init(9000.0, 2000.0, [109000.0, 109500.0, 110000.0])

    # Bot runs: ratchet advances on disk to stage 1.
    assert state._phase1_active_stage([109000.0, 109500.0, 110000.0], 109000.0) == 1
    assert json.loads(pc.read_text())["phase1"]["active_stage_index"] == 1

    # Service restarts: _phase_state reloaded earlier still holds the OLD snapshot.
    stale_phase_state = {
        "phase": 1, "active": True,
        "phase1": {"first_reward": 9000.0, "fixed_risk": 2000.0,
                   "stages": [109000.0, 109500.0, 110000.0],
                   "active_stage_index": 0, "max_prop_lots": 0.0,
                   "profitable_days": 0, "last_stage_day": "never"},
    }
    state._save_phase(stale_phase_state)   # e.g. /resume

    # The ratchet must NOT revert.
    assert json.loads(pc.read_text())["phase1"]["active_stage_index"] == 1


def test_phase1_active_stage_ratchets_and_persists(tmp_path, monkeypatch):
    pc = tmp_path / "phase_config.json"
    pc.write_text(json.dumps({"phase": 1, "active": True}))
    monkeypatch.setattr(state, "PHASE_CONFIG_PATH", pc)
    state._phase1_init(9000.0, 2000.0, [109000.0, 109500.0, 110000.0])

    assert state._phase1_active_stage([109000.0, 109500.0, 110000.0], 100000.0) == 0
    # reaching S1 advances the persisted pointer
    assert state._phase1_active_stage([109000.0, 109500.0, 110000.0], 109000.0) == 1
    assert json.loads(pc.read_text())["phase1"]["active_stage_index"] == 1
    # a loss never reverts it
    assert state._phase1_active_stage([109000.0, 109500.0, 110000.0], 107000.0) == 1
