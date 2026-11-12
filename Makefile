# Copyright 2026 Bruno Venceslau
# SPDX-License-Identifier: Apache-2.0

# The checks CI runs, from one entry point: make check.

.PHONY: check
check:
	python3 -m unittest discover -s actions/attribution
