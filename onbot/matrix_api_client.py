class MatrixApiClient:
    def __init__(
        self,
        access_token: str,
        server_domain: str,
        api_base_path: str = "/_matrix/client/v1",
        protocol: str = "http",
    ):
        self.access_token = access_token
        self.api_base_url = f"{protocol}://{server_domain}{api_base_path}/"
    def get_rooms_in_space(self):
        # https://matrix.org/docs/api/#get-/_matrix/client/v1/rooms/-roomId-/hierarchy