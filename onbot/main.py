from onbot.bot import Bot
from onbot.config import OnbotConfig, get_config
from onbot.api_client_synapse_admin import ApiClientSynapseAdmin
from onbot.api_client_matrix import ApiClientMatrix
from onbot.api_client_authentik import ApiClientAuthentik
import os
import logging

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
    run_bot()
