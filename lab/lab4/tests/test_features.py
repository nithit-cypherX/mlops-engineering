"""Offline unit tests for feature preparation, not model-behaviour tests."""
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from service.app import _score_model


def test_scoring_orders_features_and_preserves_row_values():
    # Input dictionaries deliberately use different orders from the model contract.
    rows = [
        {"load_pct": 68.0, "ambient_humidity": 55.0, "hours_since_service": 4200.0,
         "pressure_kpa": 315.2, "vibration_mm_s": 3.1, "temp_c": 78.4},
        {"pressure_kpa": 300.0, "temp_c": 90.0, "load_pct": 80.0,
         "vibration_mm_s": 4.2, "ambient_humidity": 60.0, "hours_since_service": 8500.0},
    ]
    model = Mock()
    model.predict_proba.return_value = np.array([[0.8, 0.2], [0.3, 0.7]])

    probabilities = _score_model(model, rows)

    model.predict_proba.assert_called_once()
    actual = model.predict_proba.call_args.args[0]
    expected = pd.DataFrame(
        [[78.4, 3.1, 315.2, 4200.0, 68.0, 55.0],
         [90.0, 4.2, 300.0, 8500.0, 80.0, 60.0]],
        columns=["temp_c", "vibration_mm_s", "pressure_kpa", "hours_since_service",
                 "load_pct", "ambient_humidity"],
    )
    pd.testing.assert_frame_equal(actual, expected)
    assert probabilities == [0.2, 0.7]


def test_missing_feature_does_not_reach_model():
    model = Mock()
    row = {"vibration_mm_s": 3.1, "pressure_kpa": 315.2,
           "hours_since_service": 4200.0, "load_pct": 68.0, "ambient_humidity": 55.0}

    with pytest.raises(KeyError, match="temp_c"):
        _score_model(model, [row])

    model.predict_proba.assert_not_called()
