"""Resume of finished KT runs (train.py --skip-existing) and numpy-2 prediction dumps."""

import json

import numpy as np

from knowledge_tracing.evaluate import parse_predictions
from knowledge_tracing.kt_common import EMB_TYPE
from knowledge_tracing.train import finished_run


def test_parse_predictions_accepts_numpy2_scalars_and_nan(tmp_path):
    # pyKT line: [qs, rs, ds, ts, ps, prelabels, auc, acc]; numpy >= 2 prints
    # the per-sequence AUC/ACC as np.float64(...), and AUC is nan for one-class targets.
    lines = [
        "[[1, 2], [1, 0], [], [1, 0], [0.8, 0.3], [1, 0], np.float64(1.0), np.float64(1.0)]",
        "[[3], [1], [], [1], [0.6], [1], nan, np.float64(nan)]",
        "",
    ]
    path = tmp_path / "akt_seed42_test_predictions.txt"
    path.write_text("\n".join(lines))
    y, p = parse_predictions(path)
    assert y.tolist() == [1, 0, 1]
    assert np.allclose(p, [0.8, 0.3, 0.6])


def _write_run(run_dir, mcfg, tcfg, ckpt=True):
    run_dir.mkdir(parents=True)
    (run_dir / "run_summary.json").write_text(
        json.dumps({"model_config": mcfg, "training": tcfg, "valid_auc": 0.7})
    )
    if ckpt:
        (run_dir / f"{EMB_TYPE}_model.ckpt").write_bytes(b"x")


def test_finished_run_reused_only_with_same_config_and_checkpoint(tmp_path):
    mcfg, tcfg = {"d_model": 64}, {"num_epochs": 50, "learning_rate": 0.001}
    _write_run(tmp_path / "ok", mcfg, tcfg)
    assert finished_run(tmp_path / "ok", mcfg, tcfg)["valid_auc"] == 0.7
    # a smoke run with fewer epochs must not stand in for the real one
    assert finished_run(tmp_path / "ok", mcfg, {**tcfg, "num_epochs": 2}) is None
    assert finished_run(tmp_path / "ok", {"d_model": 128}, tcfg) is None
    # killed before the checkpoint / summary was written
    _write_run(tmp_path / "no_ckpt", mcfg, tcfg, ckpt=False)
    assert finished_run(tmp_path / "no_ckpt", mcfg, tcfg) is None
    assert finished_run(tmp_path / "missing", mcfg, tcfg) is None
    (tmp_path / "torn").mkdir()
    (tmp_path / "torn" / "run_summary.json").write_text("{")
    (tmp_path / "torn" / f"{EMB_TYPE}_model.ckpt").write_bytes(b"x")
    assert finished_run(tmp_path / "torn", mcfg, tcfg) is None


def test_extra_model_configs_merge_without_changing_base_configs():
    from knowledge_tracing.kt_common import CONFIG_DIR, load_model_configs

    base = {k: v for k, v in json.loads((CONFIG_DIR / "model_configs.json").read_text()).items()
            if not k.startswith("_")}
    merged = load_model_configs()
    # The curated DKT/SAKT/AKT configs are untouched, so finished runs still match
    # (train.py --skip-existing compares model_config and training exactly).
    for name, cfg in base.items():
        assert merged[name] == cfg
    assert {"dkvmn", "saint"} <= set(merged)
    assert set(merged["dkvmn"]["model_config"]) == {"dim_s", "size_m", "dropout"}
    assert merged["saint"]["model_config"]["seq_len"] == 200
