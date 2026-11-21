# Archived notebooks

Superseded by the Chapter 4 series in `knowledge_tracing/notebooks/` (10–15). Kept as the
record of the runs they produced; they are not maintained and may refer to old paths
(they lived in `knowledge_tracing/`).

| Notebook | What it was |
|---|---|
| `01_pykt_data_preparation.ipynb` | English Learning Events (synthetic): pyKT preparation on Colab. Its outputs are the curated `data/processed/` files. |
| `02_deep_kt_training_and_evaluation.ipynb` | English: DKT/SAKT/AKT training, the 8-trial validation search and evaluation; produced `knowledge_tracing/artifacts/` and `configs/selected_model.json`. The search now lives in `knowledge_tracing/tune.py`. |
| `03_knowledge_state_extraction.ipynb` | English: learner-by-KC state export with the selected AKT checkpoint (`export_states.py`). |
| `04_ednet_benchmark_colab.ipynb` | EdNet-KT1 run 1 on Colab: DKT/SAKT/AKT, six cohorts. The executed copy is `data/colab/04_ednet_benchmark_colab_run1_executed.ipynb`. |
| `05_ednet_benchmark_colab_dkvmn_saint.ipynb` | EdNet-KT1 run 2 on Colab: DKVMN/SAINT added to existing runs; ran out of GPU time during the 20,000 cohort (executed outputs kept as the run log). Finished locally with notebook 13. |
