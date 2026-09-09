import numpy as np
import pytest

from src.features.purged_cv import PurgedTimeSeriesSplit


def test_purged_split_is_walk_forward_and_has_label_gap():
    splitter = PurgedTimeSeriesSplit(
        n_splits=4,
        purge_gap=7,
        test_size=15,
        min_train_size=30,
    )
    folds = list(splitter.split(np.arange(120)))

    assert len(folds) == 4
    previous_test_start = -1
    for train, test in folds:
        assert len(set(train) & set(test)) == 0
        assert train.max() + 7 < test.min()
        assert test.min() > previous_test_start
        assert np.array_equal(train, np.arange(train.min(), train.max() + 1))
        previous_test_start = int(test.min())


def test_purged_split_rejects_an_impossible_configuration():
    splitter = PurgedTimeSeriesSplit(
        n_splits=3,
        purge_gap=20,
        test_size=20,
        min_train_size=30,
    )

    with pytest.raises(ValueError, match="not enough observations"):
        list(splitter.split(np.arange(90)))
