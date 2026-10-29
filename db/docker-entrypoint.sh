#!/usr/bin/env bash
# Runs inside the official postgres image on first start.
set -e
export PGUSER=postgres
bash /sql/init.sh
