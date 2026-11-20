"""Which estimator is in charge: readiness, training, and the promotion gate.

    Phase 1  not enough answers for a deep model       -> BKT
    Phase 2  enough answers; a deep model is trained,   -> BKT (still)
             but has not (yet) beaten BKT
    Phase 3  the deep model beat BKT on held-out        -> deep model
             learners of this course

The decision is recorded in the course workspace's
``knowledge/active_model.json``; the course's deep model lives in its ``kt/``
folder (see course_workspace). Retraining is a
full retrain through the same gate: new questions change the model's
vocabulary, so a promoted model is never patched in place.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .bkt import DEFAULT_PARAMS, MODEL_NAME as BKT_MODEL_NAME, next_answer_predictions
from .data import (
    ANSWER_EVENTS_FILE,
    QUESTION_KC_MAP_FILE,
    cohort_label,
    current_mapping_version,
    detect_population,
    load_answer_events,
    load_kc_catalog,
    load_roster_populations,
    sha256_file,
    write_json,
)

KT_DIR = config.PROJECT_ROOT / "knowledge_tracing"
# Inside a course's kt/ folder: the prepared answer sequences and maps.
PREPARED_SUBDIR = "processed"
DEEP_MODEL = "akt"  # the only model the inference path supports


def default_thresholds():
    return {
        "min_learners_with_answers": config.MIN_LEARNERS_WITH_ANSWERS,
        "min_answer_events": config.MIN_ANSWER_EVENTS,
        "min_answers_per_kc": config.MIN_ANSWERS_PER_KC,
        "min_kc_coverage": config.MIN_KC_COVERAGE,
    }


def readiness(events, kc_catalog, thresholds=None):
    """Is there enough answer data to attempt deep-model training?"""
    limits = {**default_thresholds(), **(thresholds or {})}
    per_kc = events.groupby("kc_id").size() if len(events) else {}
    catalog_kcs = kc_catalog["kc_id"].astype(str).tolist()
    covered = sum(
        1
        for kc in catalog_kcs
        if int(dict(per_kc).get(kc, 0)) >= limits["min_answers_per_kc"]
    )
    counts = {
        "learners_with_answers": (
            int(events["learner_id"].nunique()) if len(events) else 0
        ),
        "answer_events": int(len(events)),
        "catalog_kcs": len(catalog_kcs),
        "kcs_with_enough_answers": covered,
        "kc_coverage": round(covered / len(catalog_kcs), 4) if catalog_kcs else 0.0,
    }
    unmet = []
    if counts["learners_with_answers"] < limits["min_learners_with_answers"]:
        unmet.append(
            f"{counts['learners_with_answers']} learners with answers "
            f"(need {limits['min_learners_with_answers']})"
        )
    if counts["answer_events"] < limits["min_answer_events"]:
        unmet.append(
            f"{counts['answer_events']} answer events (need {limits['min_answer_events']})"
        )
    if counts["kc_coverage"] < limits["min_kc_coverage"]:
        unmet.append(
            f"{counts['kc_coverage']:.0%} of KCs have {limits['min_answers_per_kc']}+ "
            f"answers (need {limits['min_kc_coverage']:.0%})"
        )
    return {"ready": not unmet, "counts": counts, "thresholds": limits, "unmet": unmet}


def decide_promotion(bkt_auc, deep_auc, margin=config.GATE_MIN_AUC_MARGIN):
    """The deep model must beat BKT by ``margin`` AUC on the same targets."""
    if deep_auc is None or bkt_auc is None:
        return False, "gate metrics are unavailable"
    if deep_auc >= bkt_auc + margin:
        return True, (
            f"deep model AUC {deep_auc:.4f} exceeds BKT AUC {bkt_auc:.4f} "
            f"by at least {margin:.3f}"
        )
    return False, (
        f"deep model AUC {deep_auc:.4f} does not exceed BKT AUC {bkt_auc:.4f} "
        f"by {margin:.3f}; BKT stays in charge"
    )


def course_kt_dir(active_model_file):
    """The course's ``kt/`` folder next to its ``knowledge/active_model.json``.

    This is the course-workspace layout (``<course>/knowledge/active_model.json``
    and ``<course>/kt/``), so the model a decision refers to is found from the
    decision itself.
    """
    return Path(active_model_file).resolve().parent.parent / "kt"


def load_active_model(path):
    """The current decision; a missing record (or ``path=None``) means Phase 1 with BKT."""
    if path is None or not Path(path).is_file():
        return {
            "phase": 1,
            "estimator": BKT_MODEL_NAME,
            "reason": "no training has been run",
        }
    return json.loads(Path(path).read_text(encoding="utf-8"))


def events_by_population(data_dir, events):
    """Split answer events by their learner's population.

    Returns ``{population: events}`` with an entry for every population that
    has a roster learner or an answer; a directory with neither gets one empty
    entry for its default population.
    """
    labelled = load_roster_populations(data_dir)
    default = detect_population(data_dir)
    of_event = events["learner_id"].astype(str).map(lambda lid: labelled.get(lid, default))
    present = set(labelled.values()) | set(of_event.unique()) or {default}
    return {
        population: events[of_event.eq(population).to_numpy()]
        for population in sorted(present)
    }


def training_population(populations, requested=None):
    """Which population ``train`` uses: one model is trained on one population.

    Real and simulated answers are never pooled. With both present, the
    simulated cohort is the default (it is what the pipeline demonstration
    trains on); real learners keep BKT until ``--population moodle_collector``
    passes the readiness check and the gate on their own answers.
    """
    populations = sorted(populations)
    if requested is not None:
        if requested not in populations:
            raise ValueError(
                f"No {requested!r} learners in this data directory "
                f"(populations present: {populations})"
            )
        return requested
    if len(populations) == 1:
        return populations[0]
    return config.SIMULATED_POPULATION


def status(data_dir, active_model_file):
    """Live readiness of ``data_dir`` next to the recorded decision.

    ``readiness`` is that of the population ``train`` would use by default;
    ``readiness_by_population`` lists every population in the directory.
    """
    catalog = load_kc_catalog(data_dir)
    split = events_by_population(data_dir, load_answer_events(data_dir))
    by_population = {
        population: readiness(events, catalog) for population, events in split.items()
    }
    default = training_population(split)
    return {
        "data_dir": str(data_dir),
        "population": cohort_label(split),
        "training_population": default,
        "readiness": by_population[default],
        "readiness_by_population": by_population,
        "active_model": load_active_model(active_model_file),
    }


def deep_test_targets(data_config, params=DEFAULT_PARAMS):
    """The deep model's held-out targets, one row each, with BKT's score.

    Reads the pyKT test file (held-out learners, one row per window) and, like
    ``knowledge_tracing.evaluate.baseline_scores``, skips each row's first
    answer and padded positions. BKT starts from its prior in every window, as
    the deep model starts without earlier context. Rows are in the order pyKT
    dumps its predictions: ``test_row`` (window), then position.

    Columns: test_row, learner_id, position (index among the window's valid
    answers), question, concept, response, bkt.
    """
    import pandas as pd

    sys.path.insert(0, str(KT_DIR))
    from evaluate import seq_lists

    test = pd.read_csv(Path(data_config["dpath"]) / data_config["test_file"])
    rows = []
    for test_row, (_, row) in enumerate(test.iterrows()):
        questions = seq_lists(row, "questions")
        concepts = seq_lists(row, "concepts")
        responses = seq_lists(row, "responses")
        masks = seq_lists(row, "selectmasks")
        valid = [i for i, mask in enumerate(masks) if mask != -1]
        scores = next_answer_predictions(
            [concepts[i] for i in valid], [responses[i] for i in valid], params
        )
        learner = str(row["learner_id"]) if "learner_id" in test.columns else str(row["uid"])
        for position in range(1, len(valid)):
            i = valid[position]
            rows.append((test_row, learner, position, questions[i], concepts[i],
                         responses[i], scores[position]))
    targets = pd.DataFrame(rows, columns=[
        "test_row", "learner_id", "position", "question", "concept", "response", "bkt",
    ])
    targets.attrs["n_test_rows"] = int(len(test))
    return targets


def bkt_metrics_on_deep_test_split(data_config, params=DEFAULT_PARAMS):
    """Score BKT on exactly the targets the deep model is evaluated on."""
    import numpy as np

    sys.path.insert(0, str(KT_DIR))
    from evaluate import metrics_for

    targets = deep_test_targets(data_config, params)
    metrics = metrics_for(targets["response"].to_numpy(), np.asarray(targets["bkt"], dtype=float))
    return {**metrics, "n_targets": len(targets), "n_test_rows": targets.attrs["n_test_rows"]}


def _simulator_ceiling_auc(data_dir):
    """The generator's AUC over *all* its answers, when the simulator recorded one.

    Not comparable with the gate's AUCs (other answers); ``oracle`` computes the
    same-target value. Kept as ``oracle_auc_all_answers`` in the record.
    """
    for name in (config.SIMULATION_MANIFEST_NAME, config.PLAN_MANIFEST_NAME):
        path = Path(data_dir) / name
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8")).get("ceiling_auc")
            if value is not None:
                return value
    return None


def _training_events_file(data_dir, events, total_events, prepared_dir):
    """The answer-event CSV the deep toolkit trains on.

    The collector's own file when every answer belongs to the training
    population. In a mixed cohort, a copy holding only that population's rows
    (same columns), so other learners' answers never reach the training set.
    """
    source = Path(data_dir) / ANSWER_EVENTS_FILE
    if len(events) == total_events:
        return source
    import pandas as pd

    keep = set(events["learner_id"].astype(str))
    raw = pd.read_csv(source, dtype="string", encoding="utf-8-sig")
    prepared_dir.mkdir(parents=True, exist_ok=True)
    target = prepared_dir / "training_answer_events.csv"
    raw[raw["learner_id"].isin(keep)].to_csv(target, index=False)
    return target


def _run(script, *args, kt_dir):
    """Run one knowledge_tracing script for the course whose model lives in ``kt_dir``."""
    command = [sys.executable, str(KT_DIR / script), *map(str, args)]
    print(f"\n[knowledge-engine] $ {' '.join(command)}", flush=True)
    env = {**os.environ, "KT_COURSE_DIR": str(Path(kt_dir).resolve())}
    subprocess.run(command, check=True, cwd=config.PROJECT_ROOT, env=env)


def _selected_deep_run(evaluation, selection):
    """Test metrics of the checkpoint chosen on validation AUC."""
    for run in evaluation["runs"]:
        if str(run.get("model")).lower() == DEEP_MODEL and int(
            run.get("seed", -1)
        ) == int(selection["seed"]):
            return run
    raise RuntimeError("The selected checkpoint has no test evaluation")


def train(
    data_dir,
    active_model_file,
    kt_dir=None,
    epochs=None,
    seeds=(42, 43, 44),
    split_seed=20240116,
    force=False,
    population=None,
):
    """Phase 2: train a deep model on the course's answers, then apply the gate.

    One model is trained on the answers of one learner population
    (``population``; see ``training_population`` for the default). The record
    names it, and ``estimate`` uses a promoted model for that population only.

    Returns the record written to ``active_model.json``. If the data is not
    ready (and ``force`` is false), nothing is trained and Phase 1 is recorded.
    """
    data_dir = Path(data_dir)
    kt_dir = Path(kt_dir) if kt_dir is not None else course_kt_dir(active_model_file)
    prepared_dir = kt_dir / PREPARED_SUBDIR
    all_events = load_answer_events(data_dir)
    split = events_by_population(data_dir, all_events)
    population = training_population(split, population)
    events = split[population]
    ready = readiness(events, load_kc_catalog(data_dir))
    record = {
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "training_data_dir": str(data_dir),
        "training_population": population,
        "cohort": cohort_label(split),
        "mapping_version": current_mapping_version(data_dir, all_events),
        "readiness": ready,
        "gate_min_auc_margin": config.GATE_MIN_AUC_MARGIN,
        "oracle_auc_all_answers": _simulator_ceiling_auc(data_dir),
    }
    if not ready["ready"] and not force:
        record.update(
            phase=1,
            estimator=BKT_MODEL_NAME,
            reason="not enough answer data to train a deep model: "
            + "; ".join(ready["unmet"]),
        )
        write_json(active_model_file, record)
        return record

    events_file = _training_events_file(data_dir, events, len(all_events), prepared_dir)
    mapping_file = data_dir / QUESTION_KC_MAP_FILE
    _run(
        "prepare_data.py",
        "--dataset-name",
        config.DEEP_DATASET_NAME,
        "--input",
        events_file,
        "--question-mapping",
        mapping_file,
        "--output-dir",
        prepared_dir,
        "--seed",
        split_seed,
        kt_dir=kt_dir,
    )
    _run(
        "init_pykt_dataset.py",
        "--dataset-name",
        config.DEEP_DATASET_NAME,
        "--input",
        prepared_dir / "pykt_formatted_sequences.csv",
        kt_dir=kt_dir,
    )
    train_args = [
        "--dataset-name",
        config.DEEP_DATASET_NAME,
        "--models",
        DEEP_MODEL,
        "--seeds",
        *seeds,
    ]
    if epochs is not None:
        train_args += ["--epochs", epochs]
    _run("train.py", *train_args, kt_dir=kt_dir)
    _run(
        "evaluate.py",
        "--dataset-name",
        config.DEEP_DATASET_NAME,
        "--models",
        DEEP_MODEL,
        kt_dir=kt_dir,
    )

    from knowledge_tracing.kt_common import (
        dataset_artifacts_dir,
        load_data_config,
        selected_model_path,
        use_course_kt_dir,
    )

    with use_course_kt_dir(kt_dir):
        selection = json.loads(
            selected_model_path(config.DEEP_DATASET_NAME).read_text(encoding="utf-8")
        )
        evaluation = json.loads(
            (
                dataset_artifacts_dir(config.DEEP_DATASET_NAME)
                / "evaluation"
                / "test_comparison.json"
            ).read_text()
        )
        deep = _selected_deep_run(evaluation, selection)
        bkt = bkt_metrics_on_deep_test_split(load_data_config(config.DEEP_DATASET_NAME))
    if bkt["n_targets"] != evaluation["n_targets"]:
        raise RuntimeError(
            f"BKT was scored on {bkt['n_targets']} targets but the deep model on "
            f"{evaluation['n_targets']}; the gate needs identical targets"
        )
    promoted, reason = decide_promotion(bkt["auc"], deep["auc"])
    record.update(
        phase=3 if promoted else 2,
        estimator=DEEP_MODEL if promoted else BKT_MODEL_NAME,
        reason=reason,
        training_data_sha256=sha256_file(events_file),
        gate={
            "n_test_learners": evaluation["n_test_learners"],
            "n_targets": evaluation["n_targets"],
            "bkt": {k: bkt[k] for k in ("auc", "accuracy", "log_loss")},
            "deep": {k: deep[k] for k in ("auc", "accuracy", "log_loss")},
            "other_baselines": evaluation.get("baselines", []),
        },
        deep_model={
            "model_name": selection["model_name"],
            "checkpoint_id": selection["checkpoint_id"],
            "checkpoint": selection["checkpoint"],
            "validation_metrics": selection["validation_metrics"],
        },
    )
    write_json(active_model_file, record)
    return record
