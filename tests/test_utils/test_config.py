"""Tests for config utilities."""

import os
import tempfile

import pytest
from ledgerloop.utils.config import load_config


class TestLoadConfig:
    def test_loads_simple_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("KEY=value\n")
            path = f.name
        try:
            config = load_config(path, apply=False)
            assert config == {"KEY": "value"}
        finally:
            os.unlink(path)

    def test_strips_inline_comment(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write('KEY=value # this is a comment\n')
            path = f.name
        try:
            config = load_config(path, apply=False)
            assert config == {"KEY": "value"}
        finally:
            os.unlink(path)

    def test_skips_comments(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("# comment line\nKEY=value\n")
            path = f.name
        try:
            config = load_config(path, apply=False)
            assert config == {"KEY": "value"}
        finally:
            os.unlink(path)

    def test_missing_file_returns_empty(self):
        config = load_config("/nonexistent/path.env", apply=False)
        assert config == {}

    def test_does_not_override_existing_env(self):
        os.environ["EXISTING_KEY"] = "existing_value"
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
                f.write("EXISTING_KEY=new_value\n")
                path = f.name
            config = load_config(path, apply=True)
            assert config == {"EXISTING_KEY": "new_value"}
            assert os.environ["EXISTING_KEY"] == "existing_value"
        finally:
            os.unlink(path)
            del os.environ["EXISTING_KEY"]