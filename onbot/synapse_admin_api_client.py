class SynapseAdminApiClient:
    def __init__(
        self,
        user: str,
        access_token: str,
        server_domain: str,
        api_base_path: str = "/_synapse/admin/v1",
        protocol: str = "http",
    ):
        self.user = user
        self.access_token = access_token
        self.api_url = f"{protocol}://{server_domain}/{api_base_path}"

    def _call(self, path: str):
        pass

    def list_room(self, in_space: str = None):
        """https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#list-room-api

        Args:
            in_space (_type_): _description_
        """
        self._call("rooms")
