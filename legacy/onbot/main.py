import os
import sys
import logging
import argparse

if __name__ == "__main__":
    SCRIPT_DIR = os.path.dirname(
        os.path.realpath(os.path.join(os.getcwd(), os.path.expanduser(__file__)))
    )
    MODULE_ROOT_DIR = os.path.join(SCRIPT_DIR, "..")
    sys.path.insert(0, os.path.normpath(MODULE_ROOT_DIR))

from onbot.bot import Bot
from onbot.config import OnbotConfig, get_config, generate_config_file
from onbot.api_client_synapse_admin import ApiClientSynapseAdmin
from onbot.api_client_matrix import ApiClientMatrix
from onbot.api_client_authentik import ApiClientAuthentik


arg_parser = argparse.ArgumentParser("DZDonbot")
arg_parser.add_argument(
    "--generate_config",
    help="Set this flag to just generate a config yaml file. Set env var `ONBOT_CONFIG_FILE_PATH` to control the output path. Default: ../config.dev.yml",
    action="store_true",
)
logger = logging.getLogger(__name__)
handler = logging.StreamHandler()


def run_bot():
    config: OnbotConfig = get_config()
    logging.basicConfig(level=config.log_level)
    authentik_client = ApiClientAuthentik(
        access_token=config.authentik_server.api_key,
        url=config.authentik_server.url,
    )
    synapse_admin_api_client = ApiClientSynapseAdmin(
        access_token=config.synapse_server.bot_access_token,
        server_url=config.synapse_server.server_url,
        api_base_path=config.synapse_server.admin_api_path,
    )
    matrix_api_client = ApiClientMatrix(
        user=config.synapse_server.bot_user_id,
        access_token=config.synapse_server.bot_access_token,
        device_id=config.synapse_server.bot_device_id,
        server_url=config.synapse_server.server_url,
        server_name=config.synapse_server.server_name,
        state_store_path=config.storage_dir,
    )
    bot = Bot(
        get_config(),
        authentik_client=authentik_client,
        synapse_admin_api_client=synapse_admin_api_client,
        matrix_api_client=matrix_api_client,
        server_tick_wait_time_sec_int=config.server_tick_rate_sec,
    )
    bot.start()


if __name__ == "__main__":
    args = arg_parser.parse_args()
    if args.generate_config:
        generate_config_file()
    else:
        run_bot()
