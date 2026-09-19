# Lab 2 — Run comparison

Experiment `itcs355-lab2` · Study `34f2f83bfb9641e38347f8ddb04c1caf` · 12 finished trials

Adapted from the instructor's [compare_runs.py](https://github.com/pasdptt/public_teaching_mlaiops/blob/30ba98b02e8f233c7d7a36d584738ea442e46bd1/scripts/compare_runs.py).

Seed: `20260101`. Instance: `Standard_F2s_v2`.
Code commit: `610e7cfefbfe080d711a7f621e1d6992fa803ad3`.
DVC hash: `1c886b512c8a5c9bf723da1cd119fc80.dir`. Data fingerprint: `422cccb9136e8140`.

Image:

```text
itcs355u6688124.azurecr.io/itcs355-lab2@sha256:df878435dd990605f1c63dc4538b9cb86dacd31384d0a730c6b2d1c0399b282d
```

Estimated measured-trial cost: **0.0736 THB**.
This is not the Azure bill. It excludes data setup, VM startup/idle time,
final metric/checkpoint writes, and other services. Trial duration includes model logging.

Price checked on 18 September 2026: Linux `Standard_F2s_v2` in `malaysiawest`
costs USD 0.0882/hour for Dedicated/on-demand compute. We used Microsoft's
THB reference price of 2.900677/hour for trial estimates. This is not the actual Azure bill.
Source: [Azure Retail Prices API](https://prices.azure.com/api/retail/prices).

## Comparison

Sorted by validation ROC-AUC. Test ROC-AUC is shown for reporting, not model selection.
`thb_per_point` is the estimated trial cost divided by the validation ROC-AUC gain
over the worst trial, in percentage points. Lower means less cost per added point
against this baseline; it is not a final model-selection rule.
`N/A` means zero gain over the baseline. Calculations use unrounded values;
the table rounds only for display.

| run_id                               |   n_estimators |   max_depth |   min_samples_leaf |   val_roc_auc |   test_roc_auc |   duration_s |   cost_thb | thb_per_point   |
|:-------------------------------------|---------------:|------------:|-------------------:|--------------:|---------------:|-------------:|-----------:|:----------------|
| 4a785cc0-6aef-41d3-a009-9f07bae57dba |            300 |          12 |                 10 |        0.8451 |         0.8464 |       7.1475 |     0.0058 | 0.0031          |
| 8087ae5c-d06f-411e-b6b0-7f7891ffe7ed |            100 |          12 |                 10 |        0.8445 |         0.8467 |       6.6861 |     0.0054 | 0.0030          |
| d1165423-2c74-49c4-8f8d-7950a43a8f27 |            100 |           4 |                 10 |        0.8444 |         0.8529 |       7.0538 |     0.0057 | 0.0032          |
| 1cdb9c22-a668-436e-b0c4-f85ffb2c0b6d |            300 |           8 |                 10 |        0.8428 |         0.8487 |       7.1083 |     0.0057 | 0.0035          |
| 3b10d085-563b-4fe7-b34a-19c50cd4631d |            100 |           4 |                  1 |        0.8424 |         0.8518 |      11.4860 |     0.0093 | 0.0058          |
| 5139268f-dc12-4a9f-b41a-ffe6a8a51e08 |            300 |           4 |                 10 |        0.8422 |         0.8541 |       6.8914 |     0.0056 | 0.0035          |
| 409b9d06-312d-4bf8-ab90-2095dea15356 |            300 |           4 |                  1 |        0.8404 |         0.8537 |       7.4351 |     0.0060 | 0.0043          |
| 2c974d8d-4b8e-46e1-a601-47ff089e24cb |            100 |           8 |                 10 |        0.8402 |         0.8483 |       6.6676 |     0.0054 | 0.0039          |
| 7e815e9e-6669-4059-9fd0-c0eb18bea4e7 |            300 |           8 |                  1 |        0.8338 |         0.8478 |       7.2578 |     0.0058 | 0.0080          |
| fe34ddc9-14e0-43e4-ab2b-d99fe5d0a688 |            100 |           8 |                  1 |        0.8312 |         0.8488 |       6.4525 |     0.0052 | 0.0110          |
| 1730032f-4352-4781-99dc-687b35f1778c |            100 |          12 |                  1 |        0.8268 |         0.8415 |       9.5969 |     0.0077 | 0.2103          |
| da68d600-ebac-4afa-a3a2-19a4ac633bfd |            300 |          12 |                  1 |        0.8265 |         0.8374 |       7.5609 |     0.0061 | N/A             |

## Related evidence

This table compares the original fixed-seed study. See the
[model selection justification](../README.md#model-selection-justification)
and the [five-seed results](lab2-seed-check.json) for the final choice and
seed-variance check.
