from onbot.bot import Bot
from onbot.config import OnbotConfig
from onbot.utils import YamlConfigFileHandler
from onbot.synapse_admin_api_client import SynapseAdminApiClient
from onbot.matrix_api_client import MatrixApiClient
from onbot.authentik_api_client import AuthentikApiClient
import os
import logging

logger = logging.getLogger(__name__)
handler = logging.StreamHandler()


def run_bot():
    config_file = os.getenv("ONBOT_CONFIG_FILE_PATH", "config.yml")
    config_handler = YamlConfigFileHandler(OnbotConfig, config_file)
    config_handler.generate_config_file(exists_ok=True)
    config: OnbotConfig = config_handler.get_config()
    logging.basicConfig(level=config.log_level)
    authentik_client = AuthentikApiClient(
        access_token=config.authentik_server.api_key,
        server_api_base_url=config.authentik_server.public_api_url,
    )
    synapse_admin_api_client = SynapseAdminApiClient(
        access_token=config.synapse_server.bot_access_token,
        server_url=config.synapse_server.server_url,
        api_base_path=config.synapse_server.admin_api_path,
    )
    matrix_api_client = MatrixApiClient(
        user=config.synapse_server.bot_user_id,
        access_token=config.synapse_server.bot_access_token,
        device_id=config.synapse_server.bot_device_id,
        server_url=config.synapse_server.server_url,
        server_name=config.synapse_server.server_name,
        state_store_path=config.storage_dir,
    )
    bot = Bot(
        config_handler.get_config(),
        authentik_client=authentik_client,
        synapse_admin_api_client=synapse_admin_api_client,
        matrix_api_client=matrix_api_client,
    )
    bot.start()


if __name__ == "__main__":
    run_bot()
