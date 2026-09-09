"""Walk-forward cross validation with an explicit label-overlap purge gap."""

from __future__ import annotations

from typing import Iterator

import numpy as np


class PurgedTimeSeriesSplit:
    """Expanding-window time-series splitter that never trains on the future.

    ``purge_gap`` removes observations immediately before each test fold.  It
    should be at least the largest target horizon, ensuring that no training
    label reaches into the test interval.  ``max_train_size`` optionally bounds
    old history without introducing future samples.
    """

    def __init__(
        self,
        n_splits: int = 5,
        *,
        purge_gap: int = 10,
        test_size: int | None = None,
        min_train_size: int | None = None,
        max_train_size: int | None = None,
    ) -> None:
        if n_splits < 2:
            raise ValueError("n_splits must be at least two")
        if purge_gap < 0:
            raise ValueError("purge_gap cannot be negative")
        if test_size is not None and test_size < 1:
            raise ValueError("test_size must be positive")
        if min_train_size is not None and min_train_size < 1:
            raise ValueError("min_train_size must be positive")
        if max_train_size is not None and max_train_size < 1:
            raise ValueError("max_train_size must be positive")
        self.n_splits = n_splits
        self.purge_gap = purge_gap
        self.test_size = test_size
        self.min_train_size = min_train_size
        self.max_train_size = max_train_size

    def get_n_splits(
        self, X=None, y=None, groups=None  # noqa: N803 - sklearn convention
    ) -> int:
        del X, y, groups
        return self.n_splits

    def split(
        self, X, y=None, groups=None  # noqa: N803 - sklearn convention
    ) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        del y, groups
        n_samples = len(X)
        test_size = self.test_size or n_samples // (self.n_splits + 1)
        if test_size < 1:
            raise ValueError("not enough observations for the requested folds")
        first_test_start = n_samples - self.n_splits * test_size
        required_train = self.min_train_size or 1
        if first_test_start - self.purge_gap < required_train:
            raise ValueError(
                "not enough observations after applying test folds, purge gap and "
                "minimum train size"
            )

        for fold in range(self.n_splits):
            test_start = first_test_start + fold * test_size
            test_end = min(n_samples, test_start + test_size)
            train_end = test_start - self.purge_gap
            train_start = 0
            if self.max_train_size is not None:
                train_start = max(0, train_end - self.max_train_size)
            train_indices = np.arange(train_start, train_end, dtype=int)
            test_indices = np.arange(test_start, test_end, dtype=int)
            if len(train_indices) < required_train:
                raise ValueError(
                    "a fold contains fewer training rows than min_train_size"
                )
            yield train_indices, test_indices
