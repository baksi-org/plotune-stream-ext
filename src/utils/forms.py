from plotune_sdk import FormLayout

from utils.constant_helper import get_config

from dataclasses import dataclass


@dataclass
class StreamInput:
    stream_type: str
    stream_name: str


def dynamic_form() -> dict:
    """
    Build and return the dynamic relay configuration form schema.
    """
    form = FormLayout()

    # ------------------------------------------------------------------
    # Source
    # ------------------------------------------------------------------
    (
        form.add_tab("Connection")
        .add_combobox(
            "stream_type",
            "Stream Connection Type",
            ["consumer", "producer"],
            default="consumer",
            required=True,
        )
        .add_text(
            "stream_name",
            "Stream Name",
            default="",
            required=True,
        )
    )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    connection = get_config().get("connection", {})
    host = connection.get("ip", "127.0.0.1")
    port = connection.get("port", "")
    base_url = f"http://{host}:{port}"

    (
        form.add_group("Actions").add_button(
            "start_button",
            "Visit",
            {
                "method": "POST",
                "url": f"{base_url}/start",
                "payload_fields": [
                    "stream_type",
                    "stream_name",
                ],
            },
        )
    )

    return form.to_schema()


def form_dict_to_input(data: dict) -> StreamInput:
    """
    Convert submitted form data into a RelayInput model.
    """

    def safe_int(value, default: int | None = None) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    return StreamInput(
        stream_type=data.get("stream_type"), stream_name=data.get("stream_name")
    )
