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
