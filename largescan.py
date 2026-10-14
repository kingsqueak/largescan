#!/usr/bin/env python3
"""Run largescan from a source checkout."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from largescan import cli

if __name__ == "__main__":
    cli()
