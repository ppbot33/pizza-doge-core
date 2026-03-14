#!/bin/bash

cd "$(dirname "$0")"
source ./.venv/bin/activate
# freqtrade trade --config user_data/config.json --strategy SampleStrategy
freqtrade webserver --config user_data/config.json
