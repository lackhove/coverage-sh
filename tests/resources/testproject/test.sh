#!/bin/bash
#
# SPDX-License-Identifier: MIT
# Copyright (c) 2024 Kilian Lackhove
#


SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
"${SCRIPT_DIR}"/syntax_example.sh
"${SCRIPT_DIR}"/inner.py
