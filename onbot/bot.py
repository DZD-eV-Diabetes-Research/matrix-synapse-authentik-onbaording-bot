from nio import HttpClient as MatrixClient, RoomVisibility, RoomPreset

from onbot.config import ConfigDefaultModel
from onbot.authentik_api_client import AuthentikApiClient
from onbot.synapse_admin_api_client import SynapseAdminApiClient


class Bot:
    def __init__(
        self,
        config: ConfigDefaultModel,
        authentik_client: AuthentikApiClient,
        synapse_admin_client: SynapseAdminApiClient,
        synapse_client: MatrixClient,
    ):
        self.config = config
        self.authentik_client = authentik_client
        self.synapse_admin_client = synapse_admin_client
        self.synapse_client = synapse_client

    def create_room_test(self, name):
        self.synapse_client.room_create
