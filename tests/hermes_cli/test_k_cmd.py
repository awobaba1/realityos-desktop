"""Smoke tests for the ``hermes k`` I/O adapter (ADR-V6-056).

Wires the CLI handler against a temp PTG store with seeded R9 feeling_events +
entities so ``compute_k_correlations`` runs FOR REAL (pure stats, no LLM double).
This is the **load-bearing** test for A1's 做了没发 finding (2026-07-20): it
proves compute is now reachable via CLI AND writes real edges AND show renders
them — i.e. the K-domain is no longer write-only-no-consumer.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_cli import k_cmd
from plugins.memory.ptg.store import PTGStore


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    db_path = str(tmp_path / "ptg.db")
    monkeypatch.setattr(k_cmd, "load_ptg_config", lambda: {})
    monkeypatch.setattr(k_cmd, "resolve_db_path", lambda _cfg: db_path)
    s = PTGStore(db_path=db_path)
    s.ensure_founder("u1", "founder@realityos.local")
    s.ensure_self_entity("u1")
    s._conn.execute(
        "INSERT OR REPLACE INTO ptg_meta(key, value) VALUES ('founder_user_id', ?)",
        ("u1",))
    yield s
    s.close()


def _seed_r9(store, entity_name, n_neg, n_pos):
    """Seed n_neg negative + n_pos positive R9 feeling_events for one entity."""
    store.upsert_entity(user_id="u1", entity_name=entity_name, entity_type="person")
    for polarity, count, direction in (("negative", n_neg, "down"),
                                       ("positive", n_pos, "up")):
        for i in range(count):
            store.insert_feeling_event(
                user_id="u1", source_text=f"{entity_name}-{polarity}-{i}",
                confidence_base=0.9, relation_confidence=0.9,
                state_type="mood", direction=direction, intensity="medium",
                emotion_vad=json.dumps({"valence": polarity}),
                trigger_source=json.dumps({"entity": entity_name}),
                atom_kind="R9")


def test_k_compute_writes_real_edges(temp_store, capsys):
    """Load-bearing: compute via CLI writes REAL K edges (cures 做了没发).

    Seed 张三 (10 neg + 2 pos) + 李四 (2 neg + 10 pos). Baseline P(neg)=0.5;
    张三 P(neg)=10/12=0.83, lift=0.83/0.5=1.67≥1.2 → negative edge; 李四
    P(neg)=2/12=0.17, lift=0.17/0.5=0.33≤0.83 → positive edge. Both sample=12≥10
    (F6 gate). Expect exactly 2 edges written + persisted.
    """
    _seed_r9(temp_store, "张三", 10, 2)
    _seed_r9(temp_store, "李四", 2, 10)
    rc = k_cmd.cmd_k(SimpleNamespace(k_command="compute", user_id=None))
    assert rc == 0
    out = capsys.readouterr().out
    assert "确认 2 条" in out
    # Edges really landed in the store (not just printed) — the A1 cure.
    edges = temp_store.k_correlation_edges("u1")
    assert len(edges) == 2
    pols = {e["object_name"]: e["polarity"] for e in edges}
    assert pols["张三"] == "negative"
    assert pols["李四"] == "positive"
    # Lift + sample size persisted in delta for the consumer.
    zhang = next(e for e in edges if e["object_name"] == "张三")
    assert zhang["lift"] is not None and zhang["sample_size"] == 12


def test_k_compute_cold_start_zero(temp_store, capsys):
    """Empty store → compute returns 0, honest message (not failure)."""
    rc = k_cmd.cmd_k(SimpleNamespace(k_command="compute", user_id=None))
    assert rc == 0
    assert "未确认新边" in capsys.readouterr().out


def test_k_compute_below_sample_gate(temp_store, capsys):
    """<10 samples per entity → no edge (F6 ≥10 gate), honest zero."""
    _seed_r9(temp_store, "王五", 3, 1)  # only 4 events → below ≥10 gate
    rc = k_cmd.cmd_k(SimpleNamespace(k_command="compute", user_id=None))
    assert rc == 0
    assert "未确认新边" in capsys.readouterr().out
    assert temp_store.k_correlation_edges("u1") == []


def test_k_show_renders_edges(temp_store, capsys):
    """show renders compute's output — the consumer that cures write-only."""
    _seed_r9(temp_store, "张三", 10, 2)
    _seed_r9(temp_store, "李四", 2, 10)
    k_cmd.cmd_k(SimpleNamespace(k_command="compute", user_id=None))
    capsys.readouterr()  # drain compute output
    rc = k_cmd.cmd_k(SimpleNamespace(k_command="show", user_id=None))
    assert rc == 0
    out = capsys.readouterr().out
    assert "K_Correlation 边" in out
    assert "张三：负偏" in out
    assert "李四：正偏" in out
    assert "lift" in out and "n=" in out
    assert "相关性≠因果" in out  # the caveat is always stated (PRD 01:93)


def test_k_show_empty_state(temp_store, capsys):
    """No edges → honest empty state, never fabricated."""
    rc = k_cmd.cmd_k(SimpleNamespace(k_command="show", user_id=None))
    assert rc == 0
    assert "尚无边" in capsys.readouterr().out


def test_k_no_action_prints_usage(temp_store, capsys):
    rc = k_cmd.cmd_k(SimpleNamespace(k_command=None))
    assert rc == 0
    assert "k compute" in capsys.readouterr().out
