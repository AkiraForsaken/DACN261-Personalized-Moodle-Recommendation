"""The model stages of one run, from a collector export to a Moodle bundle.

    knowledge_engine estimate -> learner_model -> recommender generate -> bundle

A full run uses the course workspace itself; a per-learner refresh uses a
``refresh/<stamp>/`` folder whose ``data/`` holds the course structure and only
the requested learners (``collector --learners``). Every stage is per learner
(BKT and the deep model score each learner's own answers; profiles and rules
look at one learner), so a refresh produces the same rows for its learners as
a full run would (tests/test_moodle_sync.py checks this).

Both kinds reuse the course's estimator decision and trained model
(``knowledge/active_model.json``, ``kt/``): a refresh never trains.

Runs in the PyTorch environment (.venv-kt) when a deep model is promoted;
``python -m moodle_sync stages`` is the command the refresh worker calls there.
"""

from pathlib import Path

from .bundle import KIND_SNAPSHOT, build_bundle


def run_model_stages(layout, workspace, kind=KIND_SNAPSHOT, generated_at=None):
    """Estimate, build profiles, recommend and bundle the export in ``layout.data``.

    Returns the bundle manifest. ``workspace`` supplies the course-level
    estimator decision and model; ``layout`` is where this run reads and writes.
    """
    from knowledge_engine.estimate import estimate
    from learner_model.builder import build_complete_learner_model
    from recommender.generate import generate

    data = Path(layout.data)
    if not (data / "learner_roster.csv").is_file():
        raise FileNotFoundError(f"No collector export in {data}")
    estimate(
        data,
        layout.knowledge_states,
        active_model_file=workspace.active_model_file,
        kt_dir=workspace.kt,
        inference_timestamp=generated_at,
    )
    build_complete_learner_model(
        data_dir=data,
        output_dir=layout.learner_model,
        knowledge_state_file=layout.knowledge_state_file,
        generated_at=generated_at,
    )
    generate(
        layout.learner_model, data, layout.recommendations, generated_at=generated_at
    )
    return build_bundle(
        layout.learner_model, layout.recommendations, data, layout.bundle, kind
    )
