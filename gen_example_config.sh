#!/bin/bash
export ONBOT_CONFIG_FILE_PATH=config.example.yml
export ONBOT_STORAGE_DIR="/home/username/.config/onbot"
/home/tim/Repos/github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot/.dzdonbot/bin/python onbot/main.py --generate_config
