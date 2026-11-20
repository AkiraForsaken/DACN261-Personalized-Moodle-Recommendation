"""Produce the learner-by-KC knowledge states for one data directory."""

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import bkt, config, deep
from .data import (
    cohort_label,
    current_mapping_version,
    detect_population,
    load_answer_events,
    load_kc_catalog,
    load_roster_ids,
    load_roster_populations,
    normalize_as_of,
    write_json,
)
from .phase import course_kt_dir, load_active_model
from .schemas import KNOWLEDGE_STATE_COLUMNS, validate_states

ESTIMATORS = ("bkt", "deep")


def choose_estimator(active_model, population, mapping_version):
    """Return ``("deep" | "bkt", reason)`` for the learners of one population.

    The promoted deep model is used only for the population and question
    mapping it was trained on. Anything else falls back to BKT, so a model
    trained on simulated learners is never applied to real ones, and a changed
    question bank forces a retrain instead of silent mis-scoring.
    """
    if active_model.get("phase") != 3:
        return "bkt", active_model.get("reason", "no promoted deep model")
    if active_model.get("training_population") != population:
        return "bkt", (
            f"the promoted model was trained on {active_model.get('training_population')!r} "
            f"data, not {population!r}"
        )
    if active_model.get("mapping_version") != mapping_version:
        return (
            "bkt",
            "the question-to-KC mapping changed since training; retrain required",
        )
    return "deep", active_model.get("reason", "promoted deep model")


def _decide(active_model, populations, mapping_version, force_estimator):
    """``{population: (estimator, reason)}`` for every population present."""
    if force_estimator is not None and force_estimator not in ESTIMATORS:
        raise ValueError(
            f"force_estimator must be one of {ESTIMATORS}, got {force_estimator!r}"
        )
    decisions = {}
    for population in sorted(set(populations)):
        chosen, reason = choose_estimator(active_model, population, mapping_version)
        if force_estimator is not None and chosen != force_estimator:
            if force_estimator == "deep":
                raise ValueError(
                    f"The deep model cannot be forced for {population!r} learners: {reason}"
                )
            chosen, reason = "bkt", "BKT was requested explicitly (force_estimator)"
        decisions[population] = (chosen, reason)
    return decisions


def estimate(
    data_dir,
    output_dir,
    as_of=None,
    active_model_file=None,
    inference_timestamp=None,
    force_estimator=None,
    kt_dir=None,
):
    """Write ``learner_kc_state.csv`` and its manifest; return the states.

    The estimator is chosen per learner population (see ``choose_estimator``).
    In a cohort that mixes real and simulated learners, each learner is scored
    by the estimator its own population is entitled to, and every row carries
    that learner's ``source_population``.

    ``force_estimator="bkt"`` ignores a promoted deep model. Offline
    evaluation uses it for a variant whose estimates cannot have seen the
    future through trained weights.

    ``active_model_file`` is the course's decision record (None: no promoted
    model, BKT for everyone). A promoted deep model is loaded from ``kt_dir``,
    by default the course's ``kt/`` folder next to that record.
    """
    data_dir, output_dir = Path(data_dir), Path(output_dir)
    cutoff = normalize_as_of(as_of)
    as_of_iso = None if cutoff is None else cutoff.isoformat()
    inference_timestamp = inference_timestamp or datetime.now(timezone.utc).isoformat()
    events = load_answer_events(data_dir, as_of_iso)
    mapping_version = current_mapping_version(data_dir, events)
    active = load_active_model(active_model_file)

    learner_ids = sorted(
        set(load_roster_ids(data_dir, as_of_iso))
        | set(events["learner_id"].astype(str))
    )
    labelled = load_roster_populations(data_dir)
    default_population = detect_population(data_dir)

    def population(learner_id):
        """The roster's label for the learner, else the directory's."""
        return labelled.get(str(learner_id), default_population)

    # A course with no learner yet still gets a (default-population) decision.
    present = {population(learner_id) for learner_id in learner_ids} or {
        default_population
    }
    decisions = _decide(active, present, mapping_version, force_estimator)
    deep_populations = {p for p, (chosen, _) in decisions.items() if chosen == "deep"}

    frames = []
    if deep_populations:
        deep_states = deep.estimate_states(
            data_dir,
            output_dir / "deep_export",
            kt_dir if kt_dir is not None else course_kt_dir(active_model_file),
            as_of=as_of_iso,
            mapping_version=mapping_version,
        )
        # The export scores every learner in the directory; keep only those
        # whose population the model was promoted for.
        keep = deep_states["learner_id"].map(population).isin(deep_populations)
        frames.append(deep_states.loc[keep])
    bkt_learners = [
        learner_id
        for learner_id in learner_ids
        if population(learner_id) not in deep_populations
    ]
    if bkt_learners or not deep_populations:
        frames.append(
            bkt.estimate_states(
                events[events["learner_id"].astype(str).isin(bkt_learners)],
                load_kc_catalog(data_dir),
                bkt_learners,
                inference_timestamp,
                as_of=as_of_iso,
                mapping_version=mapping_version,
            )
        )

    frames = [frame for frame in frames if len(frame)] or frames[:1]
    if len(frames) == 1:
        states = frames[0].copy()
    else:
        # Two estimators: one stable order (learner, then each frame's KC order).
        states = pd.concat(frames, ignore_index=True)
        states = states.sort_values("learner_id", kind="mergesort").reset_index(
            drop=True
        )
    states["source_population"] = states["learner_id"].map(population)
    states = validate_states(states[KNOWLEDGE_STATE_COLUMNS])

    output_dir.mkdir(parents=True, exist_ok=True)
    states.to_csv(output_dir / config.STATE_FILE_NAME, index=False)

    by_population = {}
    for population, (chosen, reason) in decisions.items():
        rows = states[states["source_population"].eq(population)]
        by_population[population] = {
            "estimator": chosen,
            "estimator_reason": reason,
            "estimator_phase": (
                int(rows["estimator_phase"].iloc[0]) if len(rows) else None
            ),
            "model_names": sorted(rows["model_name"].dropna().unique().tolist()),
            "learners": int(rows["learner_id"].nunique()),
            "rows": int(len(rows)),
        }
    estimators = sorted({entry["estimator"] for entry in by_population.values()})
    phases = sorted(
        {
            entry["estimator_phase"]
            for entry in by_population.values()
            if entry["estimator_phase"] is not None
        }
    )
    manifest = {
        "generated_at": inference_timestamp,
        "data_dir": str(data_dir),
        "active_model_file": None if active_model_file is None else str(active_model_file),
        # One population, or "mixed"; the per-learner value is in the CSV.
        "source_population": cohort_label(decisions),
        "by_population": by_population,
        "as_of": as_of_iso,
        "mapping_version": mapping_version,
        # Kept as single values when every learner shares them.
        "estimator": estimators[0] if len(estimators) == 1 else "mixed",
        "estimator_reason": "; ".join(
            f"{population}: {entry['estimator_reason']}"
            for population, entry in by_population.items()
        )
        if len(by_population) > 1
        else next(iter(by_population.values()))["estimator_reason"],
        "recorded_phase": active.get("phase", 1),
        "estimator_phase": phases[0] if len(phases) == 1 else None,
        "estimator_phases": phases,
        "forced_estimator": force_estimator,
        "model_names": sorted(states["model_name"].dropna().unique().tolist()),
        "learners": int(states["learner_id"].nunique()),
        "kcs": int(states["kc_id"].nunique()),
        "rows": int(len(states)),
        "state_status_counts": {
            str(k): int(v) for k, v in states["state_status"].value_counts().items()
        },
        "bkt_parameters": (
            {
                "p_init": bkt.DEFAULT_PARAMS.p_init,
                "p_learn": bkt.DEFAULT_PARAMS.p_learn,
                "p_slip": bkt.DEFAULT_PARAMS.p_slip,
                "p_guess": bkt.DEFAULT_PARAMS.p_guess,
                "p_forget": bkt.DEFAULT_PARAMS.p_forget,
                "note": "fixed, uncalibrated configuration values",
            }
            if "bkt" in estimators
            else None
        ),
        "semantics": {
            "predicted_correctness": "probability of answering the next question on the KC correctly",
            "p_mastery": "BKT latent probability that the KC is known; null for the deep model",
            "cold_start": "prior only: no answer on that KC, not individual evidence",
        },
    }
    write_json(output_dir / config.MANIFEST_FILE_NAME, manifest)
    return states
