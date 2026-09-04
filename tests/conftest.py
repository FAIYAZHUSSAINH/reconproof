"""Shared fixtures: one generated batch per test session."""

from __future__ import annotations

import pytest

from reconproof.generate import generate
from reconproof.ingest import load
from reconproof.report import load_ground_truth


@pytest.fixture(scope="session")
def batch_dir(tmp_path_factory):
    """Generate the standard seed-42 batch once, into a temporary directory.

    Tests never touch `data/generated` in the repo, so a failing test cannot
    quietly rewrite the committed data the README quotes.
    """
    root = tmp_path_factory.mktemp("reconproof-data")
    generate(
        seed=42,
        difficulty="standard",
        data_dir=root / "generated",
        truth_path=root / "ground_truth.json",
    )
    return root


@pytest.fixture
def store(batch_dir):
    """A fresh RecordStore per test - several tests mutate it."""
    return load(batch_dir / "generated")


@pytest.fixture(scope="session")
def truth(batch_dir):
    return load_ground_truth(batch_dir / "ground_truth.json")
