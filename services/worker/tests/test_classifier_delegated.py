"""Tests for Loop/OneNote/Whiteboard classification."""
import pytest
from unittest.mock import MagicMock
from app.pipeline.classifier import FileClassifier, Category, Action


@pytest.fixture
def classifier():
    return FileClassifier()


@pytest.fixture
def config():
    cfg = MagicMock()
    cfg.max_file_size_bytes = 52_428_800
    return cfg


def test_loop_file_detected(classifier, config):
    result = classifier.classify("project-plan.loop", "File", 1000, config)
    assert result.category == Category.DELEGATED_CONTENT
    assert result.action == Action.PENDING_MANUAL


def test_fluid_file_detected(classifier, config):
    result = classifier.classify("notes.fluid", "File", 500, config)
    assert result.category == Category.DELEGATED_CONTENT
    assert result.action == Action.PENDING_MANUAL


def test_onenote_file_detected(classifier, config):
    result = classifier.classify("notebook.one", "File", 2000, config)
    assert result.category == Category.DELEGATED_CONTENT
    assert result.action == Action.PENDING_MANUAL


def test_whiteboard_file_detected(classifier, config):
    result = classifier.classify("brainstorm.whiteboard", "File", 3000, config)
    assert result.category == Category.DELEGATED_CONTENT
    assert result.action == Action.PENDING_MANUAL


def test_regular_docx_unchanged(classifier, config):
    result = classifier.classify("report.docx", "File", 1000, config)
    assert result.category == Category.PROCESSABLE
    assert result.action == Action.FULL_ANALYSIS


def test_classify_with_package_metadata(classifier, config):
    result = classifier.classify_with_metadata(
        "My Notebook", "File", 0, config,
        metadata={"package": {"type": "oneNote"}},
    )
    assert result.category == Category.DELEGATED_CONTENT
    assert result.action == Action.PENDING_MANUAL


def test_classify_with_metadata_no_package(classifier, config):
    result = classifier.classify_with_metadata(
        "report.docx", "File", 1000, config,
        metadata={},
    )
    assert result.category == Category.PROCESSABLE


def test_classify_with_metadata_whiteboard_package(classifier, config):
    """Verify classify_with_metadata detects Whiteboard via package facet."""
    result = classifier.classify_with_metadata(
        "untitled", "File", 0, config,
        metadata={"package": {"type": "whiteboard"}},
    )
    assert result.category == Category.DELEGATED_CONTENT
    assert result.action == Action.PENDING_MANUAL
